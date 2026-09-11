"""Tests de integración para los subcomandos ``cmcourier batch ...`` (021)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from click.testing import CliRunner

from cmcourier.adapters.tracking import SQLiteTrackingStore
from cmcourier.cli.app import main
from cmcourier.domain.models import StageStatus

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_TESTS_ROOT = Path(__file__).parent.parent.parent
_PIPELINE_FIXTURES = _TESTS_ROOT / "fixtures" / "pipeline"
_SERVICES_FIXTURES = _TESTS_ROOT / "fixtures" / "services"
_ASSEMBLY_FIXTURES = _TESTS_ROOT / "fixtures" / "assembly"

_CMIS_BASE_URL = "http://cmis.example.test:9080/opencmcmis/browser"
_CMIS_REPO_ID = "$x!testrepo"


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
              base_url: {_CMIS_BASE_URL}
              repo_id: "{_CMIS_REPO_ID}"
            tracking:
              db_path: {tmp_path / "tracking.db"}
            observability:
              log_dir: {tmp_path / "logs"}
            """
        )
    )
    return yaml_path


def _seed_batch(
    db_path: Path,
    *,
    complete: bool = False,
    fail_stage: StageStatus | None = None,
) -> str:
    from datetime import datetime

    from cmcourier.domain.models import MigrationRecord

    store = SQLiteTrackingStore(db_path)
    try:
        batch_id = store.start_batch(total_records=1)
        record = MigrationRecord(
            trigger_shortname="TESTUSER001",
            trigger_cif="000000",
            trigger_system_id="1",
            rvabrep_txn_num="TXN_SEED",
            rvabrep_file_name="SEED.001",
            batch_id=batch_id,
            status=StageStatus.S2_PENDING,
            created_at=datetime(2026, 1, 1, 0, 0),
        )
        store.mark_stage_pending(record, StageStatus.S2_PENDING)
        if fail_stage is not None:
            store.mark_stage_failed("TXN_SEED", batch_id, fail_stage, "synthetic fail")
        else:
            store.mark_stage_done("TXN_SEED", batch_id, StageStatus.S2_DONE)
        if complete:
            store.complete_batch(batch_id)
        store.flush()
    finally:
        store.close()
    return batch_id


# ---------------------------------------------------------------------------
# batch list
# ---------------------------------------------------------------------------


