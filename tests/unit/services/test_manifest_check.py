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
from cmcourier.services.manifest_check import CheckReport, Scope, run_manifest_check
from cmcourier.services.mapping import MappingService
from cmcourier.services.metadata import (
    FieldSourceConfig,
    PadConfig,
    SourceConfig,
    ValidationConfig,
    ValueFormat,
)

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
    choices: tuple[str, ...] = (),
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
        choices=choices,
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


class TestAlcanceDeAuditoria145:
    """``scope`` decide QUÉ tipos se auditan (145 REQ-005).

    El operador revisa el manifest ENTERO en la pestaña ``M·MODELO`` y
    marca ``revisado ✓`` a medida que avanza. Un tipo revisado con una
    propiedad ``usar`` sin ``field_sources`` es un ``ConfigurationError``
    garantizado el día que lo mapee: auditar sólo lo que el CSV
    referencia lo deja pasar en silencio.
    """

    _FIELD_SOURCES: Mapping[str, FieldSourceConfig] = {
        "BAC_CIF": FieldSourceConfig(sources=(_source(),))
    }

    def _manifest(self, *, reviewed: bool) -> CmTypeManifest:
        """``DC01`` mapeado y sano; ``AF01`` sin mapear y roto."""
        return _manifest(
            _entry("DC01", props=(_prop("clbNonGroup.BAC_CIF"),)),
            _entry("AF01", props=(_prop("clbNonGroup.BAC_Sucursal"),), reviewed=reviewed),
        )

    def _report(self, *, reviewed: bool = True, scope: Scope = "reviewed") -> CheckReport:
        manifest = self._manifest(reviewed=reviewed)
        return run_manifest_check(
            _mapping(manifest, "DC01"), manifest, self._FIELD_SOURCES, scope=scope
        )

    def _codes(self, report: CheckReport) -> list[str | None]:
        return [f.id_corto for f in report.findings]

    def test_the_default_scope_audits_a_reviewed_but_unmapped_type(self) -> None:
        """El caso del operador: ``AF01`` revisado, todavía sin mapear."""
        report = self._report()
        assert report.has_critical
        assert report.criticals[0].id_corto == "AF01"
        assert "field_sources.BAC_Sucursal" in report.criticals[0].message

    def test_the_mapped_scope_keeps_the_old_behaviour(self) -> None:
        report = self._report(scope="mapped")
        assert not report.has_critical
        assert "AF01" not in self._codes(report)

    def test_the_all_scope_audits_an_unreviewed_unmapped_type(self) -> None:
        report = self._report(reviewed=False, scope="all")
        assert report.has_critical
        assert report.criticals[0].id_corto == "AF01"

    def test_the_default_scope_ignores_an_unreviewed_unmapped_type(self) -> None:
        """Sin ``revisado ✓`` el operador no declaró que lo piensa usar."""
        report = self._report(reviewed=False)
        assert "AF01" not in self._codes(report)

    @pytest.mark.parametrize("scope", ["mapped", "reviewed", "all"])
    def test_the_unreviewed_warning_never_fires_for_an_unmapped_type(self, scope: Scope) -> None:
        """Bajo ``all`` serían cientos de WARNING inútiles; bajo ``reviewed``, tautología."""
        report = self._report(reviewed=False, scope=scope)
        assert not [
            f for f in report.warnings if f.id_corto == "AF01" and "no fue revisado" in f.message
        ]

    def test_the_unreviewed_warning_still_fires_for_a_mapped_type(self) -> None:
        manifest = _manifest(_entry("DC01", reviewed=False))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, {}, scope="all")
        assert _severities(report, "todavía no fue revisado") == ["WARNING"]

    def test_mapped_codes_come_first_and_both_halves_are_sorted(self) -> None:
        """El orden viejo no se reordena: mapeados (ordenados), después los extra."""
        prop = _prop("clbNonGroup.BAC_Falta")
        manifest = _manifest(
            _entry("DC02", props=(prop,)),
            _entry("DC01", props=(prop,)),
            _entry("AB01", props=(prop,)),
            _entry("AA01", props=(prop,)),
        )
        report = run_manifest_check(_mapping(manifest, "DC02", "DC01"), manifest, {})
        assert [f.id_corto for f in report.criticals] == ["DC01", "DC02", "AA01", "AB01"]

    def test_the_info_set_difference_uses_the_audited_set(self) -> None:
        """Una entrada que sólo usa un tipo revisado-sin-mapear ya no sobra."""
        manifest = _manifest(
            _entry("DC01", props=(_prop("clbNonGroup.BAC_CIF"),)),
            _entry("AF01", props=(_prop("clbNonGroup.BAC_Sucursal"),)),
        )
        field_sources = {
            "BAC_CIF": FieldSourceConfig(sources=(_source(),)),
            "BAC_Sucursal": FieldSourceConfig(sources=(_source(),)),
        }
        assert run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources).infos == ()

    def test_the_info_message_says_auditado(self) -> None:
        """Ya no es "ningún tipo mapeado": el conjunto es más grande."""
        manifest = _manifest(_entry("DC01"))
        field_sources = {"BAC_Huerfano": FieldSourceConfig(sources=(_source(),))}
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)
        assert "no lo usa ningún tipo auditado" in report.infos[0].message


