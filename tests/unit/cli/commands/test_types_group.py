"""145 REQ-004: el grupo ``cmcourier types`` (discover/show/diff/update/review).

Todo corre con un uploader falso (duck-typed: ``get_type_descendants`` +
``verify_folder_exists``) y ``build_uploader`` monkeypatcheado, así los
tests no tocan red ni necesitan un YAML completo: ``_load`` devuelve una
config mínima con la sección `cmis` y ``mapping.type_manifest_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import cmcourier.cli.commands.types as types_cmd
from cmcourier.adapters.manifest.json_store import JsonTypeManifestStore
from cmcourier.cli.commands.types import types_group
from cmcourier.domain.cm_types import DECISION_OMIT, DECISION_USE
from cmcourier.services.type_manifest import build_manifest

pytestmark = pytest.mark.unit

_TESTS_ROOT = Path(__file__).parents[3]
_PIPE = _TESTS_ROOT / "fixtures" / "pipeline"
_ASM = _TESTS_ROOT / "fixtures" / "assembly"


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


def _prop(
    prop_id: str,
    *,
    required: bool = False,
    default: str | None = None,
    property_type: str = "string",
    max_length: int | None = None,
    updatability: str = "readwrite",
) -> dict[str, Any]:
    return {
        "id": prop_id,
        "displayName": prop_id.split(".")[-1],
        "propertyType": property_type,
        "cardinality": "single",
        "updatability": updatability,
        "required": required,
        "maxLength": max_length,
        "defaultValue": default,
        "inherited": False,
    }


def _type_node(
    id_corto: str,
    props: list[dict[str, Any]],
    *,
    suffix: str = "",
    display_name: str | None = None,
) -> dict[str, Any]:
    defs = {p["id"]: p for p in props}
    defs["clbNonGroup.BAC_ID_Corto"] = _prop(
        "clbNonGroup.BAC_ID_Corto", required=True, default=id_corto
    )
    return {
        "type": {
            "id": f"clbNonGroup.{id_corto}{suffix}",
            "localName": f"{id_corto}{suffix}",
            "displayName": (
                display_name if display_name is not None else f"{id_corto} - Clase de prueba"
            ),
            "creatable": True,
            "baseId": "cmis:document",
            "propertyDefinitions": defs,
        },
        "children": [],
    }


def _shared_nodes() -> list[dict[str, Any]]:
    """145 REQ-002: dos tipos que declaran el MISMO ID corto (PRD: ``DC35``)."""
    return [
        _type_node("DC01", [_prop("clbNonGroup.BAC_CIF", required=True, max_length=9)]),
        _type_node("DC01", [_prop("clbNonGroup.BAC_Otra")], suffix="_02", display_name="El otro"),
    ]


def _nodes(extra: bool = False) -> list[dict[str, Any]]:
    """Dos tipos; con ``extra`` el servidor agrega ``BAC_Nueva`` a DC01."""
    dc01 = [_prop("clbNonGroup.BAC_CIF", required=True, max_length=9)]
    if extra:
        dc01.append(_prop("clbNonGroup.BAC_Nueva", required=True))
    return [_type_node("DC01", dc01), _type_node("DC02", [_prop("clbNonGroup.BAC_Fecha")])]


class _FakeUploader:
    """Uploader mínimo: sólo lo que ``TypeDiscoveryService`` le pide."""

    def __init__(self, nodes: list[dict[str, Any]], *, missing: tuple[str, ...] = ()) -> None:
        self._nodes = nodes
        self._missing = missing
        self.verified: list[str] = []

    def get_type_descendants(
        self, include_property_definitions: bool = True
    ) -> list[dict[str, Any]]:
        return self._nodes

    def verify_folder_exists(self, folder_path: str) -> bool:
        self.verified.append(folder_path)
        return folder_path not in self._missing


def _config(manifest_path: Path | None) -> SimpleNamespace:
    mapping = (
        SimpleNamespace(type_manifest_path=manifest_path)
        if manifest_path is not None
        else SimpleNamespace()
    )
    return SimpleNamespace(
        cmis=SimpleNamespace(base_url="http://cm/services", repo_id="REPO"),
        mapping=mapping,
    )


def _cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("x: 1\n", encoding="utf-8")
    return path


def _run(
    args: list[str],
    *,
    nodes: list[dict[str, Any]] | None = None,
    manifest_path: Path | None = None,
    uploader: _FakeUploader | None = None,
) -> Any:
    """Invoca el grupo con ``_load`` y ``build_uploader`` fingidos."""
    fake = uploader or _FakeUploader(nodes or _nodes())
    with (
        patch.object(types_cmd, "_load", return_value=(_config(manifest_path), object())),
        patch.object(types_cmd, "_config_or_none", return_value=_config(manifest_path)),
        patch.object(types_cmd, "build_uploader", return_value=fake),
    ):
        return CliRunner().invoke(types_group, args)


def _seed(path: Path, nodes: list[dict[str, Any]] | None = None) -> JsonTypeManifestStore:
    store = JsonTypeManifestStore(path)
    store.save(
        build_manifest(
            nodes or _nodes(),
            service_url="http://cm/services",
            repository_id="REPO",
            discovered_at="2026-01-01T00:00:00+00:00",
        )
    )
    return store


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


class TestTypesDiscover145:
    def test_writes_manifest_with_summary_and_progress(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        uploader = _FakeUploader(_nodes(), missing=("/$type/DC02",))
        result = _run(
            ["discover", "--config", str(_cfg_file(tmp_path)), "--manifest", str(manifest)],
            uploader=uploader,
        )

        assert result.exit_code == 0, result.output
        stored = JsonTypeManifestStore(manifest).load()
        assert sorted(stored.types) == ["DC01", "DC02"]
        assert stored.types["DC01"].folder_ok is True
        assert stored.types["DC02"].folder_ok is False
        assert "2 tipo(s)" in result.stdout
        assert "ok=1" in result.stdout and "faltan=1" in result.stdout
        assert "procesando tipos 2/2" in result.stderr

    def test_no_verify_folders_skips_the_network(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        uploader = _FakeUploader(_nodes())
        result = _run(
            [
                "discover",
                "--config",
                str(_cfg_file(tmp_path)),
                "--manifest",
                str(manifest),
                "--no-verify-folders",
            ],
            uploader=uploader,
        )

        assert result.exit_code == 0, result.output
        assert uploader.verified == []
        assert JsonTypeManifestStore(manifest).load().types["DC01"].folder_ok is None

    def test_refuses_to_overwrite_reviewed_types(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        store = _seed(manifest)
        from cmcourier.services.type_manifest import mark_reviewed

        store.save(mark_reviewed(store.load(), "DC01"))
        before = manifest.read_text(encoding="utf-8")

        result = _run(
            ["discover", "--config", str(_cfg_file(tmp_path)), "--manifest", str(manifest)]
        )

        assert result.exit_code == 1
        assert "types update" in result.stderr
        assert manifest.read_text(encoding="utf-8") == before

    def test_force_overwrites_reviewed_types(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        store = _seed(manifest)
        from cmcourier.services.type_manifest import mark_reviewed

        store.save(mark_reviewed(store.load(), "DC01"))

        result = _run(
            [
                "discover",
                "--config",
                str(_cfg_file(tmp_path)),
                "--manifest",
                str(manifest),
                "--force",
            ]
        )

        assert result.exit_code == 0, result.output
        assert JsonTypeManifestStore(manifest).load().types["DC01"].reviewed is False

    def test_uses_the_path_from_the_config_when_there_is_no_override(self, tmp_path: Path) -> None:
        manifest = tmp_path / "desde-config.json"
        result = _run(["discover", "--config", str(_cfg_file(tmp_path))], manifest_path=manifest)

        assert result.exit_code == 0, result.output
        assert manifest.is_file()

    def test_errors_when_no_manifest_is_configured(self, tmp_path: Path) -> None:
        result = _run(["discover", "--config", str(_cfg_file(tmp_path))], manifest_path=None)

        assert result.exit_code != 0
        assert "no hay manifest configurado" in result.stderr

    def test_shared_short_id_warns_instead_of_aborting(self, tmp_path: Path) -> None:
        """145 REQ-002: el bug de PRD — dos tipos con el mismo ID corto."""
        manifest = tmp_path / "types.json"
        result = _run(
            ["discover", "--config", str(_cfg_file(tmp_path)), "--manifest", str(manifest)],
            nodes=_shared_nodes(),
        )

        assert result.exit_code == 0, result.output
        out = result.stdout
        assert "WARNING" in out
        assert "ID corto compartido DC01" in out
        assert "ganador clbNonGroup.DC01 (DC01 - Clase de prueba)" in out
        assert "candidato clbNonGroup.DC01_02 (El otro)" in out
        stored = JsonTypeManifestStore(manifest).load()
        assert sorted(stored.types) == ["DC01"]
        assert [d.type_id for d in stored.duplicates] == ["clbNonGroup.DC01_02"]

    def test_without_duplicates_there_is_no_warning(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        result = _run(
            ["discover", "--config", str(_cfg_file(tmp_path)), "--manifest", str(manifest)]
        )

        assert result.exit_code == 0, result.output
        assert "ID corto compartido" not in result.stdout


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


class TestTypesShow145:
    def test_prints_header_and_property_table(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(["show", "DC01", "--manifest", str(manifest)])

        assert result.exit_code == 0, result.output
        out = result.stdout
        assert "clbNonGroup.DC01" in out
        assert "/$type/DC01" in out
        assert "PROPIEDAD" in out and "DECISION" in out and "NOMBRE" in out
        assert "clbNonGroup.BAC_CIF" in out
        assert DECISION_USE in out

    def test_unknown_code_exits_1(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(["show", "ZZ99", "--manifest", str(manifest)])

        assert result.exit_code == 1
        assert "ZZ99" in result.stderr

    def test_lists_the_candidates_that_share_the_short_id(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest, _shared_nodes())

        result = _run(["show", "DC01", "--manifest", str(manifest)])

        assert result.exit_code == 0, result.output
        out = result.stdout
        assert "Candidatos con el mismo ID corto:" in out
        assert "clbNonGroup.DC01_02" in out
        assert "El otro" in out
        assert "--type-id" in out

    def test_without_candidates_there_is_no_block(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(["show", "DC01", "--manifest", str(manifest)])

        assert result.exit_code == 0, result.output
        assert "Candidatos con el mismo ID corto" not in result.stdout

    def test_live_reads_the_server(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(
            [
                "show",
                "DC01",
                "--config",
                str(_cfg_file(tmp_path)),
                "--manifest",
                str(manifest),
                "--live",
            ],
            nodes=_nodes(extra=True),
        )

        assert result.exit_code == 0, result.output
        assert "clbNonGroup.BAC_Nueva" in result.stdout


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


class TestTypesDiff145:
    def test_exits_1_when_the_server_moved(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(
            ["diff", "--config", str(_cfg_file(tmp_path)), "--manifest", str(manifest)],
            nodes=_nodes(extra=True),
        )

        assert result.exit_code == 1
        assert "clbNonGroup.BAC_Nueva" in result.stdout

    def test_exits_0_without_differences(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(["diff", "--config", str(_cfg_file(tmp_path)), "--manifest", str(manifest)])

        assert result.exit_code == 0, result.output
        assert "sin diferencias" in result.stdout


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestTypesUpdate145:
    def test_applies_the_diff_and_lists_touched_types(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(
            ["update", "--config", str(_cfg_file(tmp_path)), "--manifest", str(manifest)],
            nodes=_nodes(extra=True),
        )

        assert result.exit_code == 0, result.output
        assert "DC01" in result.stdout
        assert "DC02" not in result.stdout
        entry = JsonTypeManifestStore(manifest).load().types["DC01"]
        assert entry.decisions["clbNonGroup.BAC_Nueva"] == DECISION_USE
        assert entry.changes == ("+ clbNonGroup.BAC_Nueva (required)",)

    def test_only_limits_the_types_touched(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(
            [
                "update",
                "--config",
                str(_cfg_file(tmp_path)),
                "--manifest",
                str(manifest),
                "--only",
                "DC02",
            ],
            nodes=_nodes(extra=True),
        )

        assert result.exit_code == 0, result.output
        entry = JsonTypeManifestStore(manifest).load().types["DC01"]
        assert "clbNonGroup.BAC_Nueva" not in entry.decisions

    def test_says_so_when_nothing_changed(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(["update", "--config", str(_cfg_file(tmp_path)), "--manifest", str(manifest)])

        assert result.exit_code == 0, result.output
        assert "sin cambios" in result.stdout


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


class TestTypesReview145:
    def test_sets_decisions_folder_and_marks_reviewed(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(
            [
                "review",
                "DC01",
                "--manifest",
                str(manifest),
                "--omit",
                "BAC_CIF",
                "--use",
                "clbNonGroup.BAC_ID_Corto",
                "--folder",
                "/Bancos/DC01",
                "--done",
            ]
        )

        assert result.exit_code == 0, result.output
        entry = JsonTypeManifestStore(manifest).load().types["DC01"]
        assert entry.decisions["clbNonGroup.BAC_CIF"] == DECISION_OMIT
        assert entry.decisions["clbNonGroup.BAC_ID_Corto"] == DECISION_USE
        assert entry.folder == "/Bancos/DC01"
        assert entry.folder_source == "manual"
        assert entry.reviewed is True
        assert "BAC_CIF" in result.stdout

    def test_unknown_property_exits_1_listing_the_valid_ones(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(["review", "DC01", "--manifest", str(manifest), "--use", "BAC_NOPE"])

        assert result.exit_code == 1
        assert "BAC_NOPE" in result.stderr
        assert "BAC_CIF" in result.stderr
        assert JsonTypeManifestStore(manifest).load().types["DC01"].reviewed is False

    def test_unknown_code_exits_1(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)

        result = _run(["review", "ZZ99", "--manifest", str(manifest), "--done"])

        assert result.exit_code == 1
        assert "ZZ99" in result.stderr

    def test_type_id_promotes_the_candidate(self, tmp_path: Path) -> None:
        """145 REQ-002: elegir a mano el ganador de un ID corto compartido."""
        manifest = tmp_path / "types.json"
        _seed(manifest, _shared_nodes())

        result = _run(
            ["review", "DC01", "--manifest", str(manifest), "--type-id", "clbNonGroup.DC01_02"]
        )

        assert result.exit_code == 0, result.output
        stored = JsonTypeManifestStore(manifest).load()
        assert stored.types["DC01"].type_id == "clbNonGroup.DC01_02"
        assert [d.type_id for d in stored.duplicates] == ["clbNonGroup.DC01"]

    def test_type_id_applies_before_the_other_flags(self, tmp_path: Path) -> None:
        """El ``--use`` se resuelve contra las propiedades del tipo PROMOVIDO."""
        manifest = tmp_path / "types.json"
        _seed(manifest, _shared_nodes())

        result = _run(
            [
                "review",
                "DC01",
                "--manifest",
                str(manifest),
                "--type-id",
                "clbNonGroup.DC01_02",
                "--use",
                "BAC_Otra",
                "--done",
            ]
        )

        assert result.exit_code == 0, result.output
        entry = JsonTypeManifestStore(manifest).load().types["DC01"]
        assert entry.decisions["clbNonGroup.BAC_Otra"] == DECISION_USE
        assert entry.reviewed is True

    def test_unknown_type_id_exits_1_without_touching_the_manifest(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest, _shared_nodes())
        before = manifest.read_text(encoding="utf-8")

        result = _run(["review", "DC01", "--manifest", str(manifest), "--type-id", "clb.NOPE"])

        assert result.exit_code == 1
        assert "clb.NOPE" in result.stderr
        assert manifest.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# check (145 REQ-005)
# ---------------------------------------------------------------------------


def _rvi_cm_csv(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """145 REQ-001: ``MapeoRVI_CM.csv`` reducido a ``IDSistema,IDRVI,IDCM``."""
    lines = ["IDSistema,IDRVI,IDCM"]
    lines.extend(f"{sistema},{id_rvi},{id_cm}" for sistema, id_rvi, id_cm in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_DEFAULT_FIELD_SOURCES = """    BAC_CIF:
      sources:
        - source_type: trigger
          lookup_value_column: cif
