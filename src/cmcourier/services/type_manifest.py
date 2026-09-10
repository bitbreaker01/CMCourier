"""Lógica pura del manifest de tipos CM (145 REQ-002).

Derivar, decidir, diffear y mergear. Cero red y cero disco: entra la
respuesta de ``typeDescendants`` ya deserializada (o un manifest que
alguien leyó del JSON) y sale un :class:`CmTypeManifest` nuevo. El que
habla con el servidor es :mod:`cmcourier.services.type_discovery`; el
que toca el disco es :mod:`cmcourier.adapters.manifest.json_store`.

Todo es inmutable: cada operación devuelve un manifest nuevo vía
``dataclasses.replace``. Nunca se muta lo que entró.
"""

from __future__ import annotations

__all__ = [
    "ManifestDiff",
    "apply_diff",
    "build_entry_from_type",
    "build_manifest",
    "diff_manifest",
    "flatten_types",
    "mark_reviewed",
    "set_decision",
    "set_folder",
]

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from cmcourier.domain.cm_types import (
    DECISION_OMIT,
    DECISION_USE,
    FOLDER_DERIVED,
    FOLDER_MANUAL,
    CmPropertyDef,
    CmTypeEntry,
    CmTypeManifest,
    canonical_name,
    derive_folder,
)
from cmcourier.domain.exceptions import ConfigurationError

#: Las propiedades que pone el código en cada upload — no las decide el operador.
_CODE_OWNED = frozenset({"cmis:name", "cmis:objectTypeId"})
#: Sólo estas dos `updatability` se pueden escribir al crear un documento.
_WRITABLE = frozenset({"readwrite", "oncreate"})
#: Nombre canónico de la propiedad que lleva el ID corto de la clase.
_ID_CORTO_PROP = "BAC_ID_Corto"
#: Fallback: ``"PT95 - Escritura Hipotecaria"`` → ``PT95``.
_DISPLAY_PREFIX = re.compile(r"^(\S+) - ")
#: Campos de una propiedad que el diff vigila (en este orden).
_WATCHED = (
    "required",
    "default_value",
    "max_length",
    "property_type",
    "updatability",
    "cardinality",
)
_EMPTY_CHANGED: Mapping[str, tuple[str, ...]] = MappingProxyType({})


# ---------------------------------------------------------------------------
# Parseo de la respuesta del servidor
# ---------------------------------------------------------------------------