class TestAliasColgado145:
    """Un alias cuyo destino no existe se denuncia SIEMPRE (145 REQ-005).

    El CRITICAL viejo es por-propiedad: sólo sale si un tipo auditado
    consulta el alias. El operador que renombró la entrada de
    ``field_sources`` y se olvidó del alias no consulta nada, y el YAML
    se queda con basura que revienta el día que alguien la use.
    """

    def _report(
        self,
        aliases: Mapping[str, str],
        *,
        props: tuple[CmPropertyDef, ...] = (),
    ) -> CheckReport:
        manifest = _manifest(_entry("DC01", props=props))
        field_sources = {"BAC_Shortname": FieldSourceConfig(sources=(_source(),))}
        return run_manifest_check(
            _mapping(manifest, "DC01"), manifest, field_sources, field_aliases=aliases
        )

    def test_a_dangling_alias_warns_with_nobody_consulting_it(self) -> None:
        """El caso real: ``Shortname: BAC_Short_Name`` quedó apuntando al vacío."""
        report = self._report({"Shortname": "BAC_Short_Name"})
        assert not report.has_critical
        assert len(report.warnings) == 1
        warning = report.warnings[0]
        assert warning.id_corto is None
        assert warning.prop_id is None
        assert (
            warning.message
            == "metadata.field_aliases.Shortname apunta a field_sources.BAC_Short_Name, "
            "que no existe"
        )

    def test_a_resolvable_alias_does_not_warn(self) -> None:
        assert self._report({"Shortname": "BAC_Shortname"}).warnings == ()

    def test_the_warnings_are_sorted_by_alias(self) -> None:
        report = self._report(
            {"Num_Cuenta_Tarjeta": "BAC_CuentaTarjeta", "Shortname": "BAC_Short_Name"}
        )
        assert [f.message.split(".")[2].split(" ")[0] for f in report.warnings] == [
            "Num_Cuenta_Tarjeta",
            "Shortname",
        ]

    def test_the_warnings_land_before_the_info_block(self) -> None:
        report = self._report({"Shortname": "BAC_Short_Name"})
        severities = [f.severity for f in report.findings]
        assert severities == ["WARNING", "INFO"]

    def test_a_consulted_dangling_alias_gets_both_findings(self) -> None:
        """CRITICAL por-propiedad + WARNING por-alias: distinto foco, ambos válidos."""
        report = self._report(
            {"BAC_Falta": "BAC_No_Existe"}, props=(_prop("clbNonGroup.BAC_Falta"),)
        )
        assert len(report.criticals) == 1
        assert report.criticals[0].prop_id == "clbNonGroup.BAC_Falta"
        assert [f.message for f in report.warnings] == [
            "metadata.field_aliases.BAC_Falta apunta a field_sources.BAC_No_Existe, que no existe"
        ]


