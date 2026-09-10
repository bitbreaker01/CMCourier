"""Tests unitarios de ``types check`` (145 REQ-005).

El chequeo cruza las TRES fuentes de verdad que tienen que estar
alineadas para que un upload no explote en producción: el manifest
(lo que el servidor publica), el YAML (``metadata.field_sources``, de
dónde sale el VALOR de cada propiedad) y ``MapeoRVI_CM.csv`` (qué
código RVI va a qué clase).

Todo offline: el SUT es lógica pura sobre tres estructuras en memoria.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping

import pytest

from cmcourier.domain.cm_types import (
    DECISION_OMIT,
    DECISION_USE,
    CmPropertyDef,
    CmTypeEntry,
    CmTypeManifest,
)
from cmcourier.services.manifest_check import CheckReport, run_manifest_check
from cmcourier.services.mapping import MappingService
from cmcourier.services.metadata import FieldSourceConfig, SourceConfig, ValidationConfig

pytestmark = pytest.mark.unit


class _FakeSource:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def get_all(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def close(self) -> None:  # pragma: no cover — completitud de protocolo
        pass


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
    id_corto: str = "DC01",
    *,
    props: tuple[CmPropertyDef, ...] = (),
    decisions: dict[str, str] | None = None,
    reviewed: bool = True,
    folder_ok: bool | None = True,
    missing_on_server: bool = False,
) -> CmTypeEntry:
    return CmTypeEntry(
        id_corto=id_corto,
        type_id=f"$t!-2_BAC_{id_corto}v-1",
        local_name=f"BAC_{id_corto}",
        display_name=f"{id_corto} - Clase",
        folder=f"/$type/BAC_{id_corto}",
        folder_ok=folder_ok,
        properties=props,
        decisions=decisions if decisions is not None else {p.id: DECISION_USE for p in props},
        reviewed=reviewed,
        missing_on_server=missing_on_server,
    )


def _manifest(*entries: CmTypeEntry, duplicates: tuple[CmTypeEntry, ...] = ()) -> CmTypeManifest:
    return CmTypeManifest(
        service_url="http://cm.test/browser",
        repository_id="repo",
        discovered_at="2026-01-01T00:00:00Z",
        types={e.id_corto: e for e in entries},
        duplicates=duplicates,
    )


def _mapping(manifest: CmTypeManifest, *codes: str) -> MappingService:
    rows: list[dict[str, object]] = [
        {"IDSistema": "", "IDRVI": f"{i:04d}", "IDCM": code} for i, code in enumerate(codes, 1)
    ]
    return MappingService(_FakeSource(rows), type_manifest=manifest)  # type: ignore[arg-type]


def _source(pattern: str | None = None) -> SourceConfig:
    return SourceConfig(
        source_type="trigger",
        lookup_value_column="cif",
        validation=ValidationConfig(allowed_pattern=pattern) if pattern else None,
    )


def _fixed(value: str) -> FieldSourceConfig:
    """Un campo cuyo valor es fijo: sin cadena de fallback, sólo default."""
    return FieldSourceConfig(sources=(), default_value=value)


def _severities(report: CheckReport, message_part: str) -> list[str]:
    return [f.severity for f in report.findings if message_part in f.message]


# ---------------------------------------------------------------------------
# CRITICAL
# ---------------------------------------------------------------------------


class TestCritical:
    def test_missing_cm_code_is_critical(self) -> None:
        """Escenario 3 del spec."""
        manifest = _manifest(_entry("DC01"))
        mapping = _mapping(manifest, "DC01", "ZZ99")
        report = run_manifest_check(mapping, manifest, {})
        assert mapping.missing_cm_codes == ("ZZ99",)
        assert report.has_critical
        assert any(f.id_corto == "ZZ99" for f in report.criticals)

    def test_used_property_without_field_source_is_critical(self) -> None:
        """Escenario 4 del spec."""
        prop = _prop("clbNonGroup.BAC_CIF")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {})
        assert report.has_critical
        critical = report.criticals[0]
        assert critical.id_corto == "DC01"
        assert critical.prop_id == "clbNonGroup.BAC_CIF"

    def test_used_property_with_field_source_is_clean(self) -> None:
        """Escenario 4 del spec, la otra mitad."""
        prop = _prop("clbNonGroup.BAC_CIF")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        field_sources = {"BAC_CIF": FieldSourceConfig(sources=(_source(),))}
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)
        assert not report.has_critical
        assert report.findings == ()

    def test_missing_on_server_is_critical(self) -> None:
        manifest = _manifest(_entry("DC01", missing_on_server=True))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {})
        assert report.has_critical
        assert any("servidor" in f.message for f in report.criticals)

    def test_omitted_property_needs_no_field_source(self) -> None:
        prop = _prop("clbNonGroup.BAC_CIF", required=False)
        manifest = _manifest(_entry("DC01", props=(prop,), decisions={prop.id: DECISION_OMIT}))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {})
        assert report.findings == ()


# ---------------------------------------------------------------------------
# WARNING
# ---------------------------------------------------------------------------


class TestWarning:
    def test_unreviewed_type_warns(self) -> None:
        manifest = _manifest(_entry("DC01", reviewed=False))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {})
        assert not report.has_critical
        assert any("revis" in f.message for f in report.warnings)

    def test_folder_not_found_warns(self) -> None:
        manifest = _manifest(_entry("DC01", folder_ok=False))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {})
        assert any("carpeta" in f.message for f in report.warnings)

    def test_unverified_folder_does_not_warn(self) -> None:
        manifest = _manifest(_entry("DC01", folder_ok=None))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {})
        assert report.findings == ()

    def test_required_without_default_marked_omit_warns(self) -> None:
        prop = _prop("clbNonGroup.BAC_CIF", required=True, default_value=None)
        manifest = _manifest(_entry("DC01", props=(prop,), decisions={prop.id: DECISION_OMIT}))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {})
        assert [f.severity for f in report.findings] == ["WARNING"]
        assert report.warnings[0].prop_id == "clbNonGroup.BAC_CIF"

    def test_required_with_default_marked_omit_is_fine(self) -> None:
        prop = _prop("clbNonGroup.BAC_CIF", required=True, default_value="X")
        manifest = _manifest(_entry("DC01", props=(prop,), decisions={prop.id: DECISION_OMIT}))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {})
        assert report.findings == ()


class TestWarningMaxLength:
    def _report(self, pattern: str, max_length: int) -> CheckReport:
        prop = _prop("clbNonGroup.BAC_CIF", max_length=max_length)
        manifest = _manifest(_entry("DC01", props=(prop,)))
        field_sources = {"BAC_CIF": FieldSourceConfig(sources=(_source(pattern),))}
        return run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)

    def test_pattern_longer_than_cm_max_length_warns(self) -> None:
        report = self._report(r"^\d{20}$", 10)
        assert _severities(report, "max_length") == ["WARNING"]

    def test_pattern_within_cm_max_length_is_clean(self) -> None:
        assert self._report(r"^\d{8}$", 10).findings == ()

    def test_range_pattern_uses_the_upper_bound(self) -> None:
        assert self._report(r"^.{1,8}$", 10).findings == ()
        assert _severities(self._report(r"^.{1,30}$", 10), "max_length") == ["WARNING"]

    def test_unparseable_pattern_is_skipped(self) -> None:
        assert self._report(r"^[A-Z]+-\d+$", 2).findings == ()

    def test_fixed_value_longer_than_max_length_warns(self) -> None:
        prop = _prop("clbNonGroup.BAC_CIF", max_length=3)
        manifest = _manifest(_entry("DC01", props=(prop,)))
        field_sources = {"BAC_CIF": _fixed("DEMASIADO_LARGO")}
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)
        assert _severities(report, "max_length") == ["WARNING"]


class TestWarningFixedValueParsing:
    def _report(self, property_type: str, value: str) -> CheckReport:
        prop = _prop("clbNonGroup.BAC_Fecha", property_type=property_type)
        manifest = _manifest(_entry("DC01", props=(prop,)))
        field_sources = {"BAC_Fecha": _fixed(value)}
        return run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)

    @pytest.mark.parametrize(
        ("property_type", "value"),
        [
            ("integer", "no-es-un-numero"),
            ("boolean", "quizás"),
            ("datetime", "ayer"),
        ],
    )
    def test_unparseable_fixed_value_warns(self, property_type: str, value: str) -> None:
        report = self._report(property_type, value)
        assert [f.severity for f in report.findings] == ["WARNING"]

    @pytest.mark.parametrize(
        ("property_type", "value"),
        [
            ("integer", "42"),
            ("boolean", "true"),
            ("datetime", "2026-01-01T00:00:00"),
        ],
    )
    def test_parseable_fixed_value_is_clean(self, property_type: str, value: str) -> None:
        assert self._report(property_type, value).findings == ()

    def test_string_property_never_warns_about_parsing(self) -> None:
        assert self._report("string", "cualquier cosa").findings == ()

    def test_a_looked_up_value_is_not_a_fixed_value(self) -> None:
        prop = _prop("clbNonGroup.BAC_Fecha", property_type="datetime")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        field_sources = {"BAC_Fecha": FieldSourceConfig(sources=(_source(),), default_value="ayer")}
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)
        assert report.findings == ()


class TestIdCortoCompartido:
    """145 REQ-002: dos tipos CM con el mismo ID corto (visto en PRD: ``DC35``)."""

    def _dup(self, id_corto: str = "DC35") -> CmTypeEntry:
        return CmTypeEntry(
            id_corto=id_corto,
            type_id=f"$t!-2_BAC_{id_corto}_02v-1",
            local_name=f"BAC_{id_corto}_02",
            display_name=f"{id_corto} - El otro",
            folder=f"/$type/BAC_{id_corto}_02",
        )

    def test_duplicate_of_an_unmapped_code_warns(self) -> None:
        manifest = _manifest(_entry("DC01"), _entry("DC35"), duplicates=(self._dup(),))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {})
        assert not report.has_critical
        assert _severities(report, "ID corto compartido") == ["WARNING"]

    def test_duplicate_of_a_mapped_code_is_critical(self) -> None:
        manifest = _manifest(_entry("DC35"), duplicates=(self._dup(),))
        report = run_manifest_check(_mapping(manifest, "DC35"), manifest, {})
        assert report.has_critical
        assert _severities(report, "ID corto compartido") == ["CRITICAL"]

    def test_the_finding_names_both_types_and_the_way_out(self) -> None:
        manifest = _manifest(_entry("DC35"), duplicates=(self._dup(),))
        report = run_manifest_check(_mapping(manifest, "DC35"), manifest, {})
        finding = next(f for f in report.findings if "ID corto compartido" in f.message)
        assert finding.id_corto == "DC35"
        assert finding.prop_id is None
        assert "$t!-2_BAC_DC35v-1" in finding.message
        assert "$t!-2_BAC_DC35_02v-1" in finding.message
        assert "DC35 - El otro" in finding.message
        assert "--type-id" in finding.message

    def test_one_finding_per_candidate(self) -> None:
        otro = CmTypeEntry(
            id_corto="DC35",
            type_id="$t!-2_BAC_DC35_03v-1",
            local_name="BAC_DC35_03",
            display_name="DC35 - El tercero",
            folder="/$type/BAC_DC35_03",
        )
        manifest = _manifest(_entry("DC35"), duplicates=(self._dup(), otro))
        report = run_manifest_check(_mapping(manifest, "DC35"), manifest, {})
        assert _severities(report, "ID corto compartido") == ["CRITICAL", "CRITICAL"]

    def test_a_manifest_without_duplicates_says_nothing(self) -> None:
        manifest = _manifest(_entry("DC35"))
        report = run_manifest_check(_mapping(manifest, "DC35"), manifest, {})
        assert report.findings == ()


# ---------------------------------------------------------------------------
# INFO
# ---------------------------------------------------------------------------


class TestInfo:
    def test_unused_field_source_is_info(self) -> None:
        prop = _prop("clbNonGroup.BAC_CIF")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        field_sources = {
            "BAC_CIF": FieldSourceConfig(sources=(_source(),)),
            "BAC_Huerfano": FieldSourceConfig(sources=(_source(),)),
        }
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)
        assert not report.has_critical
        assert len(report.infos) == 1
        assert "BAC_Huerfano" in report.infos[0].message

    def test_field_source_used_by_an_unmapped_type_is_still_info(self) -> None:
        used = _prop("clbNonGroup.BAC_CIF")
        other = _prop("clbNonGroup.BAC_Otro")
        manifest = _manifest(_entry("DC01", props=(used,)), _entry("DC02", props=(other,)))
        field_sources = {
            "BAC_CIF": FieldSourceConfig(sources=(_source(),)),
            "BAC_Otro": FieldSourceConfig(sources=(_source(),)),
        }
        # Sólo DC01 está mapeado: BAC_Otro no lo usa NINGÚN tipo mapeado.
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)
        assert len(report.infos) == 1
        assert "BAC_Otro" in report.infos[0].message


# ---------------------------------------------------------------------------
# CheckReport
# ---------------------------------------------------------------------------


class TestCheckReport:
    def _mixed(self) -> CheckReport:
        used = _prop("clbNonGroup.BAC_CIF")
        manifest = _manifest(_entry("DC01", props=(used,), reviewed=False))
        field_sources = {"BAC_Huerfano": FieldSourceConfig(sources=(_source(),))}
        return run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)

    def test_buckets_partition_the_findings(self) -> None:
        report = self._mixed()
        assert report.criticals and report.warnings and report.infos
        assert len(report.criticals) + len(report.warnings) + len(report.infos) == len(
            report.findings
        )

    def test_has_critical(self) -> None:
        assert self._mixed().has_critical is True
        assert CheckReport().has_critical is False

    def test_render_groups_by_severity(self) -> None:
        text = self._mixed().render()
        assert text.index("CRITICAL") < text.index("WARNING") < text.index("INFO")

    def test_render_of_a_clean_report_says_so(self) -> None:
        text = CheckReport().render()
        assert text
        assert "CRITICAL" not in text

    def test_to_json_dict_is_serializable(self) -> None:
        payload = self._mixed().to_json_dict()
        assert json.dumps(payload)
        assert payload["has_critical"] is True
        assert payload["counts"]["CRITICAL"] == len(self._mixed().criticals)
        assert isinstance(payload["findings"], list)
        assert set(payload["findings"][0]) == {"severity", "id_corto", "prop_id", "message"}


class TestDeterminism:
    def test_findings_are_stable_across_runs(self) -> None:
        props = (_prop("clbNonGroup.BAC_A"), _prop("clbNonGroup.BAC_B"))
        manifest = _manifest(_entry("DC02", props=props), _entry("DC01", props=props))
        field_sources: Mapping[str, FieldSourceConfig] = {}
        first = run_manifest_check(_mapping(manifest, "DC02", "DC01"), manifest, field_sources)
        second = run_manifest_check(_mapping(manifest, "DC01", "DC02"), manifest, field_sources)
        assert first.findings == second.findings
