"""Tests unitarios para ``MappingService`` en modo manifest (145 REQ-001).

El modo manifest lee UN solo ``IDataSource`` — ``MapeoRVI_CM.csv``
reducido a ``IDSistema,IDRVI,IDCM`` — y resuelve cada ``IDCM`` contra el
:class:`CmTypeManifest` que ``types discover`` bajó del servidor. Nadie
mantiene ``MetadatosCM.csv`` a mano nunca más.

La clave del cache es ``(sistema, id_rvi)``: el mismo código RVI puede
apuntar a clases distintas según el sistema de origen, y la fila sin
sistema es el comodín.

Como en el resto de los tests de mapping, la fuente es un `fake` en
memoria: el SUT es el servicio, no el adaptador (Principio VI).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from cmcourier.domain.cm_types import (
    DECISION_OMIT,
    DECISION_USE,
    CmPropertyDef,
    CmTypeEntry,
    CmTypeManifest,
)
from cmcourier.domain.exceptions import ConfigurationError, IDRViNotMappedError
from cmcourier.services.mapping import MappingColumnsConfig, MappingService

pytestmark = pytest.mark.unit


class _FakeSource:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def get_all(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def close(self) -> None:  # pragma: no cover — completitud de protocolo
        pass


def _rows(*items: tuple[str, str, str]) -> list[dict[str, object]]:
    """Filas ``MapeoRVI_CM`` desde tuplas ``(IDSistema, IDRVI, IDCM)``."""
    return [
        {"IDSistema": sistema, "IDRVI": id_rvi, "IDCM": id_cm} for sistema, id_rvi, id_cm in items
    ]


def _prop(
    pid: str,
    *,
    required: bool = True,
    default_value: str | None = None,
    property_type: str = "string",
    max_length: int | None = None,
) -> CmPropertyDef:
    return CmPropertyDef(
        id=pid,
        display_name=pid,
        property_type=property_type,
        cardinality="single",
        updatability="readwrite",
        required=required,
        max_length=max_length,
        default_value=default_value,
        inherited=False,
        choices=(),
    )


def _entry(
    id_corto: str,
    *,
    props: tuple[CmPropertyDef, ...] = (),
    decisions: dict[str, str] | None = None,
    local_name: str | None = None,
) -> CmTypeEntry:
    local = local_name or f"BAC_{id_corto}"
    return CmTypeEntry(
        id_corto=id_corto,
        type_id=f"$t!-2_{local}v-1",
        local_name=local,
        display_name=f"{id_corto} - Clase {id_corto}",
        folder=f"/$type/{local}",
        properties=props,
        decisions=decisions if decisions is not None else {p.id: DECISION_USE for p in props},
    )


def _manifest(*entries: CmTypeEntry) -> CmTypeManifest:
    return CmTypeManifest(
        service_url="http://cm.test/browser",
        repository_id="repo",
        discovered_at="2026-01-01T00:00:00Z",
        types={e.id_corto: e for e in entries},
    )


class TestManifestModeRow:
    def test_row_resolves_against_the_manifest(self) -> None:
        props = (_prop("clbNonGroup.BAC_CIF"), _prop("clbNonGroup.BAC_ID_Corto"))
        manifest = _manifest(_entry("DC01", props=props))
        svc = MappingService(
            _FakeSource(_rows(("", "0001", "DC01"))),  # type: ignore[arg-type]
            type_manifest=manifest,
        )
        m = svc.get_mapping("0001")
        assert m.id_rvi == "0001"
        assert m.id_corto == "DC01"
        assert m.cmis_type == "$t!-2_BAC_DC01v-1"
        assert m.cmis_folder == "/$type/BAC_DC01"
        assert m.clase_id == "BAC_DC01"
        assert m.clase_name == "DC01 - Clase DC01"

    def test_required_fields_are_canonical_names_in_server_order(self) -> None:
        props = (_prop("clbNonGroup.BAC_CIF"), _prop("cmcourier:BAC_Nombre_Documento"))
        manifest = _manifest(_entry("DC01", props=props))
        svc = MappingService(
            _FakeSource(_rows(("", "0001", "DC01"))),  # type: ignore[arg-type]
            type_manifest=manifest,
        )
        m = svc.get_mapping("0001")
        assert m.required_metadata_fields == ("BAC_CIF", "BAC_Nombre_Documento")
        assert m.cmis_property_ids is not None
        assert dict(m.cmis_property_ids) == {
            "BAC_CIF": "clbNonGroup.BAC_CIF",
            "BAC_Nombre_Documento": "cmcourier:BAC_Nombre_Documento",
        }

    def test_only_usable_properties_travel(self) -> None:
        use = _prop("clbNonGroup.BAC_CIF")
        omit = _prop("clbNonGroup.BAC_Fecha")
        manifest = _manifest(
            _entry(
                "DC01",
                props=(use, omit),
                decisions={use.id: DECISION_USE, omit.id: DECISION_OMIT},
            )
        )
        svc = MappingService(
            _FakeSource(_rows(("", "0001", "DC01"))),  # type: ignore[arg-type]
            type_manifest=manifest,
        )
        assert svc.get_mapping("0001").required_metadata_fields == ("BAC_CIF",)

    def test_no_usable_properties_leaves_cmis_property_ids_none(self) -> None:
        manifest = _manifest(_entry("DC01"))
        svc = MappingService(
            _FakeSource(_rows(("", "0001", "DC01"))),  # type: ignore[arg-type]
            type_manifest=manifest,
        )
        m = svc.get_mapping("0001")
        assert m.required_metadata_fields == ()
        assert m.cmis_property_ids is None


class TestManifestModeSystemKey:
    """Escenario 2 del spec: ``(sistema, id_rvi)`` con comodín."""

    @pytest.fixture
    def svc(self) -> MappingService:
        manifest = _manifest(_entry("DC01"), _entry("DC02"))
        return MappingService(
            _FakeSource(  # type: ignore[arg-type]
                _rows(("", "0001", "DC01"), ("RVI2", "0001", "DC02"))
            ),
            type_manifest=manifest,
        )

    def test_specific_system_wins(self, svc: MappingService) -> None:
        assert svc.get_mapping("0001", "rvi2").id_corto == "DC02"

    def test_system_comparison_is_case_insensitive_and_stripped(self, svc: MappingService) -> None:
        assert svc.get_mapping("0001", "  RVI2 ").id_corto == "DC02"

    def test_unknown_system_falls_back_to_wildcard(self, svc: MappingService) -> None:
        assert svc.get_mapping("0001", "X").id_corto == "DC01"

    def test_no_system_uses_wildcard(self, svc: MappingService) -> None:
        assert svc.get_mapping("0001").id_corto == "DC01"

    def test_both_rows_are_cached(self, svc: MappingService) -> None:
        assert svc.count() == 2
        assert "0001" in svc
        assert svc.cm_codes() == ("DC01", "DC02")

    def test_missing_wildcard_and_unknown_system_raises(self) -> None:
        manifest = _manifest(_entry("DC02"))
        svc = MappingService(
            _FakeSource(_rows(("RVI2", "0001", "DC02"))),  # type: ignore[arg-type]
            type_manifest=manifest,
        )
        assert svc.get_mapping("0001", "RVI2").id_corto == "DC02"
        with pytest.raises(IDRViNotMappedError):
            svc.get_mapping("0001", "OTRO")
        with pytest.raises(IDRViNotMappedError):
            svc.get_mapping("0001")

    def test_duplicate_same_key_first_wins_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        manifest = _manifest(_entry("DC01"), _entry("DC02"))
        with caplog.at_level(logging.WARNING):
            svc = MappingService(
                _FakeSource(  # type: ignore[arg-type]
                    _rows(("RVI2", "0001", "DC01"), ("rvi2", "0001", "DC02"))
                ),
                type_manifest=manifest,
            )
        assert svc.get_mapping("0001", "RVI2").id_corto == "DC01"
        assert svc.count() == 1
        assert any("duplicate" in r.message.lower() for r in caplog.records)
        # La fila descartada del índice primario sigue siendo indexable por IDCM.
        assert svc.cm_codes() == ("DC01", "DC02")


class TestManifestModeColumns:
    def test_id_sistema_column_is_optional(self) -> None:
        manifest = _manifest(_entry("DC01"))
        svc = MappingService(
            _FakeSource([{"IDRVI": "0001", "IDCM": "DC01"}]),  # type: ignore[arg-type]
            type_manifest=manifest,
        )
        assert svc.get_mapping("0001").id_corto == "DC01"

    def test_missing_id_rvi_column_is_a_configuration_error(self) -> None:
        manifest = _manifest(_entry("DC01"))
        with pytest.raises(ConfigurationError):
            MappingService(
                _FakeSource([{"IDCM": "DC01"}]),  # type: ignore[arg-type]
                type_manifest=manifest,
            )

    def test_missing_id_cm_column_is_a_configuration_error(self) -> None:
        manifest = _manifest(_entry("DC01"))
        with pytest.raises(ConfigurationError):
            MappingService(
                _FakeSource([{"IDRVI": "0001"}]),  # type: ignore[arg-type]
                type_manifest=manifest,
            )

    def test_column_names_are_configurable(self) -> None:
        manifest = _manifest(_entry("DC01"))
        columns = MappingColumnsConfig(
            col_rvi_cm_id_rvi="RVI",
            col_rvi_cm_id_cm="CM",
            col_rvi_cm_id_sistema="SIS",
        )
        svc = MappingService(
            _FakeSource([{"SIS": "RVI2", "RVI": "0001", "CM": "DC01"}]),  # type: ignore[arg-type]
            columns,
            type_manifest=manifest,
        )
        assert svc.get_mapping("0001", "rvi2").id_corto == "DC01"

    def test_blank_id_rvi_rows_are_skipped(self) -> None:
        manifest = _manifest(_entry("DC01"))
        svc = MappingService(
            _FakeSource(_rows(("", "", "DC01"), ("", "0001", "DC01"))),  # type: ignore[arg-type]
            type_manifest=manifest,
        )
        assert svc.count() == 1


class TestManifestModeMissingCodes:
    def test_unknown_id_cm_is_dropped_and_collected(self, caplog: pytest.LogCaptureFixture) -> None:
        """Escenario 3 del spec."""
        manifest = _manifest(_entry("DC01"))
        with caplog.at_level(logging.WARNING):
            svc = MappingService(
                _FakeSource(  # type: ignore[arg-type]
                    _rows(("", "0001", "DC01"), ("", "0002", "ZZ99"))
                ),
                type_manifest=manifest,
            )
        assert svc.count() == 1
        assert "0002" not in svc
        assert svc.missing_cm_codes == ("ZZ99",)
        assert any("ZZ99" in r.getMessage() for r in caplog.records)

    def test_missing_codes_are_sorted_and_deduped(self) -> None:
        manifest = _manifest(_entry("DC01"))
        svc = MappingService(
            _FakeSource(  # type: ignore[arg-type]
                _rows(("", "0001", "ZZ99"), ("", "0002", "AA00"), ("", "0003", "ZZ99"))
            ),
            type_manifest=manifest,
        )
        assert svc.missing_cm_codes == ("AA00", "ZZ99")

    def test_other_modes_report_no_missing_codes(self) -> None:
        rows: list[dict[str, object]] = [
            {
                "ID CLASE DOCUMENTAL": "01.01",
                "ID RVI": "FF17",
                "ID Corto": "CN01",
                "CLASE DOCUMENTAL": "Clase",
                "METADATOS": "CIF",
            }
        ]
        svc = MappingService(_FakeSource(rows))  # type: ignore[arg-type]
        assert svc.missing_cm_codes == ()


class TestManifestModeExclusivity:
    def test_manifest_and_metadata_source_are_mutually_exclusive(self) -> None:
        manifest = _manifest(_entry("DC01"))
        with pytest.raises(ConfigurationError):
            MappingService(
                _FakeSource(_rows(("", "0001", "DC01"))),  # type: ignore[arg-type]
                metadata_source=_FakeSource([]),  # type: ignore[arg-type]
                type_manifest=manifest,
            )

    def test_system_id_is_ignored_in_consolidated_mode(self) -> None:
        rows: list[dict[str, object]] = [
            {
                "ID CLASE DOCUMENTAL": "01.01",
                "ID RVI": "FF17",
                "ID Corto": "CN01",
                "CLASE DOCUMENTAL": "Clase",
                "METADATOS": "CIF",
            }
        ]
        svc = MappingService(_FakeSource(rows))  # type: ignore[arg-type]
        assert svc.get_mapping("FF17", "cualquiera").id_corto == "CN01"