class TestBatchList:
    def test_help(self) -> None:
        result = CliRunner().invoke(main, ["batch", "list", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.stdout
        assert "--status" in result.stdout

    def test_empty_store(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(main, ["batch", "list", "-c", str(yaml_path)])
        assert result.exit_code == 0, result.output
        assert "No batches recorded." in result.stdout

    def test_lists_batches_with_status(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        db_path = tmp_path / "tracking.db"
        seed_a = _seed_batch(db_path, complete=True)
        seed_b = _seed_batch(db_path, complete=False)
        result = CliRunner().invoke(main, ["batch", "list", "-c", str(yaml_path)])
        assert result.exit_code == 0
        assert seed_a in result.stdout
        assert seed_b in result.stdout
        assert "STATUS" in result.stdout

    def test_filter_in_progress(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        db_path = tmp_path / "tracking.db"
        seed_a = _seed_batch(db_path, complete=True)
        seed_b = _seed_batch(db_path, complete=False)
        result = CliRunner().invoke(
            main, ["batch", "list", "-c", str(yaml_path), "--status", "in_progress"]
        )
        assert result.exit_code == 0
        assert seed_b in result.stdout
        assert seed_a not in result.stdout


# ---------------------------------------------------------------------------
# batch show
# ---------------------------------------------------------------------------


class TestBatchShow:
    def test_help(self) -> None:
        result = CliRunner().invoke(main, ["batch", "show", "--help"])
        assert result.exit_code == 0
        assert "BATCH_ID" in result.stdout

    def test_unknown_batch_exits_1(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(main, ["batch", "show", "-c", str(yaml_path), "ghost-123"])
        assert result.exit_code == 1
        assert "Batch not found" in result.stderr

    def test_show_known_batch(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_batch(tmp_path / "tracking.db", complete=True)
        result = CliRunner().invoke(main, ["batch", "show", "-c", str(yaml_path), batch_id])
        assert result.exit_code == 0, result.output
        assert batch_id in result.stdout
        assert "STAGE" in result.stdout
        assert "S2" in result.stdout

    def test_show_failed_batch_lists_failures(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_batch(tmp_path / "tracking.db", fail_stage=StageStatus.S5_FAILED)
        result = CliRunner().invoke(main, ["batch", "show", "-c", str(yaml_path), batch_id])
        assert result.exit_code == 0
        assert "FAILED records" in result.stdout
        assert "TXN_SEED" in result.stdout
        assert "S5_FAILED" in result.stdout


# ---------------------------------------------------------------------------
# batch retry-failed
# ---------------------------------------------------------------------------


class TestBatchRetryFailed:
    def test_help(self) -> None:
        result = CliRunner().invoke(main, ["batch", "retry-failed", "--help"])
        assert result.exit_code == 0
        assert "--batch" in result.stdout
        assert "--stage" in result.stdout

    def test_resets_all_failures(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_batch(tmp_path / "tracking.db", fail_stage=StageStatus.S5_FAILED)
        result = CliRunner().invoke(
            main,
            [
                "batch",
                "retry-failed",
                "-c",
                str(yaml_path),
                "--batch",
                batch_id,
            ],
        )
        assert result.exit_code == 0
        assert "Reset 1 FAILED" in result.stdout

    def test_resets_only_specified_stage(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_batch(tmp_path / "tracking.db", fail_stage=StageStatus.S5_FAILED)
        result = CliRunner().invoke(
            main,
            [
                "batch",
                "retry-failed",
                "-c",
                str(yaml_path),
                "--batch",
                batch_id,
                "--stage",
                "S5",
            ],
        )
        assert result.exit_code == 0
        assert "Reset 1 FAILED" in result.stdout
        assert "stage=S5" in result.stdout

    def test_no_failures_returns_zero(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_batch(tmp_path / "tracking.db", complete=True)
        result = CliRunner().invoke(
            main,
            [
                "batch",
                "retry-failed",
                "-c",
                str(yaml_path),
                "--batch",
                batch_id,
            ],
        )
        assert result.exit_code == 0
        assert "Reset 0 FAILED" in result.stdout


# ---------------------------------------------------------------------------
# batch export-report (023)
# ---------------------------------------------------------------------------


class TestBatchExportReport:
    def test_help(self) -> None:
        result = CliRunner().invoke(main, ["batch", "export-report", "--help"])
        assert result.exit_code == 0
        for flag in ("--batch", "--format", "--output"):
            assert flag in result.stdout

    def test_csv_stdout(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_batch(tmp_path / "tracking.db", fail_stage=StageStatus.S5_FAILED)
        result = CliRunner().invoke(
            main,
            [
                "batch",
                "export-report",
                "-c",
                str(yaml_path),
                "--batch",
                batch_id,
                "--format",
                "csv",
            ],
        )
        assert result.exit_code == 0, result.output
        # 148: el reporte pasó a tener tres bloques (etapas, cuadre, censo)
        # separados por una línea en blanco. El primero es el de siempre.
        blocks = result.stdout.strip().split("\n\n")
        stage_block = blocks[0].splitlines()
        assert stage_block[0].startswith("batch_id,status,started_at")
        assert len(stage_block) == 7
        # Cada fila tiene el batch_id en la columna 0.
        for line in stage_block[1:]:
            assert line.startswith(f"{batch_id},")
        # La fila S5 reporta la cantidad de fallos.
        s5_line = next(ln for ln in stage_block if ",S5," in ln)
        assert s5_line.endswith(",0,1,0")
        assert blocks[1].splitlines()[0] == "metric,value"
        assert blocks[2].splitlines()[0] == "bucket,reason_code,id_rvi,count"

    def test_json_stdout(self, tmp_path: Path) -> None:
        import json

        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_batch(tmp_path / "tracking.db", fail_stage=StageStatus.S5_FAILED)
        result = CliRunner().invoke(
            main,
            [
                "batch",
                "export-report",
                "-c",
                str(yaml_path),
                "--batch",
                batch_id,
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["batch_id"] == batch_id
        assert "stage_counts" in payload
        assert "failed_records" in payload
        assert payload["stage_counts"]["S5"]["FAILED"] == 1
        assert len(payload["failed_records"]) == 1
        assert payload["failed_records"][0]["txn_num"] == "TXN_SEED"

    def test_output_writes_file(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_batch(tmp_path / "tracking.db", complete=True)
        out_path = tmp_path / "report.csv"
        result = CliRunner().invoke(
            main,
            [
                "batch",
                "export-report",
                "-c",
                str(yaml_path),
                "--batch",
                batch_id,
                "--format",
                "csv",
                "--output",
                str(out_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Report written to" in result.stdout
        assert out_path.exists()
        assert out_path.read_text().startswith("batch_id,status,")

    def test_unknown_batch_exits_1(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(
            main,
            [
                "batch",
                "export-report",
                "-c",
                str(yaml_path),
                "--batch",
                "ghost-batch",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 1
        assert "Batch not found" in result.stderr


# ---------------------------------------------------------------------------
# El censo, de punta a punta (148 REQ-006)
# ---------------------------------------------------------------------------


def _seed_census_batch(db_path: Path, *, source_total: int) -> str:
    """Un batch con un migrado y cinco razones repartidas en tres baldes.

    Va contra el store REAL: lo que se ejercita es la agregación SQL de
    ``get_batch_details`` más el render, no un mock del medio.
    """
    from datetime import datetime

    from cmcourier.domain.models import MigrationRecord, ReasonCode

    store = SQLiteTrackingStore(db_path)
    try:
        batch_id = store.start_batch(total_records=0)
        store.increment_source_total(batch_id, source_total)

        def _row(txn: str, id_rvi: str, stage: StageStatus) -> None:
            store.mark_stage_pending(
                MigrationRecord(
                    trigger_shortname="TESTUSER001",
                    trigger_cif="000000",
                    trigger_system_id="1",
                    rvabrep_txn_num=txn,
                    rvabrep_file_name=f"{txn}.001",
                    batch_id=batch_id,
                    status=stage,
                    created_at=datetime(2026, 1, 1, 0, 0),
                    id_rvi=id_rvi,
                ),
                stage,
            )

        _row("TXN_OK", "CC03", StageStatus.S5_PENDING)
        store.mark_stage_done("TXN_OK", batch_id, StageStatus.S5_DONE)
        for n, id_rvi in ((1, "CC03"), (2, "CC03"), (3, "AA01")):
            _row(f"TXN_FILT_{n}", id_rvi, StageStatus.S1_PENDING)
            store.mark_stage_terminal(
                f"TXN_FILT_{n}",
                batch_id,
                StageStatus.S1_FILTERED,
                "codigo fuera de filters.document_types",
                reason_code=ReasonCode.EXCLUDED_BY_FILTER,
            )
        _row("TXN_BLOCK", "ZZ99", StageStatus.S2_PENDING)
        store.mark_stage_failed(
            "TXN_BLOCK",
            batch_id,
            StageStatus.S2_FAILED,
            "id rvi sin fila en MapeoRVI_CM.csv",
            reason_code=ReasonCode.CODE_NOT_MAPPED,
        )
        _row("TXN_FAIL", "CC03", StageStatus.S5_PENDING)
        store.mark_stage_failed(
            "TXN_FAIL",
            batch_id,
            StageStatus.S5_FAILED,
            "content manager no respondio",
            reason_code=ReasonCode.CM_TIMEOUT,
        )
        store.complete_batch(batch_id)
        store.flush()
    finally:
        store.close()
    return batch_id


class TestCensoEnBatchShow148:
    """El censo llega desde SQLite hasta la pantalla del operador."""

    def test_breakdown_renders_in_bucket_order_when_numbers_add_up(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_census_batch(tmp_path / "tracking.db", source_total=6)
        result = CliRunner().invoke(main, ["batch", "show", "-c", str(yaml_path), batch_id])
        assert result.exit_code == 0, result.output
        assert "Total en origen: 6" in result.stdout
        assert "Migrados: 1" in result.stdout
        lines = result.stdout.splitlines()
        census = [ln for ln in lines if ln.startswith(("EXCLUIDO", "BLOQUEADO", "FALLO"))]
        assert [ln.split()[:3] for ln in census] == [
            ["EXCLUIDO", "EXCLUDED_BY_FILTER", "CC03"],
            ["EXCLUIDO", "EXCLUDED_BY_FILTER", "AA01"],
            ["BLOQUEADO", "CODE_NOT_MAPPED", "ZZ99"],
            ["FALLO", "CM_TIMEOUT", "CC03"],
        ]
        assert "DESCUADRE" not in result.stdout
        cuadre = "Cuadre OK: migrados 1 + censados 5 = 6, igual al total en origen (6)."
        assert cuadre in result.stdout

    def test_discrepancy_is_stated_when_numbers_do_not_add_up(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_census_batch(tmp_path / "tracking.db", source_total=9)
        result = CliRunner().invoke(main, ["batch", "show", "-c", str(yaml_path), batch_id])
        assert result.exit_code == 0, result.output
        line = next(ln for ln in result.stdout.splitlines() if "DESCUADRE" in ln)
        assert "9" in line and "3 documentos sin explicar" in line

    def test_filtered_outcome_reaches_the_stage_table(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_census_batch(tmp_path / "tracking.db", source_total=6)
        result = CliRunner().invoke(main, ["batch", "show", "-c", str(yaml_path), batch_id])
        header = next(ln for ln in result.stdout.splitlines() if ln.startswith("STAGE"))
        assert "FILTERED" in header.split()
        s1_row = next(ln for ln in result.stdout.splitlines() if ln.startswith("S1 "))
        assert s1_row.split()[header.split().index("FILTERED")] == "3"

    def test_exports_carry_the_census(self, tmp_path: Path) -> None:
        import json

        yaml_path = _write_yaml(tmp_path)
        batch_id = _seed_census_batch(tmp_path / "tracking.db", source_total=6)
        csv_result = CliRunner().invoke(
            main,
            ["batch", "export-report", "-c", str(yaml_path)]
            + ["--batch", batch_id, "--format", "csv"],
        )
        assert csv_result.exit_code == 0, csv_result.output
        assert "EXCLUIDO,EXCLUDED_BY_FILTER,CC03,2" in csv_result.stdout
        assert "cuadra,si" in csv_result.stdout
        json_result = CliRunner().invoke(
            main,
            [
                "batch",
                "export-report",
                "-c",
                str(yaml_path),
                "--batch",
                batch_id,
                "--format",
                "json",
            ],
        )
        assert json_result.exit_code == 0, json_result.output
        census = json.loads(json_result.stdout)["census"]
        assert census["source_total"] == 6
        assert census["migrated"] == 1
        assert census["accounted"] == 5
        assert census["reconciles"] is True
        assert census["by_bucket"] == {"EXCLUIDO": 3, "BLOQUEADO": 1, "FALLO": 1}
