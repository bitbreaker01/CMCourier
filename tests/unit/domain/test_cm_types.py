"""Tests de los modelos del manifest de tipos CM (145 REQ-002).

Dominio puro: `dataclasses` frozen + dos helpers de nombres. Sin red,
sin disco, sin adapters.
"""

from __future__ import annotations

import dataclasses

import pytest

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

pytestmark = pytest.mark.unit


def _prop(pid: str, **kw: object) -> CmPropertyDef:
    base: dict[str, object] = {
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
    return CmPropertyDef(**base)  # type: ignore[arg-type]


class TestConstantes145:
    def test_valores_literales(self) -> None:
        assert DECISION_USE == "usar"
        assert DECISION_OMIT == "omitir"
        assert FOLDER_DERIVED == "derivada"
        assert FOLDER_MANUAL == "manual"


class TestCanonicalName145:
    @pytest.mark.parametrize(
        ("wire", "esperado"),
        [
            ("clbNonGroup.BAC_CIF", "BAC_CIF"),
            ("cmcourier:BAC_CIF", "BAC_CIF"),
            ("BAC_CIF", "BAC_CIF"),
            ("cmis:name", "name"),
            ("a.b:c", "c"),
            ("a:b.c", "c"),
            ("", ""),
        ],
    )
    def test_saca_el_prefijo_hasta_el_ultimo_separador(self, wire: str, esperado: str) -> None:
        assert canonical_name(wire) == esperado


class TestDeriveFolder145:
    def test_usa_el_local_name_tal_cual(self) -> None:
        assert derive_folder("BAC_01_02") == "/$type/BAC_01_02"


class TestCmPropertyDef145:
    def test_es_frozen(self) -> None:
        prop = _prop("clbNonGroup.BAC_CIF")
        with pytest.raises(dataclasses.FrozenInstanceError):
            prop.required = True  # type: ignore[misc]


class TestCmTypeEntry145:
    def test_usable_properties_respeta_orden_del_servidor(self) -> None:
        props = (_prop("p.A"), _prop("p.B"), _prop("p.C"))
        entry = CmTypeEntry(
            id_corto="DC01",
            type_id="$t!-2_DC01v-1",
            local_name="BAC_DC01",
            display_name="DC01 - Uno",
            folder=derive_folder("BAC_DC01"),
            properties=props,
            decisions={"p.C": DECISION_USE, "p.A": DECISION_USE, "p.B": DECISION_OMIT},
        )
        assert [p.id for p in entry.usable_properties()] == ["p.A", "p.C"]

    def test_decisions_es_read_only_y_copia_el_dict_del_caller(self) -> None:
        source = {"p.A": DECISION_USE}
        entry = CmTypeEntry(
            id_corto="DC01",
            type_id="t",
            local_name="ln",
            display_name="dn",
            folder="/$type/ln",
            properties=(_prop("p.A"),),
            decisions=source,
        )
        source["p.A"] = DECISION_OMIT
        assert entry.decisions["p.A"] == DECISION_USE
        with pytest.raises(TypeError):
            entry.decisions["p.B"] = DECISION_USE  # type: ignore[index]

    def test_defaults_de_los_campos_opcionales(self) -> None:
        entry = CmTypeEntry(
            id_corto="DC01",
            type_id="t",
            local_name="ln",
            display_name="dn",
            folder="/$type/ln",
        )
        assert entry.folder_source == FOLDER_DERIVED
        assert entry.folder_ok is None
        assert entry.properties == ()
        assert dict(entry.decisions) == {}
        assert entry.reviewed is False
        assert entry.changes == ()
        assert entry.missing_on_server is False


class TestCmTypeManifest145:
    def test_types_es_read_only(self) -> None:
        entry = CmTypeEntry(
            id_corto="DC01", type_id="t", local_name="ln", display_name="dn", folder="/$type/ln"
        )
        manifest = CmTypeManifest(
            service_url="http://cm/browser",
            repository_id="repo",
            discovered_at="2026-01-01T00:00:00+00:00",
            types={"DC01": entry},
        )
        assert manifest.types["DC01"] is entry
        assert manifest.without_code == ()
        with pytest.raises(TypeError):
            manifest.types["DC02"] = entry  # type: ignore[index]
