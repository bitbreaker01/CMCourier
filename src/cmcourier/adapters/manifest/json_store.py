"""Persistencia del manifest de tipos CM en JSON (145 REQ-002).

Un solo archivo, pensado para vivir versionado al lado del YAML: claves
ordenadas, ``indent=2`` y ``ensure_ascii=False`` para que un ``git diff``
muestre exactamente qué cambió el servidor o qué decidió el operador.

La escritura es atómica (tmp + :func:`os.replace`) porque la consola
(REQ-006) guarda en cada tecla: un Ctrl-C en el momento justo no puede
dejar el manifest a medio escribir.
"""

from __future__ import annotations

__all__ = ["JsonTypeManifestStore"]

import json
from pathlib import Path
from typing import Any

from cmcourier.domain.cm_types import (
    FOLDER_DERIVED,
    CmPropertyDef,
    CmTypeEntry,
    CmTypeManifest,
)
from cmcourier.domain.exceptions import ConfigurationError

#: Versión del formato en disco. Un manifest de otra versión no se adivina.
_VERSION = 1


def _property_to_json(prop: CmPropertyDef) -> dict[str, Any]:
    return {
        "id": prop.id,
        "display_name": prop.display_name,
        "property_type": prop.property_type,
        "cardinality": prop.cardinality,
        "updatability": prop.updatability,
        "required": prop.required,
        "max_length": prop.max_length,
        "default_value": prop.default_value,
        "inherited": prop.inherited,
        "choices": list(prop.choices),
    }


def _property_from_json(raw: dict[str, Any]) -> CmPropertyDef:
    max_length = raw.get("max_length")
    return CmPropertyDef(
        id=str(raw.get("id", "")),
        display_name=str(raw.get("display_name") or ""),
        property_type=str(raw.get("property_type") or ""),
        cardinality=str(raw.get("cardinality") or ""),
        updatability=str(raw.get("updatability") or ""),
        required=bool(raw.get("required", False)),
        max_length=int(max_length) if max_length is not None else None,
        default_value=raw.get("default_value"),
        inherited=bool(raw.get("inherited", False)),
        choices=tuple(str(c) for c in raw.get("choices") or ()),
    )


def _entry_to_json(entry: CmTypeEntry) -> dict[str, Any]:
    # ``id_corto`` es la clave del dict de tipos — no se duplica adentro.
    return {
        "type_id": entry.type_id,
        "local_name": entry.local_name,
        "display_name": entry.display_name,
        "folder": entry.folder,
        "folder_source": entry.folder_source,
        "folder_ok": entry.folder_ok,
        "properties": [_property_to_json(p) for p in entry.properties],
        "decisions": dict(entry.decisions),
        "reviewed": entry.reviewed,
        "changes": list(entry.changes),
        "missing_on_server": entry.missing_on_server,
    }


def _entry_from_json(id_corto: str, raw: dict[str, Any]) -> CmTypeEntry:
    return CmTypeEntry(
        id_corto=id_corto,
        type_id=str(raw.get("type_id") or ""),
        local_name=str(raw.get("local_name") or ""),
        display_name=str(raw.get("display_name") or ""),
        folder=str(raw.get("folder") or ""),
        folder_source=str(raw.get("folder_source") or FOLDER_DERIVED),
        folder_ok=raw.get("folder_ok"),
        properties=tuple(_property_from_json(p) for p in raw.get("properties") or ()),
        decisions={str(k): str(v) for k, v in (raw.get("decisions") or {}).items()},
        reviewed=bool(raw.get("reviewed", False)),
        changes=tuple(str(c) for c in raw.get("changes") or ()),
        missing_on_server=bool(raw.get("missing_on_server", False)),
    )


class JsonTypeManifestStore:
    """Lee y escribe el manifest de tipos CM en un archivo JSON."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """El archivo que gobierna este store."""
        return self._path

    def exists(self) -> bool:
        """``True`` sii ya hay un manifest en disco."""
        return self._path.is_file()

    def load(self) -> CmTypeManifest:
        """Lee el manifest del disco.

        Lanza :class:`ConfigurationError` si el archivo no existe, no es
        JSON válido o declara una ``version`` que este código no sabe
        leer — nunca devuelve un manifest a medias.
        """
        if not self._path.is_file():
            raise ConfigurationError(
                "no hay manifest de tipos CM; corré 'cmcourier types discover'",
                path=str(self._path),
            )
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                "el manifest de tipos CM no se puede leer", path=str(self._path), error=str(exc)
            ) from exc
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            raise ConfigurationError(
                "el manifest de tipos CM tiene una version desconocida",
                path=str(self._path),
                version=data.get("version") if isinstance(data, dict) else None,
                expected=_VERSION,
            )
        raw_types = data.get("types") or {}
        return CmTypeManifest(
            service_url=str(data.get("service_url") or ""),
            repository_id=str(data.get("repository_id") or ""),
            discovered_at=str(data.get("discovered_at") or ""),
            types={code: _entry_from_json(code, raw) for code, raw in raw_types.items()},
            without_code=tuple(
                (str(pair[0]), str(pair[1])) for pair in data.get("without_code") or ()
            ),
        )

    def save(self, manifest: CmTypeManifest) -> None:
        """Escribe el manifest de forma atómica: tmp + ``os.replace``."""
        payload = {
            "version": _VERSION,
            "service_url": manifest.service_url,
            "repository_id": manifest.repository_id,
            "discovered_at": manifest.discovered_at,
            "types": {code: _entry_to_json(e) for code, e in manifest.types.items()},
            "without_code": [list(pair) for pair in manifest.without_code],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        # ``Path.replace`` es ``os.replace``: rename atómico dentro del
        # mismo filesystem, sin ventana con el archivo a medio escribir.
        tmp.replace(self._path)
