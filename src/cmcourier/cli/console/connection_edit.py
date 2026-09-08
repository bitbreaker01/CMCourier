"""Editor de conexiones con alias — la parte pura (138 REQ-001).

Sin Textual. Traduce lo que el operador tipea en el modal de [2]
(strings crudos) a lo que va al YAML (tipado, sin defaults redundantes)
y arma los ``Edit`` que ``YamlDocument`` (137) aplica: alta / edición
de un alias del registro ``connections:``, apuntar sitios al alias,
baja bloqueada si algún sitio lo usa, y "mover al registro" una conexión
inline. La validación REAL sigue siendo pydantic al recargar el tmp
(``write(verify=load_config)``): acá sólo damos mensajes por campo ANTES
de escribir.
"""

from __future__ import annotations

__all__ = [
    "KIND_FIELDS",
    "ConnectionDraft",
    "DeletePlan",
    "DraftErrors",
    "Site",
    "connection_sites",
    "draft_to_yaml",
    "field_default",
    "inline_connection",
    "plan_delete",
    "plan_move_inline",
    "plan_write",
    "prefill_fields",
    "validate_alias",
    "validate_draft",
    "value_text",
    "write_error_text",
]

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from pydantic_core import PydanticUndefined

from cmcourier.config.schema import (
    _CONNECTION_ALIAS_RE,
    INLINE_CONNECTION_ALIAS,
    RESERVED_CONNECTION_ALIASES,
    As400ConnectionConfig,
    As400MetadataSourceConfig,
    As400RvabrepSource,
    ConnectionKind,
    MssqlConnectionConfig,
    MssqlMetadataSourceConfig,
    PipelineConfig,
)
from cmcourier.config.yaml_doc import DELETE, Edit, YamlPath
from cmcourier.domain.exceptions import CMCourierError

_PYDANTIC_PREFIX: Final = "Value error, "

# Campos editables por kind: los del modelo menos ``kind`` (en orden del modelo).
_MODELS: Final[dict[str, type[As400ConnectionConfig] | type[MssqlConnectionConfig]]] = {
    "as400": As400ConnectionConfig,
    "mssql": MssqlConnectionConfig,
}
KIND_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    kind: tuple(name for name in model.model_fields if name != "kind")
    for kind, model in _MODELS.items()
}
_INT_FIELDS: Final = frozenset({"port"})
_BOOL_FIELDS: Final = frozenset({"encrypt", "trust_server_certificate"})
_BOOL_WORDS: Final[dict[str, bool]] = {
    "true": True,
    "false": False,
    "sí": True,
    "si": True,
    "no": False,
    "1": True,
    "0": False,
}

# campo → mensaje corto; ``{}`` = borrador válido.
DraftErrors = dict[str, str]


@dataclass(frozen=True, slots=True)
class ConnectionDraft:
    """Lo que hay en los inputs del modal: valores CRUDOS, todavía strings."""

    alias: str
    kind: ConnectionKind
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Site:
    """Un lugar de la config que acepta un alias de conexión.

    ``current`` es el alias que hoy referencia el sitio; ``None`` si la
    conexión es inline (o no está).
    """

    path: YamlPath
    label: str
    kind: str
    current: str | None


@dataclass(frozen=True, slots=True)
class DeletePlan:
    """Resultado de ``plan_delete``: o las ediciones, o los sitios que bloquean."""

    edits: list[Edit]
    blocked_by: list[Site]

    @property
    def ok(self) -> bool:
        return not self.blocked_by


def field_default(kind: str, name: str) -> str:
    """El default del modelo como texto para el placeholder (``""`` si no hay)."""
    default = _MODELS[kind].model_fields[name].default
    if default is None or default is PydanticUndefined:
        return ""
    return value_text(default)