def flatten_types(nodes: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """Aplana el árbol de ``typeDescendants`` a una tupla de `typeDefinition`s.

    Cada nodo es ``{"type": {...}, "children": [...]}``; ``children``
    puede faltar o venir en ``null``. Un nodo que ya sea la definición
    (trae ``id`` y no ``type``) se acepta tal cual — algunos servers
    devuelven la lista plana.
    """
    out: list[Mapping[str, Any]] = []
    for node in nodes:
        type_def = node.get("type") if "type" in node else node
        if isinstance(type_def, Mapping):
            out.append(type_def)
        children = node.get("children") or ()
        if isinstance(children, Iterable) and not isinstance(children, str | bytes | Mapping):
            out.extend(flatten_types(children))
    return tuple(out)


def _default_value(raw: Any) -> str | None:
    """Normaliza el ``defaultValue`` del server a string (o ``None``).

    Viene escalar o como lista según la cardinalidad; ausente, lista
    vacía y string vacío son todos "no hay default"."""
    if isinstance(raw, list):
        if not raw:
            return None
        return ", ".join(str(v) for v in raw) or None
    if raw is None:
        return None
    return str(raw) or None


def _choices(raw: Any) -> tuple[str, ...]:
    """Aplana ``choices`` a la tupla de sus ``value``."""
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for choice in raw:
        if isinstance(choice, Mapping):
            value = choice.get("value", choice.get("displayName"))
            if isinstance(value, list):
                value = value[0] if value else None
            if value is not None:
                out.append(str(value))
        else:
            out.append(str(choice))
    return tuple(out)


def _max_length(raw: Any) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _property_def(raw: Mapping[str, Any]) -> CmPropertyDef:
    """Traduce una `propertyDefinition` `cmis` a nuestro modelo."""
    return CmPropertyDef(
        id=str(raw.get("id", "")),
        display_name=str(raw.get("displayName") or ""),
        property_type=str(raw.get("propertyType") or ""),
        cardinality=str(raw.get("cardinality") or ""),
        updatability=str(raw.get("updatability") or ""),
        required=bool(raw.get("required", False)),
        max_length=_max_length(raw.get("maxLength")),
        default_value=_default_value(raw.get("defaultValue")),
        inherited=bool(raw.get("inherited", False)),
        choices=_choices(raw.get("choices")),
    )


def _suggest(prop: CmPropertyDef) -> str:
    """Decisión sugerida: requerida y sin default en el server ⇒ la mandamos."""
    return DECISION_USE if prop.required and prop.default_value is None else DECISION_OMIT


def is_eligible(type_def: Mapping[str, Any]) -> bool:
    """``True`` sii el tipo es una clase documental creable (145 REQ-002)."""
    return bool(type_def.get("creatable")) and type_def.get("baseId") == "cmis:document"


def _id_corto(type_def: Mapping[str, Any]) -> str | None:
    """ID corto del tipo: default de ``BAC_ID_Corto``, o prefijo del displayName."""
    props = type_def.get("propertyDefinitions") or {}
    if isinstance(props, Mapping):
        for pid, raw in props.items():
            if canonical_name(str(pid)) != _ID_CORTO_PROP or not isinstance(raw, Mapping):
                continue
            value = _default_value(raw.get("defaultValue"))
            if value:
                return value.split(",")[0].strip()
    match = _DISPLAY_PREFIX.match(str(type_def.get("displayName") or ""))
    return match.group(1) if match else None


def build_entry_from_type(type_def: Mapping[str, Any]) -> CmTypeEntry | None:
    """Arma la entrada del manifest para UN `typeDefinition` del server.

    Devuelve ``None`` cuando el tipo no aplica: no es creable, su base no
    es ``cmis:document``, o no tiene ID corto (esos últimos los junta el
    caller en ``without_code``).
    """
    if not is_eligible(type_def):
        return None
    id_corto = _id_corto(type_def)
    if not id_corto:
        return None
    raw_props = type_def.get("propertyDefinitions") or {}
    props: list[CmPropertyDef] = []
    if isinstance(raw_props, Mapping):
        for pid, raw in raw_props.items():
            if str(pid) in _CODE_OWNED or not isinstance(raw, Mapping):
                continue
            if str(raw.get("updatability") or "") not in _WRITABLE:
                continue
            props.append(_property_def({**raw, "id": raw.get("id", pid)}))
    local_name = str(type_def.get("localName") or "")
    return CmTypeEntry(
        id_corto=id_corto,
        type_id=str(type_def.get("id") or ""),
        local_name=local_name,
        display_name=str(type_def.get("displayName") or ""),
        folder=derive_folder(local_name),
        folder_source=FOLDER_DERIVED,
        folder_ok=None,
        properties=tuple(props),
        decisions={p.id: _suggest(p) for p in props},
        reviewed=False,
        changes=(),
        missing_on_server=False,
    )


def build_manifest(
    types: Iterable[Mapping[str, Any]],
    *,
    service_url: str,
    repository_id: str,
    discovered_at: str,
    on_type: Callable[[int, int], None] | None = None,
) -> CmTypeManifest:
    """Arma el manifest completo a partir del árbol de ``typeDescendants``.

    ``on_type(done, total)`` se llama después de procesar cada tipo — lo
    usa :mod:`cmcourier.services.type_discovery` para el progreso.
    Dos tipos con el mismo ID corto son un error de configuración del
    servidor: no hay forma de decidir cuál gana.
    """
    flat = flatten_types(types)
    total = len(flat)
    entries: dict[str, CmTypeEntry] = {}
    without_code: list[tuple[str, str]] = []
    for done, type_def in enumerate(flat, start=1):
        entry = build_entry_from_type(type_def)
        if entry is None:
            if is_eligible(type_def):
                without_code.append(
                    (str(type_def.get("id") or ""), str(type_def.get("displayName") or ""))
                )
        elif entry.id_corto in entries:
            raise ConfigurationError(
                "dos tipos CM comparten el mismo ID corto",
                id_corto=entry.id_corto,
                type_ids=(entries[entry.id_corto].type_id, entry.type_id),
            )
        else:
            entries[entry.id_corto] = entry
        if on_type is not None:
            on_type(done, total)
    return CmTypeManifest(
        service_url=service_url,
        repository_id=repository_id,
        discovered_at=discovered_at,
        types=entries,
        without_code=tuple(without_code),
    )


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    """Lo que cambió entre el manifest local y lo que hoy dice el servidor.

    ``changed`` mapea ID corto → líneas legibles (``+`` agregada, ``-``
    quitada, ``~`` modificada)."""

    new_types: tuple[str, ...] = ()
    missing_types: tuple[str, ...] = ()
    changed: Mapping[str, tuple[str, ...]] = field(default=_EMPTY_CHANGED)

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed", MappingProxyType(dict(self.changed)))

    @property
    def is_empty(self) -> bool:
        """``True`` sii el manifest local está al día con el servidor."""
        return not (self.new_types or self.missing_types or self.changed)

    def render(self) -> str:
        """Texto para el operador — lo que imprime ``types diff``."""
        if self.is_empty:
            return "El manifest está al día con el servidor."
        lines: list[str] = []
        if self.new_types:
            lines.append(f"Tipos nuevos en el servidor ({len(self.new_types)}):")
            lines.extend(f"  + {code}" for code in self.new_types)
        if self.missing_types:
            lines.append(f"Tipos que el servidor ya no publica ({len(self.missing_types)}):")
            lines.extend(f"  - {code}" for code in self.missing_types)
        if self.changed:
            lines.append(f"Tipos con cambios ({len(self.changed)}):")
            for code in sorted(self.changed):
                lines.append(f"  {code}:")
                lines.extend(f"    {line}" for line in self.changed[code])
        return "\n".join(lines)


def _fmt(value: object) -> str:
    return "None" if value is None else str(value)


def _prop_changes(local: CmTypeEntry, live: CmTypeEntry) -> list[str]:
    """Líneas ``+``/``~``/``-`` de las propiedades de un tipo."""
    before = {p.id: p for p in local.properties}
    lines: list[str] = []
    for prop in live.properties:
        old = before.get(prop.id)
        if old is None:
            suffix = " (required)" if prop.required else ""
            lines.append(f"+ {prop.id}{suffix}")
            continue
        for campo in _WATCHED:
            was, now = getattr(old, campo), getattr(prop, campo)
            if was != now:
                lines.append(f"~ {prop.id}: {campo} {_fmt(was)} → {_fmt(now)}")
    after = {p.id for p in live.properties}
    lines.extend(f"- {p.id}" for p in local.properties if p.id not in after)
    return lines


def _type_changes(local: CmTypeEntry, live: CmTypeEntry) -> tuple[str, ...]:
    """Todas las líneas de cambio de un tipo presente en ambos lados."""
    lines: list[str] = []
    if local.type_id != live.type_id:
        lines.append(f"~ type_id: {local.type_id} → {live.type_id}")
    if local.local_name != live.local_name:
        lines.append(f"~ local_name: {local.local_name} → {live.local_name}")
    if local.missing_on_server:
        lines.append("~ vuelve a existir en el servidor")
    lines.extend(_prop_changes(local, live))
    return tuple(lines)


def diff_manifest(local: CmTypeManifest, live: CmTypeManifest) -> ManifestDiff:
    """Compara el manifest local contra la foto fresca del servidor."""
    changed = {
        code: lines
        for code, entry in local.types.items()
        if code in live.types and (lines := _type_changes(entry, live.types[code]))
    }
    return ManifestDiff(
        new_types=tuple(sorted(c for c in live.types if c not in local.types)),
        missing_types=tuple(sorted(c for c in local.types if c not in live.types)),
        changed=changed,
    )


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _merge_entry(local: CmTypeEntry, live: CmTypeEntry, changes: tuple[str, ...]) -> CmTypeEntry:
    """Aplica las propiedades del servidor sobre la entrada local.

    Conserva la decisión de las propiedades que sobreviven, sugiere las
    de las nuevas, respeta la carpeta manual y recalcula la derivada.
    Todo tipo tocado vuelve a ``reviewed=False``."""
    decisions = {
        p.id: local.decisions.get(p.id) or live.decisions.get(p.id) or _suggest(p)
        for p in live.properties
    }
    manual = local.folder_source == FOLDER_MANUAL
    folder = local.folder if manual else derive_folder(live.local_name)
    return replace(
        local,
        type_id=live.type_id,
        local_name=live.local_name,
        display_name=live.display_name,
        folder=folder,
        folder_source=FOLDER_MANUAL if manual else FOLDER_DERIVED,
        folder_ok=local.folder_ok if folder == local.folder else None,
        properties=live.properties,
        decisions=decisions,
        reviewed=False,
        changes=changes,
        missing_on_server=False,
    )


def apply_diff(
    local: CmTypeManifest, live: CmTypeManifest, only: set[str] | None = None
) -> CmTypeManifest:
    """Mergea la foto del servidor sobre el manifest local (145 REQ-002).

    ``only`` acota qué IDs cortos se tocan; el resto queda igual, byte
    por byte. Los tipos que el servidor ya no publica NO se borran: se
    marcan ``missing_on_server``."""
    merged: dict[str, CmTypeEntry] = {}
    for code, entry in local.types.items():
        if only is not None and code not in only:
            merged[code] = entry
            continue
        live_entry = live.types.get(code)
        if live_entry is None:
            merged[code] = replace(entry, missing_on_server=True)
            continue
        changes = _type_changes(entry, live_entry)
        merged[code] = _merge_entry(entry, live_entry, changes) if changes else entry
    for code, live_entry in live.types.items():
        if code in merged or (only is not None and code not in only):
            continue
        merged[code] = replace(live_entry, reviewed=False)
    return CmTypeManifest(
        service_url=live.service_url,
        repository_id=live.repository_id,
        discovered_at=live.discovered_at,
        types=merged,
        without_code=live.without_code,
    )


# ---------------------------------------------------------------------------
# Edición del operador
# ---------------------------------------------------------------------------


def _entry_or_fail(manifest: CmTypeManifest, id_corto: str) -> CmTypeEntry:
    entry = manifest.types.get(id_corto)
    if entry is None:
        raise ConfigurationError("el manifest no tiene ese ID corto", id_corto=id_corto)
    return entry


def _replace_entry(manifest: CmTypeManifest, entry: CmTypeEntry) -> CmTypeManifest:
    return replace(manifest, types={**dict(manifest.types), entry.id_corto: entry})


def set_decision(
    manifest: CmTypeManifest, id_corto: str, prop_id: str, decision: str
) -> CmTypeManifest:
    """Marca una propiedad como ``usar`` u ``omitir``.

    ``prop_id`` acepta el id de wire (``clbNonGroup.BAC_CIF``) o el
    nombre canónico (``BAC_CIF``)."""
    if decision not in (DECISION_USE, DECISION_OMIT):
        raise ConfigurationError(
            "decisión inválida (usá 'usar' u 'omitir')", id_corto=id_corto, decision=decision
        )
    entry = _entry_or_fail(manifest, id_corto)
    target = next(
        (p.id for p in entry.properties if prop_id in (p.id, canonical_name(p.id))),
        None,
    )
    if target is None:
        raise ConfigurationError(
            "el tipo no tiene esa propiedad escribible", id_corto=id_corto, prop_id=prop_id
        )
    return _replace_entry(
        manifest, replace(entry, decisions={**dict(entry.decisions), target: decision})
    )


def set_folder(manifest: CmTypeManifest, id_corto: str, folder: str) -> CmTypeManifest:
    """Fija la carpeta a mano: deja de derivarse y hay que re-verificarla."""
    entry = _entry_or_fail(manifest, id_corto)
    return _replace_entry(
        manifest,
        replace(entry, folder=folder, folder_source=FOLDER_MANUAL, folder_ok=None),
    )


def mark_reviewed(manifest: CmTypeManifest, id_corto: str) -> CmTypeManifest:
    """El operador se hace cargo del tipo: revisado y sin cambios pendientes."""
    entry = _entry_or_fail(manifest, id_corto)
    return _replace_entry(manifest, replace(entry, reviewed=True, changes=()))
