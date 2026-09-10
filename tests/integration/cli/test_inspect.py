"""Tests de integración para los subcomandos ``cmcourier inspect ...`` (021)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from click.testing import CliRunner

from cmcourier.cli.app import main

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_TESTS_ROOT = Path(__file__).parent.parent.parent
_PIPELINE_FIXTURES = _TESTS_ROOT / "fixtures" / "pipeline"
_SERVICES_FIXTURES = _TESTS_ROOT / "fixtures" / "services"
_ASSEMBLY_FIXTURES = _TESTS_ROOT / "fixtures" / "assembly"


def _write_yaml(tmp_path: Path) -> Path:
    triggers = tmp_path / "triggers.csv"
    triggers.write_text("ShortName,CIF,SystemID\nTESTCLIENT01,123456,1\n")
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        dedent(
            f"""\
            trigger:
              csv_path: {triggers}
            indexing:
              source:
                kind: csv
                csv_path: {_PIPELINE_FIXTURES / "rvabrep.csv"}
              columns:
                shortname_column: shortname
                system_id_column: system_id
                delete_code_column: delete_code
                txn_num_column: txn_num
                index2_column: index2
                index3_column: index3
                index4_column: index4
                index5_column: index5
                index6_column: index6
                index7_column: index7
                image_type_column: image_type
                image_path_column: image_path
                file_name_column: file_name
                creation_date_column: creation_date
                last_view_date_column: last_view_date
                total_pages_column: total_pages
            mapping:
              csv_path: {_SERVICES_FIXTURES / "modelo_documental.csv"}
            metadata:
              field_sources:
                BAC_CIF:
                  sources:
                    - source_type: trigger
                      lookup_value_column: cif
            assembly:
              source_root: {_ASSEMBLY_FIXTURES}
              temp_dir: {tmp_path / "stg"}
            cmis:
              base_url: http://cmis.test:9080/cmis
              repo_id: "$x!t"
            tracking:
              db_path: {tmp_path / "tracking.db"}
            observability:
              log_dir: {tmp_path / "logs"}
            """
        )
    )
    return yaml_path


# ---------------------------------------------------------------------------
# inspect rvabrep
# ---------------------------------------------------------------------------


class TestInspectRvabrep:
    def test_help(self) -> None:
        result = CliRunner().invoke(main, ["inspect", "rvabrep", "--help"])
        assert result.exit_code == 0
        assert "SHORTNAME" in result.stdout
        assert "SYSTEM_ID" in result.stdout

    def test_match(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(
            main,
            [
                "inspect",
                "rvabrep",
                "-c",
                str(yaml_path),
                "TESTCLIENT01",
                "1",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "TXN_NUM" in result.stdout
        assert "FILE_NAME" in result.stdout

    def test_no_match(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(
            main,
            [
                "inspect",
                "rvabrep",
                "-c",
                str(yaml_path),
                "GHOSTCLIENT",
                "99",
            ],
        )
        assert result.exit_code == 0
        assert "No RVABREP records found" in result.stderr


# ---------------------------------------------------------------------------
# inspect mapping
# ---------------------------------------------------------------------------


class TestInspectMapping:
    def test_help(self) -> None:
        result = CliRunner().invoke(main, ["inspect", "mapping", "--help"])
        assert result.exit_code == 0
        assert "ID_RVI" in result.stdout

    def test_known_id_rvi(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(main, ["inspect", "mapping", "-c", str(yaml_path), "CC03"])
        assert result.exit_code == 0, result.output
        assert "ID RVI: CC03" in result.stdout
        assert "CM folder:" in result.stdout
        assert "CM object type:" in result.stdout
        assert "Required metadata fields:" in result.stdout

    def test_unknown_id_rvi(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(main, ["inspect", "mapping", "-c", str(yaml_path), "FFXX"])
        assert result.exit_code == 0
        assert "No mapping found" in result.stderr

    def test_help_shows_system_option(self) -> None:
        """145 REQ-001: el sistema es la primera mitad de la clave del mapeo."""
        result = CliRunner().invoke(main, ["inspect", "mapping", "--help"])
        assert result.exit_code == 0
        assert "--system" in result.stdout

    def test_system_is_ignored_outside_manifest_mode(self, tmp_path: Path) -> None:
        """El modo consolidado guarda todo bajo el comodín: el sistema no cambia nada."""
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(
            main, ["inspect", "mapping", "-c", str(yaml_path), "--system", "RVI2", "CC03"]
        )
        assert result.exit_code == 0, result.output
        assert "ID RVI: CC03" in result.stdout


# ---------------------------------------------------------------------------
# inspect trigger (023)
# ---------------------------------------------------------------------------


class TestInspectTrigger:
    def test_help(self) -> None:
        result = CliRunner().invoke(main, ["inspect", "trigger", "--help"])
        assert result.exit_code == 0
        assert "--source" in result.stdout
        assert "--limit" in result.stdout

    def test_no_source_uses_yaml(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(main, ["inspect", "trigger", "-c", str(yaml_path)])
        assert result.exit_code == 0, result.output
        assert "SHORTNAME" in result.stdout
        assert "TESTCLIENT01" in result.stdout

    def test_source_csv_overrides(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        alt = tmp_path / "alt_triggers.csv"
        alt.write_text("ShortName,CIF,SystemID\nOTHERCLIENT,999999,2\n")
        result = CliRunner().invoke(
            main,
            [
                "inspect",
                "trigger",
                "-c",
                str(yaml_path),
                "--source",
                f"csv:{alt}",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "OTHERCLIENT" in result.stdout
        assert "TESTCLIENT01" not in result.stdout

    def test_source_single_doc(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(
            main,
            [
                "inspect",
                "trigger",
                "-c",
                str(yaml_path),
                "--source",
                "single_doc:DEBUG_SHORT,7,888888",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "DEBUG_SHORT" in result.stdout
        assert "888888" in result.stdout
        assert "7" in result.stdout

    def test_source_as400_rejected(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(
            main,
            [
                "inspect",
                "trigger",
                "-c",
                str(yaml_path),
                "--source",
                "as400:SELECT 1",
            ],
        )
        assert result.exit_code == 2
        assert "trigger.kind" in result.stderr or "YAML" in result.stderr

    def test_limit_caps_output(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        many = tmp_path / "many_triggers.csv"
        many.write_text(
            "ShortName,CIF,SystemID\n"
            + "\n".join(f"CLIENT{i:02d},{i:06d},1" for i in range(20))
            + "\n"
        )
        result = CliRunner().invoke(
            main,
            [
                "inspect",
                "trigger",
                "-c",
                str(yaml_path),
                "--source",
                f"csv:{many}",
                "--limit",
                "3",
            ],
        )
        assert result.exit_code == 0
        # Header + 3 filas de datos = 4 líneas (sin resumen al final).
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 4

    def test_zero_triggers(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        empty = tmp_path / "empty.csv"
        empty.write_text("ShortName,CIF,SystemID\n")
        result = CliRunner().invoke(
            main,
            [
                "inspect",
                "trigger",
                "-c",
                str(yaml_path),
                "--source",
                f"csv:{empty}",
            ],
        )
        assert result.exit_code == 0
        assert "No triggers produced" in result.stderr


# ---------------------------------------------------------------------------
# inspect mapping-stats (023)
# ---------------------------------------------------------------------------


class TestInspectMappingStats:
    def test_help(self) -> None:
        result = CliRunner().invoke(main, ["inspect", "mapping-stats", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.stdout

    def test_basic_summary(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(main, ["inspect", "mapping-stats", "-c", str(yaml_path)])
        assert result.exit_code == 0, result.output
        assert "Total mappings:" in result.stdout
        assert "Distinct document classes:" in result.stdout
        assert "Mappings with ID Corto:" in result.stdout
        assert "Distinct CM object types:" in result.stdout
        assert "Distinct CM folders:" in result.stdout

    def test_top_classes_table_present(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(main, ["inspect", "mapping-stats", "-c", str(yaml_path)])
        assert result.exit_code == 0
        assert "Top classes by mapping count:" in result.stdout
        assert "CLASS" in result.stdout
        assert "COUNT" in result.stdout
