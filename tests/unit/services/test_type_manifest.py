"""Tests de la lógica pura del manifest de tipos CM (145 REQ-002).

Sin red y sin disco: se le dan `dict`s con la forma que devuelve
``typeDescendants`` y se verifica el parseo, el diff y el merge. El
fixture ``type_descendants_sample.json`` son 4 tipos recortados del dump
real de PRD (``reference-data/cmis-responses/EjemploRespuestaCMIS.txt``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cmcourier.domain.cm_types import (
    DECISION_OMIT,
    DECISION_USE,
    FOLDER_DERIVED,
    FOLDER_MANUAL,
    CmPropertyDef,
    CmTypeEntry,
    CmTypeManifest,
)
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.services.type_manifest import (
    ManifestDiff,
    apply_diff,
    build_entry_from_type,
    build_manifest,
    diff_manifest,
    flatten_types,
    mark_reviewed,
    set_decision,
    set_folder,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "cmis"
_FIXTURE = _FIXTURES / "type_descendants_sample.json"


def _sample_tree() -> list[dict[str, Any]]:
    with _FIXTURE.open(encoding="utf-8") as fh:
        data: list[dict[str, Any]] = json.load(fh)
    return data


def _pd(pid: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": pid,
        "displayName": pid,
        "propertyType": "string",
        "cardinality": "single",
        "updatability": "readwrite",
        "required": False,
        "inherited": False,
    }
    base.update(kw)
    return base


def _type_def(
    *,
    type_id: str = "$t!-2_BAC_DC01v-1",
    local_name: str = "BAC_DC01",
    display_name: str = "DC01 - Uno",
    creatable: bool = True,
    base_id: str = "cmis:document",
    props: list[dict[str, Any]] | None = None,
    id_corto: str | None = "DC01",
) -> dict[str, Any]:
    defs = list(props or [])
    if id_corto is not None:
        defs.insert(0, _pd("clbNonGroup.BAC_ID_Corto", required=True, defaultValue=id_corto))
    return {
        "id": type_id,
        "localName": local_name,
        "displayName": display_name,
        "baseId": base_id,
        "creatable": creatable,
        "propertyDefinitions": {d["id"]: d for d in defs},
    }


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


def _manifest(
    *entries: CmTypeEntry, discovered_at: str = "2026-01-01T00:00:00+00:00"
) -> CmTypeManifest:
    return CmTypeManifest(
        service_url="http://cm/browser",
        repository_id="repo",
        discovered_at=discovered_at,
        types={e.id_corto: e for e in entries},
    )


def _entry(**kw: Any) -> CmTypeEntry:
    base: dict[str, Any] = {
        "id_corto": "DC01",
        "type_id": "$t!-2_BAC_DC01v-1",
        "local_name": "BAC_DC01",
        "display_name": "DC01 - Uno",
        "folder": "/$type/BAC_DC01",
    }
    base.update(kw)
    return CmTypeEntry(**base)


# ---------------------------------------------------------------------------
# build_entry_from_type
# ---------------------------------------------------------------------------


class TestBuildEntryFromType145:
    def test_id_corto_sale_del_default_de_bac_id_corto(self) -> None:
        entry = build_entry_from_type(_type_def(id_corto="PT95"))
        assert entry is not None
        assert entry.id_corto == "PT95"

    def test_id_corto_acepta_default_como_lista(self) -> None:
        td = _type_def(id_corto=None)
        td["propertyDefinitions"]["clbNonGroup.BAC_ID_Corto"] = _pd(
            "clbNonGroup.BAC_ID_Corto", defaultValue=["PT55.2"], required=True
        )
        entry = build_entry_from_type(td)
        assert entry is not None
        assert entry.id_corto == "PT55.2"

    def test_fallback_al_prefijo_del_display_name(self) -> None:
        entry = build_entry_from_type(
            _type_def(id_corto=None, display_name="PT12 - Cédula de identidad")
        )
        assert entry is not None
        assert entry.id_corto == "PT12"

    def test_sin_id_corto_ni_prefijo_devuelve_none(self) -> None:
        assert build_entry_from_type(_type_def(id_corto=None, display_name="Default Doc")) is None

    def test_no_creatable_devuelve_none(self) -> None:
        assert build_entry_from_type(_type_def(creatable=False)) is None

    def test_base_id_distinto_de_document_devuelve_none(self) -> None:
        assert build_entry_from_type(_type_def(base_id="cmis:folder")) is None

    def test_excluye_readonly_y_las_dos_propiedades_del_codigo(self) -> None:
        entry = build_entry_from_type(
            _type_def(
                props=[
                    _pd("cmis:name", required=True),
                    _pd("cmis:objectTypeId", updatability="oncreate", required=True),
                    _pd("cmis:objectId", updatability="readonly"),
                    _pd("clbNonGroup.BAC_CIF", required=True, maxLength=9),
                ]
            )
        )
        assert entry is not None
        assert [p.id for p in entry.properties] == [
            "clbNonGroup.BAC_ID_Corto",
            "clbNonGroup.BAC_CIF",
        ]

    def test_conserva_oncreate(self) -> None:
        entry = build_entry_from_type(
            _type_def(props=[_pd("clbNonGroup.BAC_X", updatability="oncreate")])
        )
        assert entry is not None
        assert [p.id for p in entry.properties][-1] == "clbNonGroup.BAC_X"

    def test_decision_sugerida_requerida_sin_default_es_usar(self) -> None:
        entry = build_entry_from_type(
            _type_def(
                props=[
                    _pd("clbNonGroup.BAC_CIF", required=True, maxLength=9),
                    _pd("clbNonGroup.BAC", required=True, defaultValue="BAC"),
                    _pd("clbNonGroup.BAC_Opt"),
                ]
            )
        )
        assert entry is not None
        assert dict(entry.decisions) == {
            "clbNonGroup.BAC_ID_Corto": DECISION_OMIT,
            "clbNonGroup.BAC_CIF": DECISION_USE,
            "clbNonGroup.BAC": DECISION_OMIT,
            "clbNonGroup.BAC_Opt": DECISION_OMIT,
        }
        assert [p.id for p in entry.usable_properties()] == ["clbNonGroup.BAC_CIF"]

    @pytest.mark.parametrize(
        ("raw", "esperado"),
        [
            ({}, None),
            ({"defaultValue": None}, None),
            ({"defaultValue": []}, None),
            ({"defaultValue": ""}, None),
            ({"defaultValue": "BAC"}, "BAC"),
            ({"defaultValue": ["BAC"]}, "BAC"),
            ({"defaultValue": ["A", "B"]}, "A, B"),
            ({"defaultValue": 7}, "7"),
        ],
    )
    def test_normaliza_default_value(self, raw: dict[str, Any], esperado: str | None) -> None:
        entry = build_entry_from_type(_type_def(props=[_pd("clbNonGroup.BAC_X", **raw)]))
        assert entry is not None
        assert entry.properties[-1].default_value == esperado

    def test_choices_se_aplanan_a_sus_valores(self) -> None:
        entry = build_entry_from_type(
            _type_def(
                props=[
                    _pd(
                        "clbNonGroup.BAC_Tipo",
                        choices=[
                            {"value": "A", "displayName": "Alfa"},
                            {"value": "B", "displayName": "Beta"},
                        ],
                    )
                ]
            )
        )
        assert entry is not None
        assert entry.properties[-1].choices == ("A", "B")

    def test_campos_derivados_de_la_entrada_nueva(self) -> None:
        entry = build_entry_from_type(_type_def())
        assert entry is not None
        assert entry.folder == "/$type/BAC_DC01"
        assert entry.folder_source == FOLDER_DERIVED
        assert entry.folder_ok is None
        assert entry.reviewed is False
        assert entry.changes == ()
        assert entry.missing_on_server is False


# ---------------------------------------------------------------------------
# build_manifest / flatten_types
# ---------------------------------------------------------------------------


class TestBuildManifest145:
    def test_aplana_el_arbol_recursivamente(self) -> None:
        tree = _sample_tree()
        assert len(tree) == 2  # el fixture tiene hijos anidados
        assert len(flatten_types(tree)) == 4

    def test_fixture_real_pt55_y_pt55_2_son_entradas_distintas(self) -> None:
        manifest = build_manifest(
            _sample_tree(),
            service_url="http://cm/browser",
            repository_id="repo",
            discovered_at="2026-01-01T00:00:00+00:00",
        )
        assert set(manifest.types) == {"PT95", "PT55", "PT55.2"}
        assert manifest.types["PT55"].type_id != manifest.types["PT55.2"].type_id
        assert manifest.types["PT55"].display_name.startswith("PT55 - ")
        assert manifest.types["PT55.2"].display_name.startswith("PT55 - ")

    def test_fixture_real_sin_codigo_va_a_without_code(self) -> None:
        manifest = build_manifest(
            _sample_tree(),
            service_url="http://cm/browser",
            repository_id="repo",
            discovered_at="2026-01-01T00:00:00+00:00",
        )
        assert manifest.without_code == (("$t!-2_CmisDocumentv-1", "Default Document Type"),)

    def test_fixture_real_conserva_max_length_y_carpeta_derivada(self) -> None:
        manifest = build_manifest(
            _sample_tree(),
            service_url="http://cm/browser",
            repository_id="repo",
            discovered_at="2026-01-01T00:00:00+00:00",
        )
        pt95 = manifest.types["PT95"]
        cif = next(p for p in pt95.properties if p.id == "clbNonGroup.BAC_CIF")
        assert cif.max_length == 9
        assert cif.required is True
        assert pt95.folder == "/$type/BAC_01_01_02_04_01_18"

    def test_guarda_la_metadata_del_repositorio(self) -> None:
        manifest = build_manifest(
            [],
            service_url="http://cm/browser",
            repository_id="repo-7",
            discovered_at="2026-02-03T04:05:06+00:00",
        )
        assert manifest.service_url == "http://cm/browser"
        assert manifest.repository_id == "repo-7"
        assert manifest.discovered_at == "2026-02-03T04:05:06+00:00"
        assert dict(manifest.types) == {}

    def test_id_corto_duplicado_es_configuration_error(self) -> None:
        tree = [
            {"type": _type_def(type_id="$t!-2_Av-1", local_name="A")},
            {"type": _type_def(type_id="$t!-2_Bv-1", local_name="B")},
        ]
        with pytest.raises(ConfigurationError) as exc:
            build_manifest(
                tree,
                service_url="u",
                repository_id="r",
                discovered_at="2026-01-01T00:00:00+00:00",
            )
        assert "$t!-2_Av-1" in str(exc.value)
        assert "$t!-2_Bv-1" in str(exc.value)
        assert "DC01" in str(exc.value)

    def test_on_type_reporta_progreso_por_tipo(self) -> None:
        seen: list[tuple[int, int]] = []
        build_manifest(
            _sample_tree(),
            service_url="u",
            repository_id="r",
            discovered_at="2026-01-01T00:00:00+00:00",
            on_type=lambda done, total: seen.append((done, total)),
        )
        assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]


# ---------------------------------------------------------------------------
# diff_manifest / ManifestDiff
# ---------------------------------------------------------------------------


class TestDiffManifest145:
    def test_sin_cambios_es_vacio(self) -> None:
        entry = _entry(properties=(_prop("p.A"),), decisions={"p.A": DECISION_OMIT})
        diff = diff_manifest(_manifest(entry), _manifest(entry))
        assert diff.is_empty
        assert diff.new_types == ()
        assert diff.missing_types == ()
        assert dict(diff.changed) == {}

    def test_tipo_nuevo_y_tipo_ausente(self) -> None:
        local = _manifest(_entry())
        live = _manifest(_entry(id_corto="DC02", local_name="BAC_DC02"))
        diff = diff_manifest(local, live)
        assert diff.new_types == ("DC02",)
        assert diff.missing_types == ("DC01",)
        assert not diff.is_empty

    def test_propiedad_agregada_requerida(self) -> None:
        local = _manifest(_entry(properties=(_prop("clbNonGroup.BAC_CIF"),)))
        live = _manifest(
            _entry(
                properties=(
                    _prop("clbNonGroup.BAC_CIF"),
                    _prop("clbNonGroup.BAC_Nueva", required=True),
                )
            )
        )
        diff = diff_manifest(local, live)
        assert diff.changed["DC01"] == ("+ clbNonGroup.BAC_Nueva (required)",)

    def test_propiedad_quitada(self) -> None:
        local = _manifest(_entry(properties=(_prop("clbNonGroup.BAC_Vieja"),)))
        live = _manifest(_entry(properties=()))
        assert diff_manifest(local, live).changed["DC01"] == ("- clbNonGroup.BAC_Vieja",)

    def test_cambio_de_max_length(self) -> None:
        local = _manifest(_entry(properties=(_prop("clbNonGroup.BAC_CIF", max_length=9),)))
        live = _manifest(_entry(properties=(_prop("clbNonGroup.BAC_CIF", max_length=12),)))
        assert diff_manifest(local, live).changed["DC01"] == (
            "~ clbNonGroup.BAC_CIF: max_length 9 → 12",
        )

    def test_cambio_de_type_id_y_local_name(self) -> None:
        local = _manifest(_entry())
        live = _manifest(_entry(type_id="$t!-2_BAC_DC01v-2", local_name="BAC_DC01_NEW"))
        assert diff_manifest(local, live).changed["DC01"] == (
            "~ type_id: $t!-2_BAC_DC01v-1 → $t!-2_BAC_DC01v-2",
            "~ local_name: BAC_DC01 → BAC_DC01_NEW",
        )

    def test_tipo_marcado_ausente_que_vuelve_al_servidor(self) -> None:
        local = _manifest(_entry(missing_on_server=True))
        live = _manifest(_entry())
        assert diff_manifest(local, live).changed["DC01"] == ("~ vuelve a existir en el servidor",)

    def test_render_lista_las_tres_secciones(self) -> None:
        diff = ManifestDiff(
            new_types=("DC02",),
            missing_types=("DC03",),
            changed={"DC01": ("+ clbNonGroup.BAC_Nueva (required)",)},
        )
        texto = diff.render()
        assert "DC02" in texto
        assert "DC03" in texto
        assert "DC01" in texto
        assert "+ clbNonGroup.BAC_Nueva (required)" in texto

    def test_render_de_un_diff_vacio_lo_dice(self) -> None:
        assert ManifestDiff((), (), {}).render().strip() != ""


# ---------------------------------------------------------------------------
# apply_diff
# ---------------------------------------------------------------------------


class TestApplyDiff145:
    def test_escenario_5_propiedad_nueva_requerida(self) -> None:
        """145 escenario 5: entra como ``usar``, la decisión previa se conserva."""
        local = _manifest(
            _entry(
                properties=(_prop("clbNonGroup.BAC_CIF", required=True),),
                decisions={"clbNonGroup.BAC_CIF": DECISION_OMIT},
                reviewed=True,
            )
        )
        live = _manifest(
            _entry(
                properties=(
                    _prop("clbNonGroup.BAC_CIF", required=True),
                    _prop("clbNonGroup.BAC_Nueva", required=True),
                ),
                decisions={
                    "clbNonGroup.BAC_CIF": DECISION_USE,
                    "clbNonGroup.BAC_Nueva": DECISION_USE,
                },
            )
        )
        merged = apply_diff(local, live)
        entry = merged.types["DC01"]
        assert entry.decisions["clbNonGroup.BAC_CIF"] == DECISION_OMIT
        assert entry.decisions["clbNonGroup.BAC_Nueva"] == DECISION_USE
        assert entry.reviewed is False
        assert entry.changes == ("+ clbNonGroup.BAC_Nueva (required)",)

    def test_escenario_6_carpeta_manual_no_se_pisa(self) -> None:
        local = _manifest(
            _entry(folder="/custom/ruta", folder_source=FOLDER_MANUAL, folder_ok=True)
        )
        live = _manifest(_entry(local_name="BAC_OTRO", folder="/$type/BAC_OTRO"))
        entry = apply_diff(local, live).types["DC01"]
        assert entry.folder == "/custom/ruta"
        assert entry.folder_source == FOLDER_MANUAL

    def test_carpeta_derivada_se_recalcula_y_pierde_la_verificacion(self) -> None:
        local = _manifest(_entry(folder_ok=True))
        live = _manifest(_entry(local_name="BAC_OTRO", folder="/$type/BAC_OTRO"))
        entry = apply_diff(local, live).types["DC01"]
        assert entry.folder == "/$type/BAC_OTRO"
        assert entry.folder_source == FOLDER_DERIVED
        assert entry.folder_ok is None

    def test_tipo_ausente_se_marca_no_se_borra(self) -> None:
        local = _manifest(_entry())
        live = _manifest()
        merged = apply_diff(local, live)
        assert merged.types["DC01"].missing_on_server is True

    def test_tipo_nuevo_entra_sin_revisar(self) -> None:
        local = _manifest()
        live = _manifest(_entry(reviewed=True))
        entry = apply_diff(local, live).types["DC01"]
        assert entry.reviewed is False

    def test_tipo_sin_cambios_no_se_toca(self) -> None:
        entry = _entry(reviewed=True, properties=(_prop("p.A"),), decisions={"p.A": DECISION_USE})
        merged = apply_diff(_manifest(entry), _manifest(entry))
        assert merged.types["DC01"] is entry

    def test_only_acota_los_tipos_tocados(self) -> None:
        local = _manifest(
            _entry(properties=(), reviewed=True),
            _entry(id_corto="DC02", local_name="BAC_DC02", properties=(), reviewed=True),
        )
        live = _manifest(
            _entry(properties=(_prop("p.A"),)),
            _entry(id_corto="DC02", local_name="BAC_DC02", properties=(_prop("p.A"),)),
        )
        merged = apply_diff(local, live, only={"DC01"})
        assert merged.types["DC01"].properties != ()
        assert merged.types["DC02"].properties == ()
        assert merged.types["DC02"].reviewed is True

    def test_only_no_agrega_tipos_fuera_del_filtro(self) -> None:
        local = _manifest()
        live = _manifest(_entry(), _entry(id_corto="DC02", local_name="BAC_DC02"))
        merged = apply_diff(local, live, only={"DC01"})
        assert set(merged.types) == {"DC01"}

    def test_toma_la_metadata_del_live(self) -> None:
        local = _manifest(_entry(), discovered_at="2020-01-01T00:00:00+00:00")
        live = _manifest(_entry(), discovered_at="2026-06-06T06:06:06+00:00")
        assert apply_diff(local, live).discovered_at == "2026-06-06T06:06:06+00:00"

    def test_no_muta_el_manifest_local(self) -> None:
        local = _manifest(_entry(properties=(), reviewed=True))
        live = _manifest(_entry(properties=(_prop("p.A"),)))
        apply_diff(local, live)
        assert local.types["DC01"].properties == ()
        assert local.types["DC01"].reviewed is True


# ---------------------------------------------------------------------------
# set_decision / set_folder / mark_reviewed
# ---------------------------------------------------------------------------


class TestEdicionInmutable145:
    def test_set_decision_por_id_de_wire(self) -> None:
        base = _manifest(
            _entry(
                properties=(_prop("clbNonGroup.BAC_CIF"),),
                decisions={"clbNonGroup.BAC_CIF": DECISION_OMIT},
            )
        )
        nuevo = set_decision(base, "DC01", "clbNonGroup.BAC_CIF", DECISION_USE)
        assert nuevo.types["DC01"].decisions["clbNonGroup.BAC_CIF"] == DECISION_USE
        assert base.types["DC01"].decisions["clbNonGroup.BAC_CIF"] == DECISION_OMIT

    def test_set_decision_acepta_el_nombre_canonico(self) -> None:
        base = _manifest(
            _entry(
                properties=(_prop("clbNonGroup.BAC_CIF"),),
                decisions={"clbNonGroup.BAC_CIF": DECISION_OMIT},
            )
        )
        nuevo = set_decision(base, "DC01", "BAC_CIF", DECISION_USE)
        assert nuevo.types["DC01"].decisions["clbNonGroup.BAC_CIF"] == DECISION_USE

    def test_set_decision_rechaza_valores_invalidos(self) -> None:
        base = _manifest(_entry(properties=(_prop("p.A"),), decisions={"p.A": DECISION_USE}))
        with pytest.raises(ConfigurationError):
            set_decision(base, "DC01", "p.A", "quizas")

    def test_set_decision_rechaza_tipo_o_propiedad_inexistente(self) -> None:
        base = _manifest(_entry(properties=(_prop("p.A"),), decisions={"p.A": DECISION_USE}))
        with pytest.raises(ConfigurationError):
            set_decision(base, "ZZ99", "p.A", DECISION_USE)
        with pytest.raises(ConfigurationError):
            set_decision(base, "DC01", "p.Z", DECISION_USE)

    def test_set_folder_marca_manual_y_borra_la_verificacion(self) -> None:
        base = _manifest(_entry(folder_ok=True))
        entry = set_folder(base, "DC01", "/otra/ruta").types["DC01"]
        assert entry.folder == "/otra/ruta"
        assert entry.folder_source == FOLDER_MANUAL
        assert entry.folder_ok is None
        assert base.types["DC01"].folder_source == FOLDER_DERIVED

    def test_mark_reviewed_limpia_los_cambios(self) -> None:
        base = _manifest(_entry(changes=("+ p.A",)))
        entry = mark_reviewed(base, "DC01").types["DC01"]
        assert entry.reviewed is True
        assert entry.changes == ()
        assert base.types["DC01"].reviewed is False

    def test_mark_reviewed_de_un_tipo_inexistente_falla(self) -> None:
        with pytest.raises(ConfigurationError):
            mark_reviewed(_manifest(), "ZZ99")