"""


def _check_yaml(
    tmp_path: Path,
    *,
    rvi_cm_csv: Path,
    manifest_json: Path | None,
    field_sources: str = _DEFAULT_FIELD_SOURCES,
) -> Path:
    """YAML REAL en modo manifest — ``check`` corre contra la config posta."""
    triggers = tmp_path / "triggers.csv"
    triggers.write_text("ShortName,CIF,SystemID\nTESTCLIENT01,123456,1\n", encoding="utf-8")
    mapping = (
        f"  rvi_cm_csv_path: {rvi_cm_csv}\n  type_manifest_path: {manifest_json}\n"
        if manifest_json is not None
        else f"  csv_path: {_TESTS_ROOT / 'fixtures' / 'services' / 'modelo_documental.csv'}\n"
    )
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        f"""trigger:
  csv_path: {triggers}
indexing:
  source:
    kind: csv
    csv_path: {_PIPE / "rvabrep.csv"}
mapping:
{mapping}metadata:
  field_sources:
{field_sources}assembly:
  source_root: {_ASM}
  temp_dir: {tmp_path / "stg"}
cmis:
  base_url: http://cm/services
  repo_id: REPO
tracking:
  db_path: {tmp_path / "tracking.db"}
observability:
  log_dir: {tmp_path / "logs"}