class TestAliasDeCampos145:
    """``metadata.field_aliases`` también resuelve una propiedad del manifest.

    El runtime (``MetadataService._normalize_fields_with_friendly``) acepta
    un nombre de campo que sea llave de ``field_sources`` O llave de
    ``field_aliases`` (case-insensitive), y en ese caso el VALOR del alias
    es la llave real de ``field_sources``. El chequeo tiene que mirar lo
    mismo, si no le grita CRITICAL a una config que sube perfecto.
    """

    _ALIASED = {"BAC_Short_Name": FieldSourceConfig(sources=(_source(),))}

    def _report(
        self,
        *,
        prop: CmPropertyDef,
        field_sources: Mapping[str, FieldSourceConfig] | None = None,
        aliases: Mapping[str, str],
    ) -> CheckReport:
        manifest = _manifest(_entry("DC01", props=(prop,)))
        return run_manifest_check(
            _mapping(manifest, "DC01"),
            manifest,
            self._ALIASED if field_sources is None else field_sources,
            field_aliases=aliases,
        )

    def test_a_property_resolved_through_an_alias_is_clean(self) -> None:
        """El caso del operador: arregla el drift con un alias y no hay drama."""
        report = self._report(
            prop=_prop("clbNonGroup.BAC_Shortname"),
            aliases={"BAC_Shortname": "BAC_Short_Name"},
        )
        assert not report.has_critical
        assert report.findings == ()

    def test_the_alias_lookup_is_case_insensitive(self) -> None:
        """Igual que el runtime: ``{k.lower(): v}`` contra ``raw.lower()``."""
        report = self._report(
            prop=_prop("clbNonGroup.BAC_SHORTNAME"),
            aliases={"bac_shortname": "BAC_Short_Name"},
        )
        assert report.findings == ()

    def test_a_dangling_alias_is_critical(self) -> None:
        """El alias apunta a una entrada que no existe: el runtime revienta."""
        report = self._report(
            prop=_prop("clbNonGroup.BAC_Shortname"),
            field_sources={},
            aliases={"BAC_Shortname": "BAC_No_Existe"},
        )
        assert report.has_critical
        critical = report.criticals[0]
        assert critical.id_corto == "DC01"
        assert critical.prop_id == "clbNonGroup.BAC_Shortname"
        assert "el alias BAC_Shortname apunta a field_sources.BAC_No_Existe" in critical.message
        assert "que no existe" in critical.message

    def test_a_dangling_alias_is_not_the_plain_missing_entry_message(self) -> None:
        """Los dos son CRITICAL pero el operador arregla cosas distintas."""
        prop = _prop("clbNonGroup.BAC_Shortname")
        colgado = self._report(
            prop=prop, field_sources={}, aliases={"BAC_Shortname": "BAC_No_Existe"}
        )
        sin_entrada = self._report(prop=prop, field_sources={}, aliases={})
        assert "sin entrada metadata.field_sources" in sin_entrada.criticals[0].message
        assert "sin entrada metadata.field_sources" not in colgado.criticals[0].message

    def test_the_max_length_warning_names_the_resolved_key(self) -> None:
        """El operador edita ``field_sources.BAC_Short_Name``, no el alias."""
        report = self._report(
            prop=_prop("clbNonGroup.BAC_Shortname", max_length=3),
            field_sources={"BAC_Short_Name": _fixed("DEMASIADO_LARGO")},
            aliases={"BAC_Shortname": "BAC_Short_Name"},
        )
        assert _severities(report, "max_length") == ["WARNING"]
        assert "field_sources.BAC_Short_Name declara largo" in report.warnings[0].message

    def test_the_typed_fixed_value_warning_names_the_resolved_key(self) -> None:
        report = self._report(
            prop=_prop("clbNonGroup.BAC_Fecha_Alias", property_type="datetime"),
            field_sources={"BAC_Fecha": _fixed("ayer")},
            aliases={"BAC_Fecha_Alias": "BAC_Fecha"},
        )
        assert [f.severity for f in report.findings] == ["WARNING"]
        assert "field_sources.BAC_Fecha tiene un valor fijo" in report.warnings[0].message

    def test_a_field_source_reached_through_an_alias_is_not_info(self) -> None:
        """El INFO cuenta la llave RESUELTA, no el nombre de la propiedad."""
        report = self._report(
            prop=_prop("clbNonGroup.BAC_Shortname"),
            aliases={"BAC_Shortname": "BAC_Short_Name"},
        )
        assert report.infos == ()

    def test_an_alias_that_nobody_uses_still_leaves_its_target_as_info(self) -> None:
        manifest = _manifest(_entry("DC01"))
        report = run_manifest_check(
            _mapping(manifest, "DC01"),
            manifest,
            self._ALIASED,
            field_aliases={"BAC_Shortname": "BAC_Short_Name"},
        )
        assert len(report.infos) == 1
        assert "BAC_Short_Name" in report.infos[0].message

    def test_without_aliases_the_behaviour_is_the_one_of_always(self) -> None:
        """El parámetro es keyword-only con default: los `callers` viejos siguen."""
        prop = _prop("clbNonGroup.BAC_Shortname")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, self._ALIASED)
        assert _severities(report, "sin entrada metadata.field_sources.BAC_Shortname") == [
            "CRITICAL"
        ]


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

    def test_field_source_used_by_an_unaudited_type_is_still_info(self) -> None:
        used = _prop("clbNonGroup.BAC_CIF")
        other = _prop("clbNonGroup.BAC_Otro")
        manifest = _manifest(
            _entry("DC01", props=(used,)), _entry("DC02", props=(other,), reviewed=False)
        )
        field_sources = {
            "BAC_CIF": FieldSourceConfig(sources=(_source(),)),
            "BAC_Otro": FieldSourceConfig(sources=(_source(),)),
        }
        # DC02 no está mapeado NI revisado: fuera del alcance auditado.
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


