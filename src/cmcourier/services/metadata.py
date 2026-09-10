"""Servicio de resolución de metadata.

Cadena de fallback de fuente por campo, con regexes de validación,
fallback a valor por defecto, cadenas de dependencia entre campos
(``field.<NAME>``, 147) resueltas en orden topológico, normalización
de aliases de campos y pre-fetching ansioso de fuentes ``csv:<alias>``
al construirse. El stage S3 de cada `pipeline` depende de este
servicio.

147 REQ-001 sacó el self-healing de CIF hardcodeado: en vez de curar
``BAC_CIF`` de un solo salto con un ``cif_override`` hilvanado a mano,
cualquier campo puede declarar que su clave de búsqueda es el valor ya
resuelto de otro campo, y el orden de resolución sale del grafo.

Principio I de la Constitución: importa solo ``cmcourier.domain.*`` y
stdlib. Principio VIII: nunca loguear VALORES resueltos de campos
(PII); loguear solo NOMBRES de campos.
"""

from __future__ import annotations

__all__ = [
    "FieldResolution",
    "FieldSourceConfig",
    "MetadataConfig",
    "MetadataResolution",
    "MetadataService",
    "PadConfig",
    "SourceConfig",
    "ValidationConfig",
    "ValueFormat",
    "apply_format",
]

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from cmcourier.config.schema import split_field_reference, split_lookup_source_type
from cmcourier.domain.exceptions import (
    ConfigurationError,
    DefaultValidationFailedError,
    MetadataError,
    SourceFailedError,
)
from cmcourier.domain.models import (
    ClientTrigger,
    CMMapping,
    ResolvedMetadata,
    RVABREPDocument,
    Trigger,
)
from cmcourier.domain.ports import IDataSource

_logger = logging.getLogger(__name__)

# 130: `csv:` / `as400:` / `mssql:` resuelven contra el registro por alias
# con el mismo contrato; `split_lookup_source_type` es el único parser.


def _trigger_cif(trigger: Trigger) -> str | None:
    """046: extrae el CIF de la forma que el trigger exponga.

    ``ClientTrigger.cif`` es la ruta canónica del atributo; los
    triggers basados en fila (``RvabrepRowTrigger``,
    ``LocalScanTrigger``) llevan el CIF dentro de su fila bajo la
    columna ``col_cif`` configurada. La proyección ``audit_row`` ya
    sabe cómo extraerlo; aquí solo se consume eso.
    """
    if isinstance(trigger, ClientTrigger):
        return trigger.cif
    audit = trigger.audit_row()
    cif = audit.get("cif")
    return cif if isinstance(cif, str) and cif else None


# Indica "todas las fuentes probadas" en el contexto de ``SourceFailedError``.
_ALL_SOURCES_SENTINEL = "<all>"


# ---------------------------------------------------------------------------
# Dataclasses públicas de configuración / resultado
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    """Validación opcional para el valor resuelto de una fuente."""

    allowed_pattern: str | None = None


@dataclass(frozen=True, slots=True)
class PadConfig:
    """146: relleno hasta ``width`` con ``char`` (un solo carácter).

    El default ``"0"`` sirve al caso de ``pad_left`` (cuentas de largo
    fijo); para ``pad_right`` el YAML declara ``" "``, que es cómo DB2
    entrega una columna ``CHAR(n)``. El schema (:mod:`config.schema`)
    modela ese default por campo; acá la dataclass es una sola.
    """

    width: int
    char: str = "0"


