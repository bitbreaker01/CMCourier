"""Tests del store JSON del manifest de tipos CM (145 REQ-002).

El JSON tiene que ser diffeable en git (claves ordenadas, indent 2, sin
escapes unicode) y el round-trip tiene que ser exacto — incluido el
ORDEN de las propiedades dentro de cada tipo, que es el del servidor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cmcourier.adapters.manifest import JsonTypeManifestStore
from cmcourier.domain.cm_types import (
    DECISION_OMIT,
    DECISION_USE,
    FOLDER_MANUAL,
    CmPropertyDef,
    CmTypeEntry,
    CmTypeManifest,
)
from cmcourier.domain.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


def _prop(pid: str, **kw: Any) -> CmPropertyDef:
    base: dict[str, Any] = {
        "id": pid,
        "display_name": pid,
        "property_type": "string",
        "cardinality": "single",
        "updatability": "readwrite",
        "required": False,
        "max_length": None,
        "default_value": None,
        "inherited": False,
        "choices": (),
    }
    base.update(kw)
    return CmPropertyDef(**base)


def _manifest() -> CmTypeManifest:
    entry = CmTypeEntry(
        id_corto="PT55.2",
        type_id="$t!-2_BAC_01v-1",
        local_name="BAC_01",
        display_name="PT55 - Contratos y Pagarés",
        folder="/carpeta/manual",
        folder_source=FOLDER_MANUAL,
        folder_ok=False,
        properties=(
            _prop("clbNonGroup.BAC_Zeta", required=True, max_length=9),
            _prop("clbNonGroup.BAC_Alfa", default_value="BAC", choices=("A", "B")),
        ),
        decisions={"clbNonGroup.BAC_Zeta": DECISION_USE, "clbNonGroup.BAC_Alfa": DECISION_OMIT},
        reviewed=True,
        changes=("+ clbNonGroup.BAC_Zeta (required)",),
        missing_on_server=True,
    )
    otro = CmTypeEntry(
        id_corto="DC01",
        type_id="$t!-2_BAC_02v-1",
        local_name="BAC_02",
        display_name="DC01 - Otro",
        folder="/$type/BAC_02",
    )
    return CmTypeManifest(
        service_url="http://cm/browser",
        repository_id="repo",
        discovered_at="2026-01-01T00:00:00+00:00",
        types={"PT55.2": entry, "DC01": otro},
        without_code=(("$t!-2_CmisDocumentv-1", "Default Document Type"),),
    )


class TestJsonTypeManifestStore145:
    def test_round_trip_exacto(self, tmp_path: Path) -> None:
        store = JsonTypeManifestStore(tmp_path / "types.json")
        original = _manifest()
        store.save(original)
        vuelta = store.load()
        assert vuelta == original
        assert dict(vuelta.types) == dict(original.types)

    def test_conserva_el_orden_de_las_propiedades(self, tmp_path: Path) -> None:
        store = JsonTypeManifestStore(tmp_path / "types.json")
        store.save(_manifest())
        props = store.load().types["PT55.2"].properties
        assert [p.id for p in props] == ["clbNonGroup.BAC_Zeta", "clbNonGroup.BAC_Alfa"]

    def test_layout_diffeable(self, tmp_path: Path) -> None:
        path = tmp_path / "types.json"
        JsonTypeManifestStore(path).save(_manifest())
        texto = path.read_text(encoding="utf-8")
        assert texto.startswith("{\n")
        assert '  "discovered_at"' in texto  # indent 2
        assert "Pagarés" in texto  # ensure_ascii=False
        data = json.loads(texto)
        assert data["version"] == 1
        assert list(data) == sorted(data)  # sort_keys
        assert list(data["types"]) == ["DC01", "PT55.2"]
        assert data["without_code"] == [["$t!-2_CmisDocumentv-1", "Default Document Type"]]

    def test_las_propiedades_son_una_lista_y_las_decisiones_un_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "types.json"
        JsonTypeManifestStore(path).save(_manifest())
        entry = json.loads(path.read_text(encoding="utf-8"))["types"]["PT55.2"]
        assert isinstance(entry["properties"], list)
        assert isinstance(entry["decisions"], dict)
        assert "id_corto" not in entry  # es la clave del dict, no se duplica

    def test_exists(self, tmp_path: Path) -> None:
        store = JsonTypeManifestStore(tmp_path / "types.json")
        assert store.exists() is False
        store.save(_manifest())
        assert store.exists() is True

    def test_save_crea_los_directorios_padre(self, tmp_path: Path) -> None:
        store = JsonTypeManifestStore(tmp_path / "sub" / "dir" / "types.json")
        store.save(_manifest())
        assert store.exists() is True

    def test_save_es_atomico_y_no_deja_el_tmp(self, tmp_path: Path) -> None:
        path = tmp_path / "types.json"
        store = JsonTypeManifestStore(path)
        store.save(_manifest())
        store.save(_manifest())
        assert not path.with_suffix(".tmp").exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["types.json"]

    def test_load_de_un_archivo_ausente_explica_el_problema(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError) as exc:
            JsonTypeManifestStore(tmp_path / "types.json").load()
        assert "types.json" in str(exc.value)

    def test_load_rechaza_una_version_desconocida(self, tmp_path: Path) -> None:
        path = tmp_path / "types.json"
        path.write_text(json.dumps({"version": 2, "types": {}}), encoding="utf-8")
        with pytest.raises(ConfigurationError) as exc:
            JsonTypeManifestStore(path).load()
        assert "version" in str(exc.value).lower()

    def test_load_rechaza_json_invalido(self, tmp_path: Path) -> None:
        path = tmp_path / "types.json"
        path.write_text("{no soy json", encoding="utf-8")
        with pytest.raises(ConfigurationError):
            JsonTypeManifestStore(path).load()

    def test_load_tolera_campos_opcionales_ausentes(self, tmp_path: Path) -> None:
        path = tmp_path / "types.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "service_url": "u",
                    "repository_id": "r",
                    "discovered_at": "d",
                    "types": {
                        "DC01": {
                            "type_id": "t",
                            "local_name": "ln",
                            "display_name": "dn",
                            "folder": "/$type/ln",
                            "properties": [{"id": "p.A"}],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        manifest = JsonTypeManifestStore(path).load()
        entry = manifest.types["DC01"]
        assert entry.id_corto == "DC01"
        assert entry.properties[0].id == "p.A"
        assert entry.properties[0].max_length is None
        assert entry.reviewed is False
        assert manifest.without_code == ()