# ---------------------------------------------------------------------------
# 146 — el `format` declarativo cruzado con lo que declara CM
# ---------------------------------------------------------------------------


def _fmt_field(
    fmt: ValueFormat | None = None,
    *,
    source_fmt: ValueFormat | None = None,
    pattern: str | None = None,
) -> FieldSourceConfig:
    """Un campo con una fuente, con `format` por campo y/o por fuente."""
    source = SourceConfig(
        source_type="trigger",
        lookup_value_column="cif",
        validation=ValidationConfig(allowed_pattern=pattern) if pattern else None,
        format=source_fmt,
    )
    return FieldSourceConfig(sources=(source,), format=fmt)


def _check(prop: CmPropertyDef, fsc: FieldSourceConfig) -> CheckReport:
    manifest = _manifest(_entry("DC01", props=(prop,)))
    name = prop.id.split(".")[-1]
    return run_manifest_check(_mapping(manifest, "DC01"), manifest, {name: fsc})


class TestWarningFormatLength:
    """El largo que DECLARA el formato contra el `max_length` de CM."""

    def test_pad_left_mas_largo_que_max_length_avisa(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=6),
            _fmt_field(ValueFormat(pad_left=PadConfig(width=9, char="0"))),
        )
        assert _severities(report, "max_length") == ["WARNING"]
        assert "field_sources.BAC_CIF formatea a 9 y el max_length de CM es 6" in (
            report.warnings[0].message
        )

    def test_pad_right_mas_largo_que_max_length_avisa(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=10),
            _fmt_field(ValueFormat(pad_right=PadConfig(width=20, char=" "))),
        )
        assert "formatea a 20" in report.warnings[0].message

    def test_toma_el_mayor_de_los_dos_pads(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=10),
            _fmt_field(
                ValueFormat(
                    pad_left=PadConfig(width=12, char="0"),
                    pad_right=PadConfig(width=20, char=" "),
                )
            ),
        )
        assert "formatea a 20" in report.warnings[0].message

    def test_truncate_gana_sobre_los_pads(self) -> None:
        # Con `truncate` el largo es un TOPE, y ese tope es el que manda.
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=10),
            _fmt_field(ValueFormat(pad_left=PadConfig(width=9, char="0"), truncate=30)),
        )
        assert "formatea a 30" in report.warnings[0].message

    def test_formato_que_entra_en_max_length_no_avisa(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=9),
            _fmt_field(ValueFormat(pad_left=PadConfig(width=9, char="0"))),
        )
        assert report.findings == ()

    def test_formato_sin_largo_declarado_no_avisa(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=3),
            _fmt_field(ValueFormat(trim=True, case="lower")),
        )
        assert report.findings == ()

    def test_propiedad_sin_max_length_no_avisa(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF"),
            _fmt_field(ValueFormat(pad_left=PadConfig(width=99, char="0"))),
        )
        assert report.findings == ()

    def test_el_formato_por_fuente_tambien_declara_largo(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=6),
            _fmt_field(source_fmt=ValueFormat(pad_left=PadConfig(width=9, char="0"))),
        )
        assert "formatea a 9" in report.warnings[0].message

    def test_el_formato_por_campo_manda_sobre_el_de_la_fuente(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=6),
            _fmt_field(
                ValueFormat(truncate=8),
                source_fmt=ValueFormat(pad_left=PadConfig(width=20, char="0")),
            ),
        )
        assert _severities(report, "max_length") == ["WARNING"]
        assert "formatea a 8" in report.warnings[0].message

    def test_el_warning_nombra_la_llave_resuelta_por_alias(self) -> None:
        prop = _prop("clbNonGroup.BAC_Shortname", max_length=3)
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01"),
            manifest,
            {"BAC_Short_Name": _fmt_field(ValueFormat(pad_right=PadConfig(width=9, char=" ")))},
            field_aliases={"BAC_Shortname": "BAC_Short_Name"},
        )
        assert "field_sources.BAC_Short_Name formatea a 9" in report.warnings[0].message