@dataclass(frozen=True, slots=True)
class ValueFormat:
    """146: normalización declarativa del valor de un metadato.

    Espejo en el dominio del servicio de ``ValueFormatModel``. Se aplica
    con :func:`apply_format` SIEMPRE en el mismo orden fijo — ver el
    docstring de esa función.
    """

    trim: bool = False
    case: Literal["upper", "lower"] | None = None
    strip_leading_zeros: bool = False
    pad_left: PadConfig | None = None
    pad_right: PadConfig | None = None
    truncate: int | None = None


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Un paso de la cadena de fallback de un campo.

    084: ``lookup_value_source`` define **de dónde sale el valor**
    que se va a buscar en la fuente de lookup (CSV o AS400). Default
    ``"trigger.cif"`` preserva el contrato pre-084 (CSV indexado por
    CIF del trigger). Sintaxis admitida:

    - ``"trigger.cif"`` / ``"trigger.shortname"`` / ``"trigger.system_id"`` —
      atributos del trigger (o equivalente en ``audit_row``).
    - ``"rvabrep.<col>"`` — atributo del :class:`RVABREPDocument`
      (cualquier columna del modelo: ``txn_num``, ``index1`` … ``index7``,
      ``image_type``, etc.).
    - ``"field.<CANONICAL_NAME>"`` (147) — el valor YA RESUELTO de otro
      campo. Es lo que permite encadenar saltos, y crea una arista en el
      grafo de dependencias que ordena la resolución.
    """

    source_type: str
    lookup_value_column: str
    lookup_key_column: str | None = None
    validation: ValidationConfig | None = None
    lookup_value_source: str = "trigger.cif"
    # 146: corre ENTRE el fetch y `validation`.
    format: ValueFormat | None = None


@dataclass(frozen=True, slots=True)
class FieldSourceConfig:
    """Cadena de fallback completa más el `default` para un campo
    canónico (``BAC_*``)."""

    sources: tuple[SourceConfig, ...]
    default_value: str | None = None
    # 146: corre sobre el valor ganador y sobre `default_value`, justo
    # antes de devolverlo.
    format: ValueFormat | None = None


@dataclass(frozen=True, slots=True)
class MetadataConfig:
    """Configuración de alto nivel para la resolución de metadata."""

    field_aliases: Mapping[str, str]
    field_sources: Mapping[str, FieldSourceConfig]
    prefetch_enabled: bool = True


@dataclass(frozen=True, slots=True)
class MetadataResolution:
    """Resultado de ``resolve()``: la bolsa de metadata más el trigger
    (posiblemente self-healed).

    .. deprecated:: 147
       ``healed_trigger`` y ``healed_cif`` quedan **deprecados** por 147
       REQ-004: la identidad del cliente ya no se deduce del trigger curado
       sino de la :class:`~cmcourier.services.identity.ResolvedIdentity` que
       S2 resuelve, y de ahí salen las TRES escrituras (``migration_log``,
       ``CTECIF``/``CTENUM`` de RVIMGLOG y la clave del mapeo). Ninguna
       escritura los lee ya. Se mantienen un ciclo por compatibilidad de
       hooks y tests, y porque el ``document_cache`` (037) sigue guardando
       ``trigger_cif`` para reconstruir un ``ClientTrigger`` en un cache hit.

    046: ``healed_trigger`` es polimórfico. Para inputs
    ``ClientTrigger`` el resolver puede producir un nuevo
    ``ClientTrigger`` con el campo CIF seteado al valor self-healed.
    Para los subtipos basados en fila se devuelve el trigger original
    sin cambios: el mapping de la fila es inmutable, y el CIF
    self-healed vive en ``metadata`` (como la propiedad
    ``cmcourier:BAC_CIF``) y en el `document_cache` (037) en lugar
    de re-proyectarse sobre el trigger.
    """

    metadata: ResolvedMetadata
    #: .. deprecated:: 147 — ver el docstring de la clase.
    healed_trigger: Trigger
    # 046: el CIF resuelto, capturado explícitamente para que el
    # `document_cache` pueda persistirlo sin inspeccionar el subtipo
    # de trigger.
    #: .. deprecated:: 147 — ver el docstring de la clase.
    healed_cif: str | None = None


@dataclass(frozen=True, slots=True)
class FieldResolution:
    """147: el valor de un campo MÁS la cadena que se intentó para llegar.

    ``value`` en ``None`` significa "ninguna fuente dio y no hubo default".
    ``chain`` lista cada fuente probada, en orden, incluyendo los saltos de
    los campos de los que éste dependía — es lo que vuelve diagnóstico al
    error de :class:`~cmcourier.domain.exceptions.IdentityResolutionError`.
    Nunca lleva VALORES (Principio VIII): sólo nombres y motivos.
    """

    field: str
    value: str | None
    chain: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pad(value: str, pad: PadConfig | None, *, left: bool) -> str:
    """Rellena hasta ``pad.width``. NUNCA recorta: un valor más largo
    vuelve intacto (para recortar está ``truncate``, que es explícito)."""
    if pad is None or len(value) >= pad.width:
        return value
    return value.rjust(pad.width, pad.char) if left else value.ljust(pad.width, pad.char)


def apply_format(value: str, fmt: ValueFormat | None) -> str:
    """146: normaliza *value* según *fmt*. Pura y sin efectos.

    El orden es FIJO y no configurable — un orden libre vuelve el YAML
    imposible de leer y de auditar:

    1. ``trim`` — ``value.strip()``
    2. ``case`` — ``upper()`` / ``lower()``
    3. ``strip_leading_zeros`` — saca los ceros de la izquierda
    4. ``pad_left`` — rellena a la izquierda hasta ``width``
    5. ``pad_right`` — rellena a la derecha hasta ``width``
    6. ``truncate`` — se queda con los primeros ``N`` caracteres

    Reglas de borde: ``strip_leading_zeros`` nunca devuelve vacío
    (``"00000"`` → ``"0"``, porque un vacío significa "esta fuente no
    dio" y borraría un valor legítimo) y ``pad_*`` nunca recorta.
    ``fmt`` en ``None`` devuelve *value* sin tocar.
    """
    if fmt is None:
        return value
    if fmt.trim:
        value = value.strip()
    if fmt.case == "upper":
        value = value.upper()
    elif fmt.case == "lower":
        value = value.lower()
    if fmt.strip_leading_zeros and value:
        value = value.lstrip("0") or "0"
    value = _pad(value, fmt.pad_left, left=True)
    value = _pad(value, fmt.pad_right, left=False)
    if fmt.truncate is not None:
        value = value[: fmt.truncate]
    return value


def _validates(value: str, validation: ValidationConfig | None) -> bool:
    """Devuelve ``True`` si ``value`` pasa la validación (validación
    ``None`` = siempre ``True``)."""
    if validation is None or validation.allowed_pattern is None:
        return True
    return re.fullmatch(validation.allowed_pattern, value) is not None


def _record(attempts: list[str] | None, field: str, sc: SourceConfig, outcome: str) -> None:
    """147: anota un intento en la traza de la cadena. Sin VALORES (PII):
    sólo el campo, la coordenada de la fuente y por qué se descartó."""
    if attempts is None:
        return
    if split_lookup_source_type(sc.source_type) is None:
        where = f"{sc.source_type}.{sc.lookup_value_column}"
    else:
        where = f"{sc.source_type}[key={sc.lookup_value_source}]"
    attempts.append(f"{field} <- {where}: {outcome}")


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------


class MetadataService:
    """Resolución de metadata por campo, con cadenas de fallback y cadenas
    de DEPENDENCIA entre campos (147).

    Un campo puede declarar ``lookup_value_source: "field.<OTRO>"``: su clave
    de búsqueda es el valor ya resuelto de otro campo. El servicio arma el
    grafo, lo ordena topológicamente y resuelve en ese orden. Eso reemplaza
    al self-healing de CIF hardcodeado pre-147 (que sólo curaba ``BAC_CIF``,
    de un solo salto, con un ``cif_override`` hilvanado a mano).

    Ver ``specs/005-metadata-service/{spec,plan}.md`` y
    ``specs/147-identity-resolution/spec.md`` para el contexto completo.
    """

    def __init__(
        self,
        config: MetadataConfig,
        sources_registry: Mapping[str, IDataSource],
    ) -> None:
        self._config = config
        self._sources_registry = sources_registry
        # 122: los aliases son inmutables — pre-122 este dict se
        # reconstruía UNA VEZ POR DOCUMENTO dentro de resolve().
        self._aliases_lower = {k.lower(): v for k, v in config.field_aliases.items()}
        # Forma de la clave de cache:
        # (alias, key_column, key_value, value_column) -> value.
        self._csv_cache: dict[tuple[str, str, str, str], str] = {}
        # 147 REQ-001: grafo campo → dependencias, congelado al construir.
        self._dependencies = {
            name: self._declared_dependencies(name) for name in config.field_sources
        }
        self._order_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], tuple[str, ...]] = {}
        # 147 REQ-005: memo de la CADENA, con vida de corrida. Ortogonal al
        # `prefetch` de tablas (que indexa la tabla entera al arrancar) y al
        # cache de metadata cross-batch de 037 (que vive en SQLite): éste
        # evita repetir el MISMO salto para todos los documentos de un mismo
        # cliente dentro de la misma corrida. Memoiza también los misses: un
        # cliente que no está en la tabla no se vuelve a preguntar.
        self._lookup_memo: dict[tuple[str, str, str, str, str], str | None] = {}
        self._memo_hits = 0
        if config.prefetch_enabled:
            self._prefetch_csv_sources()

    @property
    def memo_hits(self) -> int:
        """147 REQ-005: cuántos saltos de cadena se ahorraron por el memo."""
        return self._memo_hits

    # --- construcción --------------------------------------------------

    def _prefetch_csv_sources(self) -> None:
        """084: prefetch para CSV **y** AS400. El nombre se conserva
        por backward-compat — internamente cubre ambos tipos de lookup
        source con el mismo contrato (``get_all()`` + indexado en
        memoria por ``key_column``)."""
        seen_pairs: set[tuple[str, str, str]] = set()
        for fsc in self._config.field_sources.values():
            for sc in fsc.sources:
                lookup = split_lookup_source_type(sc.source_type)
                if lookup is None:
                    continue
                prefix_label, alias = lookup
                if alias not in self._sources_registry:
                    raise ConfigurationError(
                        f"unknown {prefix_label} alias referenced in metadata config",
                        alias=alias,
                    )
                if sc.lookup_key_column is None:
                    raise ConfigurationError(
                        f"{prefix_label} source requires lookup_key_column",
                        source_type=sc.source_type,
                    )
                triple = (alias, sc.lookup_key_column, sc.lookup_value_column)
                if triple in seen_pairs:
                    continue
                seen_pairs.add(triple)
                self._populate_cache_for(alias, sc.lookup_key_column, sc.lookup_value_column)

    def _populate_cache_for(self, alias: str, key_col: str, val_col: str) -> None:
        for row in self._sources_registry[alias].get_all():
            key = row.get(key_col)
            val = row.get(val_col)
            if key is None or val is None:
                continue
            cache_key = (alias, key_col, str(key), val_col)
            self._csv_cache.setdefault(cache_key, str(val))  # ante duplicados gana el primero

    # --- punto de entrada público --------------------------------------

    def resolve(
        self,
        trigger: Trigger,
        document: RVABREPDocument,
        mapping: CMMapping,
        seed: Mapping[str, str] | None = None,
    ) -> MetadataResolution:
        """Resuelve cada campo requerido de metadata.

        147 REQ-001: se resuelve en orden TOPOLÓGICO, no en el orden de
        entrada, y sólo los campos pedidos más sus dependencias transitivas
        (el resto de ``field_sources`` ni se toca).

        147 REQ-003: *seed* son los campos que la resolución de identidad ya
        resolvió al INICIO de S2. Entran al dict de resolución como si ya se
        hubieran resuelto en esta pasada, así que S3 no repite el trabajo: ni
        el campo sembrado, ni las dependencias que existían SÓLO para llegar
        a él (el ahorro es la cadena entera, no el último salto). El memo por
        corrida (REQ-005) hace lo mismo entre documentos; la semilla lo hace
        entre etapas del MISMO documento, donde el memo ya alcanzaría pero
        igual pagaría el recorrido del grafo.
        """
        canonical_fields, canonical_to_friendly = self._normalize_fields_with_friendly(
            mapping.required_metadata_fields
        )
        values = self._resolve_graph(canonical_fields, trigger, document, seed)
        resolved = {f: values[f] for f in canonical_fields if f in values}

        # 046: el trigger es polimórfico. ``_trigger_cif`` extrae el
        # CIF del atributo que use cada subtipo (``ClientTrigger.cif``
        # o ``row[col_cif]`` para los subtipos basados en fila).
        # 147: el nombre ``BAC_CIF`` sobrevive SÓLO acá, alimentando los
        # campos deprecados ``healed_trigger`` / ``healed_cif`` (REQ-004 los
        # reemplaza por ``ResolvedIdentity``). La resolución en sí ya no
        # conoce ningún nombre de campo.
        current_cif = _trigger_cif(trigger) or values.get("BAC_CIF")

        # 038: traduce las claves de propiedad a IDs de propiedad
        # `cmis` cuando el mapping incluye un catálogo
        # (``MetadatosCM.CMISPropertyId``). Las claves que no están en
        # el catálogo (o todas las claves cuando el catálogo está
        # ausente / ``None``) pasan sin modificación: backward-compat.
        if mapping.cmis_property_ids:
            translated: dict[str, str] = {}
            for canonical, value in resolved.items():
                friendly = canonical_to_friendly.get(canonical, canonical)
                cmis_id = mapping.cmis_property_ids.get(friendly)
                translated[cmis_id if cmis_id else canonical] = value
            resolved = translated

        # 046: si la entrada fue un ``ClientTrigger`` sin CIF y se
        # resolvió uno, se devuelve un ``ClientTrigger`` fresco con
        # el CIF self-healed para que el código downstream que lea
        # ``trigger.cif`` directamente (post-046 ya no queda nada
        # adentro de cmcourier, pero tests / hooks podrían) vea el
        # nuevo valor. Los triggers basados en fila quedan sin
        # cambios (su mapping de fila es inmutable); el CIF
        # self-healed viaja en ``metadata`` + ``healed_cif``.
        healed_trigger: Trigger = trigger
        if isinstance(trigger, ClientTrigger) and trigger.cif != current_cif:
            healed_trigger = ClientTrigger(
                shortname=trigger.shortname,
                cif=current_cif,
                system_id=trigger.system_id,
            )
        return MetadataResolution(
            metadata=ResolvedMetadata.from_dict(resolved),
            healed_trigger=healed_trigger,
            healed_cif=current_cif,
        )

    # --- 147 REQ-001: grafo de dependencias entre campos ------------------

    def _declared_dependencies(self, canonical_field: str) -> tuple[str, ...]:
        """Los campos referenciados por ``field.<NAME>`` en la cadena de
        *canonical_field*, deduplicados y en orden de config.

        Una referencia a un campo que no existe en ``field_sources`` se
        ignora acá: el schema ya la rechazó al cargar el YAML (147 REQ-001) y
        en runtime se comporta como una dependencia que no resolvió.
        """
        fsc = self._config.field_sources.get(canonical_field)
        if fsc is None:
            return ()
        deps: list[str] = []
        for sc in fsc.sources:
            dep = split_field_reference(sc.lookup_value_source)
            if dep is None or dep not in self._config.field_sources or dep in deps:
                continue
            deps.append(dep)
        return tuple(deps)

    def _resolution_order(
        self, requested: Sequence[str], seeded: frozenset[str] = frozenset()
    ) -> tuple[str, ...]:
        """Orden topológico del cierre transitivo de *requested*: dependencias
        antes que dependientes, y NADA que no haga falta para lo pedido.

        147 REQ-003: un campo de *seeded* corta la bajada — ya está resuelto,
        así que sus dependencias dejan de hacer falta.
        """
        cache_key = (tuple(requested), tuple(sorted(seeded)))
        cached = self._order_cache.get(cache_key)
        if cached is not None:
            return cached
        order: list[str] = []
        done: set[str] = set(seeded)
        on_stack: set[str] = set()

        def visit(field: str) -> None:
            if field in done:
                return
            if field in on_stack:
                # Inalcanzable vía YAML (el schema rechaza los ciclos al
                # cargar); sólo lo alcanza una MetadataConfig armada a mano.
                raise ConfigurationError("field dependency cycle", field=field)
            on_stack.add(field)
            for dep in self._dependencies.get(field, ()):
                visit(dep)
            on_stack.discard(field)
            done.add(field)
            order.append(field)

        for field in requested:
            visit(field)
        self._order_cache[cache_key] = tuple(order)
        return tuple(order)

    def _resolve_graph(
        self,
        requested: Sequence[str],
        trigger: Trigger,
        document: RVABREPDocument,
        seed: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Resuelve el cierre transitivo de *requested* en orden topológico.

        Una dependencia que no resolvió NO aborta el documento: se queda
        fuera del dict y su dependiente saltea esa fuente igual que un valor
        vacío. Un campo PEDIDO que no resolvió sí propaga su error — es el
        contrato pre-147 y no cambia.

        147 REQ-003: *seed* arranca el dict con lo que S2 ya resolvió.
        """
        wanted = set(requested)
        values: dict[str, str] = dict(seed or {})
        for field in self._resolution_order(requested, frozenset(values)):
            try:
                values[field] = self._resolve_one(field, trigger, document, values)
            except MetadataError:
                if field in wanted:
                    raise
                _logger.debug("dependency field=%s did not resolve; dependents skip it", field)
        return values

    def resolve_fields(
        self,
        canonical_fields: Sequence[str],
        trigger: Trigger,
        document: RVABREPDocument,
    ) -> dict[str, FieldResolution]:
        """147 REQ-002: resuelve campos sueltos (sin ``CMMapping``) y devuelve
        la CADENA intentada junto al valor.

        No levanta por un campo que no resolvió — devuelve ``value=None`` con
        la traza. Quién decide qué hacer con eso es el
        :class:`~cmcourier.services.identity.IdentityResolver`, vía
        ``on_missing``. Los errores de CONFIG (``ConfigurationError``) sí
        propagan: son bugs del YAML, no datos faltantes.
        """
        order = self._resolution_order(canonical_fields)
        values: dict[str, str] = {}
        traces: dict[str, tuple[str, ...]] = {}
        for field in order:
            attempts: list[str] = []
            try:
                values[field] = self._resolve_one(field, trigger, document, values, attempts)
            except MetadataError as exc:
                attempts.append(f"{field} <- (all sources exhausted): {type(exc).__name__}")
            traces[field] = tuple(attempts)
        return {
            field: FieldResolution(
                field=field,
                value=values.get(field),
                chain=self._chain_trace(field, traces),
            )
            for field in canonical_fields
        }

    def _chain_trace(
        self, canonical_field: str, traces: Mapping[str, tuple[str, ...]]
    ) -> tuple[str, ...]:
        """La traza del campo MÁS la de cada salto previo del que dependía,
        en orden de resolución."""
        lines: list[str] = []
        for field in self._resolution_order([canonical_field]):
            lines.extend(traces.get(field, ()))
        return tuple(lines)

    # --- resolución por campo ------------------------------------------

    def _resolve_one(
        self,
        canonical_field: str,
        trigger: Trigger,
        document: RVABREPDocument,
        values: Mapping[str, str],
        attempts: list[str] | None = None,
    ) -> str:
        if canonical_field not in self._config.field_sources:
            raise ConfigurationError(
                "no field_sources config for field",
                field=canonical_field,
            )
        fsc = self._config.field_sources[canonical_field]

        for sc in fsc.sources:
            value = self._try_source(canonical_field, sc, trigger, document, values, attempts)
            if value is not None:
                # 146 (b): el formato por campo es la garantía de salida.
                return apply_format(value, fsc.format)

        # Todas las fuentes fallaron. Intentar el `default` si existe.
        return self._resolve_default(canonical_field, fsc, attempts)

    def _try_source(
        self,
        canonical_field: str,
        sc: SourceConfig,
        trigger: Trigger,
        document: RVABREPDocument,
        values: Mapping[str, str],
        attempts: list[str] | None,
    ) -> str | None:
        """Un paso de la cadena: ``None`` significa "esta fuente no dio, pasar
        a la siguiente"."""
        dep = split_field_reference(sc.lookup_value_source)
        if dep is not None and dep not in values:
            # 147 REQ-001: la dependencia no resolvió ⇒ se saltea SÓLO esta
            # fuente. Ni siquiera se toca la tabla.
            _record(attempts, canonical_field, sc, f"skipped (dependency {dep!r} unresolved)")
            return None
        raw = self._fetch_from_source(canonical_field, sc, trigger, document, values)
        if raw is None:
            _record(attempts, canonical_field, sc, "no value")
            return None
        # 146 (a): el formato por fuente corre ANTES de validar —
        # normaliza la vestimenta para que el patrón pueda ser estricto.
        value = apply_format(raw, sc.format)
        if value == "":
            # Vacío después de formatear = esta fuente no dio (el
            # caso `CHAR(n)` de AS400 todo espacios con `trim`).
            _record(attempts, canonical_field, sc, "empty value")
            return None
        if not _validates(value, sc.validation):
            _logger.debug(
                "validation failed for field=%s source=%s",
                canonical_field,
                sc.source_type,
            )
            _record(attempts, canonical_field, sc, "failed validation")
            return None
        _record(attempts, canonical_field, sc, "resolved")
        return value

    @staticmethod
    def _resolve_default(
        canonical_field: str,
        fsc: FieldSourceConfig,
        attempts: list[str] | None = None,
    ) -> str:
        """El `default` del campo, ya formateado (146).

        El orden es formatear y DESPUÉS validar, y es deliberado: el
        default se valida contra el patrón de la PRIMERA fuente, así que
        un ``default_value: "0"`` con un ``format`` de campo
        ``pad_left: {width: 9}`` llega como ``"000000000"`` y pasa un
        ``^\\d{9}$`` que crudo lo mataría.
        """
        if fsc.default_value is None:
            _logger.warning(
                "all sources failed for field=%s (no default configured)",
                canonical_field,
            )
            if attempts is not None:
                attempts.append(f"{canonical_field} <- default: none configured")
            raise SourceFailedError(field_name=canonical_field, source=_ALL_SOURCES_SENTINEL)
        default = apply_format(fsc.default_value, fsc.format)
        first_validation = fsc.sources[0].validation if fsc.sources else None
        if not _validates(default, first_validation):
            if attempts is not None:
                attempts.append(f"{canonical_field} <- default: failed validation")
            raise DefaultValidationFailedError(
                field_name=canonical_field,
                default_value=default,
            )
        if attempts is not None:
            attempts.append(f"{canonical_field} <- default: used")
        return default

    # --- normalización de nombres de campo -----------------------------

    def _normalize_fields(self, raw_fields: tuple[str, ...]) -> list[str]:
        canonical, _ = self._normalize_fields_with_friendly(raw_fields)
        return canonical

    def _normalize_fields_with_friendly(
        self, raw_fields: tuple[str, ...]
    ) -> tuple[list[str], dict[str, str]]:
        """Igual que :meth:`_normalize_fields`, pero también devuelve
        el mapa inverso ``canonical -> raw_friendly`` (038).

        El nombre `friendly` es el nombre visible al operador tal como
        está escrito en ``MetadatosCM.Metadato`` (preservado
        textualmente, sin strip). El mapa le permite a :meth:`resolve`
        consultar ``cmis_property_ids`` (que se indexa por nombre
        `friendly`) después de que la resolución produjo valores
        indexados por nombre canónico.
        """
        aliases_lower = self._aliases_lower
        canonical: list[str] = []
        canonical_to_friendly: dict[str, str] = {}
        for raw in raw_fields:
            if raw in self._config.field_sources:
                canonical.append(raw)  # ya es canónico
                canonical_to_friendly[raw] = raw
                continue
            canonical_match = aliases_lower.get(raw.lower())
            if canonical_match is not None:
                canonical.append(canonical_match)
                canonical_to_friendly[canonical_match] = raw
                continue
            raise ConfigurationError(
                "unknown field (no alias and no field_sources entry)",
                field=raw,
            )
        return canonical, canonical_to_friendly

    # --- `dispatch` por fuente -----------------------------------------

    def _fetch_from_source(
        self,
        canonical_field: str,
        sc: SourceConfig,
        trigger: Trigger,
        document: RVABREPDocument,
        values: Mapping[str, str],
    ) -> str | None:
        if sc.source_type == "trigger":
            return self._fetch_trigger(sc, trigger)
        if sc.source_type == "rvabrep":
            return self._fetch_rvabrep(sc, document)
        lookup = split_lookup_source_type(sc.source_type)
        if lookup is not None:
            kind, alias = lookup
            return self._fetch_lookup(canonical_field, sc, alias, kind, trigger, document, values)
        raise ConfigurationError("unknown source_type", source_type=sc.source_type)

    def _fetch_trigger(self, sc: SourceConfig, trigger: Trigger) -> str | None:
        """Lee un campo desde el trigger.

        Antes de 046 el trigger siempre tenía ``shortname / cif /
        system_id`` como atributos directos (forma ``ClientTrigger``).
        Post-046 los triggers basados en fila llevan los mismos datos
        dentro de su mapping ``row`` bajo los nombres de columna
        RVABREP configurados. Primero se intenta el path de atributo
        (funciona para ``ClientTrigger`` y nombres de
        ``lookup_value_column`` como ``shortname`` / ``cif``); si el
        trigger no lo expone, se cae a la proyección row + audit.
        """
        # ``ClientTrigger`` tiene shortname/cif/system_id como atributos.
        if hasattr(trigger, sc.lookup_value_column):
            value = getattr(trigger, sc.lookup_value_column)
            return None if value is None else str(value)
        # Los triggers basados en fila mapean ``lookup_value_column`` →
        # proyección ``audit_row`` para shortname/cif/system_id; el
        # resto cae a ``None``.
        audit = trigger.audit_row()
        if sc.lookup_value_column in audit:
            v = audit[sc.lookup_value_column]
            return None if v is None else str(v)
        raise ConfigurationError(
            "trigger source has no attribute",
            attribute=sc.lookup_value_column,
            trigger_kind=type(trigger).__name__,
        )

    def _fetch_rvabrep(self, sc: SourceConfig, document: RVABREPDocument) -> str | None:
        if not hasattr(document, sc.lookup_value_column):
            raise ConfigurationError(
                "RVABREPDocument has no attribute",
                attribute=sc.lookup_value_column,
            )
        value = getattr(document, sc.lookup_value_column)
        return None if value is None else str(value)

    def _fetch_lookup(
        self,
        canonical_field: str,
        sc: SourceConfig,
        alias: str,
        kind_label: str,
        trigger: Trigger,
        document: RVABREPDocument,
        values: Mapping[str, str],
    ) -> str | None:
        """084: path unificado de lookup contra CSV o AS400.

        Pre-084 había solo ``_fetch_csv`` con CIF hardcoded. Ahora el
        valor de lookup sale de ``sc.lookup_value_source`` (default
        ``"trigger.cif"`` para backward-compat). AS400 hereda el mismo
        contrato — el adapter ``As400DataSource`` ya implementa
        ``IDataSource`` (``get_all`` + ``get_by_fields``).

        147 REQ-005: el resultado (hit Y miss) se memoiza por corrida."""
        if alias not in self._sources_registry:
            raise ConfigurationError(
                f"unknown {kind_label} alias at resolution time",
                alias=alias,
            )
        key_column = sc.lookup_key_column
        if key_column is None:
            raise ConfigurationError(
                f"{kind_label} source requires lookup_key_column",
                source_type=sc.source_type,
            )
        lookup_value = self._resolve_lookup_value(sc.lookup_value_source, trigger, document, values)
        if lookup_value is None:
            return None
        # 147 REQ-005: la clave lleva las coordenadas COMPLETAS de la fuente,
        # no sólo (campo, valor de clave): dos fuentes de lookup distintas del
        # mismo campo pueden compartir el valor de clave y devolver cosas
        # distintas.
        memo_key = (
            canonical_field,
            sc.source_type,
            key_column,
            sc.lookup_value_column,
            lookup_value,
        )
        if memo_key in self._lookup_memo:
            self._memo_hits += 1
            return self._lookup_memo[memo_key]
        found = self._lookup_uncached(sc, alias, key_column, lookup_value)
        self._lookup_memo[memo_key] = found
        return found

    def _lookup_uncached(
        self, sc: SourceConfig, alias: str, key_column: str, lookup_value: str
    ) -> str | None:
        """El salto real contra la fuente: índice del `prefetch` si está
        prendido, ``get_by_fields`` si no."""
        if self._config.prefetch_enabled:
            cache_key = (alias, key_column, lookup_value, sc.lookup_value_column)
            return self._csv_cache.get(cache_key)
        rows = self._sources_registry[alias].get_by_fields({key_column: lookup_value})
        if not rows:
            return None
        raw = rows[0].get(sc.lookup_value_column)
        return None if raw is None else str(raw)

    def _resolve_lookup_value(
        self,
        spec: str,
        trigger: Trigger,
        document: RVABREPDocument,
        values: Mapping[str, str],
    ) -> str | None:
        """084 + 147: parsea ``lookup_value_source`` y devuelve el valor a
        buscar en la fuente de lookup.

        Sintaxis: ``"<scope>.<attr>"`` con scope ``trigger``, ``rvabrep`` o
        ``field`` (147 REQ-001). El scope ``field`` lee de *values*, el dict
        de lo YA RESUELTO en esta pasada — es lo que permite encadenar
        saltos. Reemplaza al ``cif_override`` pre-147, que era un único valor
        suelto y sólo servía para ``trigger.cif``.
        """
        if "." not in spec:
            raise ConfigurationError(
                "lookup_value_source must be of the form '<scope>.<attr>' "
                "(e.g. 'trigger.cif', 'rvabrep.txn_num' or 'field.BAC_CIF')",
                lookup_value_source=spec,
            )
        scope, attr = spec.split(".", 1)
        if scope == "field":
            # Ausente = la dependencia no resolvió: esta fuente se saltea.
            return values.get(attr)
        if scope == "trigger":
            if attr == "cif":
                return _trigger_cif(trigger)
            if hasattr(trigger, attr):
                value = getattr(trigger, attr)
                return None if value is None else str(value)
            audit = trigger.audit_row()
            if attr in audit:
                v = audit[attr]
                return None if v is None else str(v)
            raise ConfigurationError(
                "trigger has no such attribute for lookup_value_source",
                attribute=attr,
                trigger_kind=type(trigger).__name__,
            )
        if scope == "rvabrep":
            if not hasattr(document, attr):
                raise ConfigurationError(
                    "RVABREPDocument has no such attribute for lookup_value_source",
                    attribute=attr,
                )
            value = getattr(document, attr)
            return None if value is None else str(value)
        raise ConfigurationError(
            "unknown scope in lookup_value_source (expected 'trigger', 'rvabrep' or 'field')",
            scope=scope,
        )
