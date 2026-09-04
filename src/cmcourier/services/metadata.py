"""Servicio de resolución de metadata.

Cadena de fallback de fuente por campo, con regexes de validación,
fallback a valor por defecto, self-healing de CIF, normalización
de aliases de campos y pre-fetching ansioso de fuentes ``csv:<alias>``
al construirse. El stage S3 de cada `pipeline` depende de este
servicio.

Principio I de la Constitución: importa solo ``cmcourier.domain.*`` y
stdlib. Principio VIII: nunca loguear VALORES resueltos de campos
(PII); loguear solo NOMBRES de campos.
"""

from __future__ import annotations

__all__ = [
    "FieldSourceConfig",
    "MetadataConfig",
    "MetadataResolution",
    "MetadataService",
    "SourceConfig",
    "ValidationConfig",
]

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from cmcourier.domain.exceptions import (
    ConfigurationError,
    DefaultValidationFailedError,
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

_CSV_PREFIX = "csv:"
_AS400_PREFIX = "as400:"


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
    """

    source_type: str
    lookup_value_column: str
    lookup_key_column: str | None = None
    validation: ValidationConfig | None = None
    lookup_value_source: str = "trigger.cif"


@dataclass(frozen=True, slots=True)
class FieldSourceConfig:
    """Cadena de fallback completa más el `default` para un campo
    canónico (``BAC_*``)."""

    sources: tuple[SourceConfig, ...]
    default_value: str | None = None


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
    healed_trigger: Trigger
    # 046: el CIF resuelto, capturado explícitamente para que el
    # `document_cache` pueda persistirlo sin inspeccionar el subtipo
    # de trigger.
    healed_cif: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validates(value: str, validation: ValidationConfig | None) -> bool:
    """Devuelve ``True`` si ``value`` pasa la validación (validación
    ``None`` = siempre ``True``)."""
    if validation is None or validation.allowed_pattern is None:
        return True
    return re.fullmatch(validation.allowed_pattern, value) is not None


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------


class MetadataService:
    """Resolución de metadata por campo con fallback y self-healing
    de CIF.

    Ver ``specs/005-metadata-service/{spec,plan}.md`` para el contexto
    completo.
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
        if config.prefetch_enabled:
            self._prefetch_csv_sources()

    # --- construcción --------------------------------------------------

    def _prefetch_csv_sources(self) -> None:
        """084: prefetch para CSV **y** AS400. El nombre se conserva
        por backward-compat — internamente cubre ambos tipos de lookup
        source con el mismo contrato (``get_all()`` + indexado en
        memoria por ``key_column``)."""
        seen_pairs: set[tuple[str, str, str]] = set()
        for fsc in self._config.field_sources.values():
            for sc in fsc.sources:
                if sc.source_type.startswith(_CSV_PREFIX):
                    alias = sc.source_type[len(_CSV_PREFIX) :]
                    prefix_label = "csv"
                elif sc.source_type.startswith(_AS400_PREFIX):
                    alias = sc.source_type[len(_AS400_PREFIX) :]
                    prefix_label = "as400"
                else:
                    continue
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
    ) -> MetadataResolution:
        """Resuelve cada campo requerido de metadata."""
        canonical_fields, canonical_to_friendly = self._normalize_fields_with_friendly(
            mapping.required_metadata_fields
        )
        resolved: dict[str, str] = {}

        # 046: el trigger es polimórfico. ``_trigger_cif`` extrae el
        # CIF del atributo que use cada subtipo (``ClientTrigger.cif``
        # o ``row[col_cif]`` para los subtipos basados en fila).
        current_cif = _trigger_cif(trigger)

        # Self-healing de CIF PRIMERO, para que los lookups de CSV
        # subsiguientes puedan usar el valor resuelto.
        if current_cif is None and "BAC_CIF" in canonical_fields:
            cif_value = self._resolve_one("BAC_CIF", trigger, document, cif_override=current_cif)
            current_cif = cif_value
            resolved["BAC_CIF"] = cif_value

        for f in canonical_fields:
            if f in resolved:
                continue
            resolved[f] = self._resolve_one(f, trigger, document, cif_override=current_cif)

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

    # --- resolución por campo ------------------------------------------

    def _resolve_one(
        self,
        canonical_field: str,
        trigger: Trigger,
        document: RVABREPDocument,
        *,
        cif_override: str | None = None,
    ) -> str:
        if canonical_field not in self._config.field_sources:
            raise ConfigurationError(
                "no field_sources config for field",
                field=canonical_field,
            )
        fsc = self._config.field_sources[canonical_field]
        first_validation = fsc.sources[0].validation if fsc.sources else None

        for sc in fsc.sources:
            value = self._fetch_from_source(sc, trigger, document, cif_override=cif_override)
            if value is None or value == "":
                continue
            if not _validates(value, sc.validation):
                _logger.debug(
                    "validation failed for field=%s source=%s",
                    canonical_field,
                    sc.source_type,
                )
                continue
            return value

        # Todas las fuentes fallaron. Intentar el `default` si existe.
        if fsc.default_value is None:
            _logger.warning(
                "all sources failed for field=%s (no default configured)",
                canonical_field,
            )
            raise SourceFailedError(field_name=canonical_field, source=_ALL_SOURCES_SENTINEL)
        if not _validates(fsc.default_value, first_validation):
            raise DefaultValidationFailedError(
                field_name=canonical_field,
                default_value=fsc.default_value,
            )
        return fsc.default_value

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
        sc: SourceConfig,
        trigger: Trigger,
        document: RVABREPDocument,
        *,
        cif_override: str | None = None,
    ) -> str | None:
        if sc.source_type == "trigger":
            return self._fetch_trigger(sc, trigger)
        if sc.source_type == "rvabrep":
            return self._fetch_rvabrep(sc, document)
        if sc.source_type.startswith(_CSV_PREFIX):
            alias = sc.source_type[len(_CSV_PREFIX) :]
            return self._fetch_lookup(
                sc, alias, "csv", trigger, document, cif_override=cif_override
            )
        if sc.source_type.startswith(_AS400_PREFIX):
            alias = sc.source_type[len(_AS400_PREFIX) :]
            return self._fetch_lookup(
                sc, alias, "as400", trigger, document, cif_override=cif_override
            )
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
        sc: SourceConfig,
        alias: str,
        kind_label: str,
        trigger: Trigger,
        document: RVABREPDocument,
        *,
        cif_override: str | None = None,
    ) -> str | None:
        """084: path unificado de lookup contra CSV o AS400.

        Pre-084 había solo ``_fetch_csv`` con CIF hardcoded. Ahora el
        valor de lookup sale de ``sc.lookup_value_source`` (default
        ``"trigger.cif"`` para backward-compat). AS400 hereda el mismo
        contrato — el adapter ``As400DataSource`` ya implementa
        ``IDataSource`` (``get_all`` + ``get_by_fields``)."""
        if alias not in self._sources_registry:
            raise ConfigurationError(
                f"unknown {kind_label} alias at resolution time",
                alias=alias,
            )
        if sc.lookup_key_column is None:
            raise ConfigurationError(
                f"{kind_label} source requires lookup_key_column",
                source_type=sc.source_type,
            )
        lookup_value = self._resolve_lookup_value(
            sc.lookup_value_source, trigger, document, cif_override=cif_override
        )
        if lookup_value is None:
            return None
        if self._config.prefetch_enabled:
            cache_key = (alias, sc.lookup_key_column, lookup_value, sc.lookup_value_column)
            return self._csv_cache.get(cache_key)
        rows = self._sources_registry[alias].get_by_fields({sc.lookup_key_column: lookup_value})
        if not rows:
            return None
        raw = rows[0].get(sc.lookup_value_column)
        return None if raw is None else str(raw)

    def _resolve_lookup_value(
        self,
        spec: str,
        trigger: Trigger,
        document: RVABREPDocument,
        *,
        cif_override: str | None = None,
    ) -> str | None:
        """084: parsea ``lookup_value_source`` y devuelve el valor a
        buscar en la fuente de lookup.

        Sintaxis: ``"<scope>.<attr>"`` donde scope es ``trigger`` o
        ``rvabrep``. Caso especial ``"trigger.cif"`` honra el CIF
        self-healed (``cif_override``) para preservar la lógica
        pre-084.
        """
        if "." not in spec:
            raise ConfigurationError(
                "lookup_value_source must be of the form '<scope>.<attr>' "
                "(e.g. 'trigger.cif' or 'rvabrep.txn_num')",
                lookup_value_source=spec,
            )
        scope, attr = spec.split(".", 1)
        if scope == "trigger":
            if attr == "cif":
                return cif_override if cif_override is not None else _trigger_cif(trigger)
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
            "unknown scope in lookup_value_source (expected 'trigger' or 'rvabrep')",
            scope=scope,
        )