class TestFormatReemplazaAlPatron:
    """Cuando el `format` declara un largo, el patrón ya no manda sobre el
    largo de salida: nunca los dos warnings para la misma propiedad."""

    def test_solo_sale_el_warning_del_formato(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=6),
            _fmt_field(ValueFormat(pad_left=PadConfig(width=9, char="0")), pattern=r"^\d{20}$"),
        )
        assert _severities(report, "max_length") == ["WARNING"]
        assert "formatea a 9" in report.warnings[0].message
        assert "declara largo" not in report.warnings[0].message

    def test_el_formato_corto_calla_al_patron_largo(self) -> None:
        # El patrón admite 20 pero el formato recorta a 6: no hay hallazgo.
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=6),
            _fmt_field(ValueFormat(truncate=6), pattern=r"^\d{20}$"),
        )
        assert report.findings == ()

    def test_sin_format_el_patron_sigue_mandando(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_CIF", max_length=6),
            _fmt_field(pattern=r"^\d{20}$"),
        )
        assert "field_sources.BAC_CIF declara largo 20" in report.warnings[0].message


class TestWarningCaseUpperContraChoices:
    def test_upper_sin_ninguna_opcion_en_mayusculas_avisa(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_Tipo", choices=("Factura", "Recibo")),
            _fmt_field(ValueFormat(case="upper")),
        )
        assert [f.severity for f in report.findings] == ["WARNING"]
        assert "field_sources.BAC_Tipo" in report.warnings[0].message
        assert "mayúsculas" in report.warnings[0].message

    def test_una_sola_opcion_en_mayusculas_alcanza(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_Tipo", choices=("Factura", "RECIBO")),
            _fmt_field(ValueFormat(case="upper")),
        )
        assert report.findings == ()

    def test_una_opcion_sin_letras_cuenta_como_mayuscula(self) -> None:
        # "01" == "01".upper(): un valor en mayúsculas todavía puede matchear.
        report = _check(
            _prop("clbNonGroup.BAC_Tipo", choices=("01", "Factura")),
            _fmt_field(ValueFormat(case="upper")),
        )
        assert report.findings == ()

    def test_sin_choices_no_avisa(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_Tipo", choices=()),
            _fmt_field(ValueFormat(case="upper")),
        )
        assert report.findings == ()

    def test_case_lower_no_avisa(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_Tipo", choices=("Factura", "Recibo")),
            _fmt_field(ValueFormat(case="lower")),
        )
        assert report.findings == ()

    def test_el_upper_por_fuente_tambien_cuenta(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_Tipo", choices=("Factura", "Recibo")),
            _fmt_field(source_fmt=ValueFormat(case="upper")),
        )
        assert [f.severity for f in report.findings] == ["WARNING"]

    def test_el_case_por_campo_pisa_al_de_la_fuente(self) -> None:
        # La fuente sube a mayúsculas pero el campo baja a minúsculas: lo
        # que llega a CM es minúscula, no hay nada que avisar.
        report = _check(
            _prop("clbNonGroup.BAC_Tipo", choices=("Factura", "Recibo")),
            _fmt_field(ValueFormat(case="lower"), source_fmt=ValueFormat(case="upper")),
        )
        assert report.findings == ()

    def test_sin_format_no_avisa(self) -> None:
        report = _check(
            _prop("clbNonGroup.BAC_Tipo", choices=("Factura", "Recibo")),
            _fmt_field(),
        )
        assert report.findings == ()