""",
        encoding="utf-8",
    )
    return yaml_path


def _check(args: list[str]) -> Any:
    """``types check`` sin dobles: config real, manifest real, cero red."""
    return CliRunner().invoke(types_group, args)


class TestTypesCheck145:
    def test_exit_0_and_groups_findings_by_severity(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)
        yaml_path = _check_yaml(
            tmp_path,
            rvi_cm_csv=_rvi_cm_csv(tmp_path / "MapeoRVI_CM.csv", [("", "FB01", "DC01")]),
            manifest_json=manifest,
        )

        result = _check(["check", "--config", str(yaml_path)])

        assert result.exit_code == 0, result.output
        assert "CRITICAL" not in result.stdout
        # DC01 todavía no fue revisado por el operador
        assert "WARNING" in result.stdout
        assert "DC01" in result.stdout

    def test_exit_1_when_a_used_property_has_no_field_source(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)
        yaml_path = _check_yaml(
            tmp_path,
            rvi_cm_csv=_rvi_cm_csv(tmp_path / "MapeoRVI_CM.csv", [("", "FB01", "DC01")]),
            manifest_json=manifest,
            field_sources=_DEFAULT_FIELD_SOURCES.replace("BAC_CIF", "BAC_Otro"),
        )

        result = _check(["check", "--config", str(yaml_path)])

        assert result.exit_code == 1
        assert "CRITICAL" in result.stdout
        assert "field_sources.BAC_CIF" in result.stdout

    def test_exit_1_when_the_csv_points_at_a_code_outside_the_manifest(
        self, tmp_path: Path
    ) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)
        yaml_path = _check_yaml(
            tmp_path,
            rvi_cm_csv=_rvi_cm_csv(tmp_path / "MapeoRVI_CM.csv", [("", "FB01", "ZZ99")]),
            manifest_json=manifest,
        )

        result = _check(["check", "--config", str(yaml_path)])

        assert result.exit_code == 1
        assert "ZZ99" in result.stdout

    def test_json_flag_prints_the_machine_readable_report(self, tmp_path: Path) -> None:
        manifest = tmp_path / "types.json"
        _seed(manifest)
        yaml_path = _check_yaml(
            tmp_path,
            rvi_cm_csv=_rvi_cm_csv(tmp_path / "MapeoRVI_CM.csv", [("", "FB01", "DC01")]),
            manifest_json=manifest,
        )

        result = _check(["check", "--config", str(yaml_path), "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["has_critical"] is False
        assert payload["counts"]["WARNING"] >= 1
        assert all("severity" in f for f in payload["findings"])

    def test_manifest_override_wins_over_the_yaml(self, tmp_path: Path) -> None:
        """``--manifest`` gobierna las DOS puntas: mapping y manifest leído."""
        configured = tmp_path / "types.json"
        _seed(configured)
        override = tmp_path / "otro.json"
        _seed(override, [_type_node("ZZ99", [_prop("clbNonGroup.BAC_CIF", required=True)])])
        yaml_path = _check_yaml(
            tmp_path,
            rvi_cm_csv=_rvi_cm_csv(tmp_path / "MapeoRVI_CM.csv", [("", "FB01", "ZZ99")]),
            manifest_json=configured,
        )

        result = _check(["check", "--config", str(yaml_path), "--manifest", str(override)])

        assert result.exit_code == 0, result.output
        assert "no existe en el manifest" not in result.stdout

    def test_errors_outside_manifest_mode(self, tmp_path: Path) -> None:
        yaml_path = _check_yaml(
            tmp_path, rvi_cm_csv=tmp_path / "nada.csv", manifest_json=None, field_sources="    {}\n"
        )

        result = _check(["check", "--config", str(yaml_path)])

        assert result.exit_code != 0
        assert "manifest" in result.stderr


# ---------------------------------------------------------------------------
# Registro en el CLI raíz
# ---------------------------------------------------------------------------


class TestTypesRegistered145:
    def test_root_cli_exposes_the_group(self) -> None:
        from cmcourier.cli.app import main

        assert "types" in main.commands