def value_text(value: object) -> str:
    """Un valor del YAML / modelo como lo mostraría un Input (bool → ``true``/``false``)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def prefill_fields(kind: str, raw: Mapping[str, object]) -> dict[str, str]:
    """Los campos de la kind presentes en ``raw`` (YAML crudo o ``model_dump``) como texto."""
    return {
        name: value_text(raw[name])
        for name in KIND_FIELDS[kind]
        if name in raw and raw[name] is not None
    }


# ------------------------------------------------------------ validación


def validate_alias(alias: str, *, existing: set[str], editing: str | None) -> str | None:
    """Mensaje de error del alias o ``None`` si es válido."""
    if not alias:
        return "requerido"
    if not _CONNECTION_ALIAS_RE.fullmatch(alias):
        return "sólo minúsculas, dígitos y _ (empieza con letra, máx. 32)"
    if alias in RESERVED_CONNECTION_ALIASES or alias == INLINE_CONNECTION_ALIAS:
        return "reservado"
    if alias in existing and alias != editing:
        return "ya existe"
    return None


def validate_draft(
    draft: ConnectionDraft, *, existing: set[str], editing: str | None
) -> DraftErrors:
    """Errores por campo ANTES de escribir; pydantic valida de verdad al recargar."""
    errors: DraftErrors = {}
    alias_error = validate_alias(draft.alias.strip(), existing=existing, editing=editing)
    if alias_error:
        errors["alias"] = alias_error
    values = {name: draft.fields.get(name, "").strip() for name in KIND_FIELDS[draft.kind]}
    if not values.get("host"):
        errors["host"] = "requerido"
    if draft.kind == "mssql" and not values.get("database"):
        errors["database"] = "requerido"
    for name, raw in values.items():
        if not raw:
            continue  # vacío → se omite y aplica el default del modelo
        if name in _INT_FIELDS and not (raw.isdigit() and 1 <= int(raw) <= 65535):
            errors[name] = f"{name}: 1..65535"
        elif name in _BOOL_FIELDS and raw.lower() not in _BOOL_WORDS:
            errors[name] = "true/false/sí/no/1/0"
    return errors


def draft_to_yaml(draft: ConnectionDraft) -> dict[str, object]:
    """El mapping que va al YAML: ``kind`` + campos no vacíos ya tipados."""
    out: dict[str, object] = {"kind": draft.kind}
    for name in KIND_FIELDS[draft.kind]:
        raw = draft.fields.get(name, "").strip()
        if not raw:
            continue
        if name in _INT_FIELDS:
            out[name] = int(raw)
        elif name in _BOOL_FIELDS:
            out[name] = _BOOL_WORDS[raw.lower()]
        else:
            out[name] = raw
    return out


def write_error_text(exc: BaseException) -> str:
    """Texto corto para el toast cuando ``write(verify=load_config)`` falla.

    ``ConfigurationError`` guarda los errores de pydantic en ``context``;
    su ``str`` vuelca la config ENTERA (``input``), inservible en un
    toast. Se busca por la cadena de causas y se quedan sólo los ``msg``.
    """
    cause: BaseException | None = exc
    while cause is not None:
        errors = cause.context.get("errors") if isinstance(cause, CMCourierError) else None
        if isinstance(errors, list) and errors:
            msgs = (str(e.get("msg", e)) for e in errors if isinstance(e, dict))
            return "; ".join(m.removeprefix(_PYDANTIC_PREFIX) for m in msgs) or str(exc)
        cause = cause.__cause__
    return str(exc)


# ------------------------------------------------------------ sitios


def _current(value: object) -> str | None:
    return value if isinstance(value, str) else None


def connection_sites(config: PipelineConfig) -> list[Site]:
    """Los sitios de la config que aceptan un alias, en orden de aparición."""
    sites: list[Site] = []
    source = config.indexing.source
    if isinstance(source, As400RvabrepSource):
        sites.append(
            Site(
                ("indexing", "source", "connection"),
                "indexing.source",
                "as400",
                _current(source.connection),
            )
        )
    for i, meta in enumerate(config.metadata.sources):
        label = f"metadata.sources[{i}] {meta.alias}"
        if isinstance(meta, As400MetadataSourceConfig):
            path: YamlPath = ("metadata", "sources", i, "as400_connection")
            sites.append(Site(path, label, "as400", _current(meta.as400_connection)))
        elif isinstance(meta, MssqlMetadataSourceConfig):
            path = ("metadata", "sources", i, "connection")
            sites.append(Site(path, label, "mssql", meta.connection))
    sync = config.tracking.as400_sync
    if sync.enabled:
        sites.append(
            Site(
                ("tracking", "as400_sync", "connection"),
                "tracking.as400_sync",
                "as400",
                _current(sync.connection),
            )
        )
    return sites


# ------------------------------------------------------------ planes


def _field_edits(draft: ConnectionDraft, alias: str) -> list[Edit]:
    """Edición campo por campo (E3): asigna los tipeados y borra los vacíos.

    Reemplazar el mapping entero perdería el estilo (un ``rvi: {…}`` en
    flow style pasaría a bloque) y los comentarios adentro.
    """
    payload = draft_to_yaml(draft)
    return [
        Edit(("connections", alias, name), payload.get(name, DELETE))
        for name in KIND_FIELDS[draft.kind]
    ]


def plan_write(
    draft: ConnectionDraft,
    config: PipelineConfig,
    *,
    use_at: set[YamlPath],
    editing: str | None = None,
) -> list[Edit]:
    """Alta/edición del alias + apuntar cada sitio de ``use_at`` a él.

    Alta: ``("connections", alias)`` ← ``draft_to_yaml``. Edición
    (``editing`` = el alias): un ``Edit`` por campo. Un sitio de kind
    distinta (o desconocido) es un ``ValueError`` — la UI no debería
    dejarlo pasar, pero el disco no se toca (E8).
    """
    alias = draft.alias.strip()
    if editing is not None:
        edits = _field_edits(draft, alias)
    else:
        edits = [Edit(("connections", alias), draft_to_yaml(draft))]
    by_path = {site.path: site for site in connection_sites(config)}
    for path in sorted(use_at, key=str):
        site = by_path.get(path)
        if site is None:
            raise ValueError(f"sitio desconocido: {'.'.join(map(str, path))}")
        if site.kind != draft.kind:
            raise ValueError(
                f"{site.label} necesita una conexión {site.kind}, no {draft.kind} ({alias})"
            )
        edits.append(Edit(site.path, alias))
    return edits


def plan_delete(alias: str, config: PipelineConfig) -> DeletePlan:
    """Baja del alias — bloqueada si algún sitio lo referencia."""
    blocked = [site for site in connection_sites(config) if site.current == alias]
    if blocked:
        return DeletePlan(edits=[], blocked_by=blocked)
    return DeletePlan(edits=[Edit(("connections", alias), DELETE)], blocked_by=[])


def inline_connection(site: Site, config: PipelineConfig) -> As400ConnectionConfig:
    """El modelo inline que vive en ``site`` (``ValueError`` si el sitio usa un alias)."""
    node: object = config
    for step in site.path:
        node = node[step] if isinstance(step, int) else getattr(node, step)  # type: ignore[index]
    if not isinstance(node, As400ConnectionConfig):
        raise ValueError(f"{site.label} no tiene una conexión inline (usa {site.current!r})")
    return node


def plan_move_inline(site: Site, alias: str, config: PipelineConfig) -> list[Edit]:
    """La conexión inline de ``site`` pasa al registro como ``alias`` y el
    sitio la referencia por nombre. ``alias`` se valida como en el borrador."""
    error = validate_alias(alias, existing=set(config.connections), editing=None)
    if error:
        raise ValueError(f"alias: {error}")
    payload = inline_connection(site, config).model_dump(mode="json", exclude_none=True)
    return [Edit(("connections", alias), payload), Edit(site.path, alias)]