# ---------------------------------------------------------------------------
# 149 — el default contra los `allowed_pattern` de sus fuentes
#
# 149 REQ-001 sacó del RUNTIME la validación del `default_value` (que se
# hacía contra el patrón de la PRIMERA fuente: magia implícita y
# acoplamiento posicional). Lo que se pierde ahí se gana acá, que es donde
# corresponde: el operador está sentado, con tiempo, leyendo un reporte.
#
# INFO y no WARNING a propósito: un default deliberadamente distinto de los
# datos reales —un marcador como `000000` que después se busca y se
# corrige— es una técnica legítima. El check informa, no juzga.
# ---------------------------------------------------------------------------


def _default_field(
    default: str | None,
    *patterns: str | None,
    fmt: ValueFormat | None = None,
) -> FieldSourceConfig:
    """Un campo con una fuente por patrón (``None`` = fuente sin validación)."""
    return FieldSourceConfig(
        sources=tuple(_source(p) for p in patterns),
        default_value=default,
        format=fmt,
    )


def _infos(report: CheckReport, part: str) -> list[str]:
    return [f.message for f in report.infos if part in f.message]


class TestInfoDefaultSinPatron149:
    def test_el_default_que_no_matchea_ningun_patron_informa(self) -> None:
        # El caso del operador: seis ceros contra catorce-a-dieciséis.
        prop = _prop("clbNonGroup.BAC_Num_Cuenta_Tarjeta")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01"),
            manifest,
            {"BAC_Num_Cuenta_Tarjeta": _default_field("000000", r"^[0-9]{14,16}$")},
        )
        assert not report.has_critical
        assert report.infos[0].severity == "INFO"
        assert report.infos[0].message == (
            "metadata.field_sources.BAC_Num_Cuenta_Tarjeta: el default '000000' no "
            "matchea ningún allowed_pattern de sus fuentes ('^[0-9]{14,16}$')"
        )

    def test_lista_los_patrones_en_orden_de_fuente(self) -> None:
        prop = _prop("clbNonGroup.BAC_X")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01"),
            manifest,
            {"BAC_X": _default_field("zzz", r"^\d{6}$", None, r"^[A-Z]{3}$")},
        )
        assert _infos(report, "BAC_X: el default") == [
            "metadata.field_sources.BAC_X: el default 'zzz' no matchea ningún "
            "allowed_pattern de sus fuentes ('^\\d{6}$', '^[A-Z]{3}$')"
        ]

    def test_un_default_que_matchea_alguno_no_informa(self) -> None:
        # Alcanza con UNA fuente: el default no tiene por qué matchearlas todas.
        prop = _prop("clbNonGroup.BAC_X")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01"),
            manifest,
            {"BAC_X": _default_field("ABC", r"^\d{6}$", r"^[A-Z]{3}$")},
        )
        assert report.findings == ()

    def test_ninguna_fuente_con_patron_no_informa(self) -> None:
        prop = _prop("clbNonGroup.BAC_X")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01"), manifest, {"BAC_X": _default_field("000000", None, None)}
        )
        assert report.findings == ()

    def test_sin_default_no_informa(self) -> None:
        prop = _prop("clbNonGroup.BAC_X")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01"), manifest, {"BAC_X": _default_field(None, r"^\d{6}$")}
        )
        assert report.findings == ()

    def test_el_default_se_compara_ya_formateado(self) -> None:
        # 146 REQ-003 + 149: el `format` de campo se le aplica al default,
        # así que el check tiene que juzgar el valor que SALE, no el crudo.
        # "0" + pad_left(9) = "000000000", que sí matchea ^\d{9}$.
        prop = _prop("clbNonGroup.BAC_X")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01"),
            manifest,
            {
                "BAC_X": _default_field(
                    "0", r"^\d{9}$", fmt=ValueFormat(pad_left=PadConfig(width=9, char="0"))
                )
            },
        )
        assert report.findings == ()

    def test_el_mensaje_nombra_el_valor_formateado(self) -> None:
        prop = _prop("clbNonGroup.BAC_X")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01"),
            manifest,
            {
                "BAC_X": _default_field(
                    "ABC", r"^\d{9}$", fmt=ValueFormat(pad_left=PadConfig(width=9, char="0"))
                )
            },
        )
        assert "el default '000000ABC'" in report.infos[0].message

    def test_se_emite_una_sola_vez_aunque_dos_tipos_usen_el_campo(self) -> None:
        # El hallazgo es sobre `field_sources`, no sobre un tipo auditado.
        prop = _prop("clbNonGroup.BAC_X")
        manifest = _manifest(_entry("DC01", props=(prop,)), _entry("DC02", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01", "DC02"),
            manifest,
            {"BAC_X": _default_field("000000", r"^[0-9]{14,16}$")},
        )
        assert len(_infos(report, "no matchea ningún allowed_pattern")) == 1

    def test_no_lleva_id_corto_ni_prop_id(self) -> None:
        prop = _prop("clbNonGroup.BAC_X")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        report = run_manifest_check(
            _mapping(manifest, "DC01"), manifest, {"BAC_X": _default_field("zz", r"^\d{6}$")}
        )
        assert (report.infos[0].id_corto, report.infos[0].prop_id) == (None, None)

    def test_un_campo_que_nadie_usa_igual_se_informa(self) -> None:
        # Como el WARNING de alias colgado: es un hallazgo del YAML, no del
        # cruce con el manifest. El INFO de "no lo usa ningún tipo" sale
        # además, y son dos cosas distintas que el operador arregla distinto.
        prop = _prop("clbNonGroup.BAC_CIF")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        field_sources = {
            "BAC_CIF": FieldSourceConfig(sources=(_source(),)),
            "BAC_Huerfano": _default_field("000000", r"^[0-9]{14,16}$"),
        }
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)
        assert len(_infos(report, "no matchea ningún allowed_pattern")) == 1
        assert len(_infos(report, "no lo usa ningún tipo auditado")) == 1

    def test_el_orden_es_estable_entre_campos(self) -> None:
        prop = _prop("clbNonGroup.BAC_CIF")
        manifest = _manifest(_entry("DC01", props=(prop,)))
        field_sources = {
            "BAC_Z": _default_field("zz", r"^\d{6}$"),
            "BAC_A": _default_field("aa", r"^\d{6}$"),
        }
        report = run_manifest_check(_mapping(manifest, "DC01"), manifest, field_sources)
        ordered = _infos(report, "no matchea ningún allowed_pattern")
        assert len(ordered) == 2
        assert "BAC_A" in ordered[0]
        assert "BAC_Z" in ordered[1]
