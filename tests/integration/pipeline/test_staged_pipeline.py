"""Integration tests for :class:`StagedPipeline`.

End-to-end pipeline tests: every adapter and service is real
(Constitution Principle VI). Only the CMIS HTTP layer is stubbed via the
``responses`` library. Each test builds its trigger CSV at runtime under
``tmp_path`` for self-containment.

The :class:`PipelineHarness` fixture (see ``conftest.py``) wires the full
adapter graph; tests register CMIS stubs and assert on the returned
``RunReport`` plus side effects in the tracking store.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx

from cmcourier.domain.models import RvabrepRowTrigger

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_trigger_csv(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """Write a triggers CSV under tmp_path. rows: (ShortName, CIF, SystemID)."""
    path = tmp_path / "triggers.csv"
    lines = ["ShortName,CIF,SystemID"]
    lines.extend(",".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n")
    return path


def _count_rows(db_path: Path, batch_id: str, status: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM migration_log WHERE batch_id = ? AND status = ?",
            (batch_id, status),
        ).fetchone()[0]
    finally:
        conn.close()


def _batch_completed_at(db_path: Path, batch_id: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT completed_at FROM migration_batch WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Group 1 — Parameter validation
# ---------------------------------------------------------------------------


class TestParameterValidation:
    def test_batch_size_zero_raises(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        with pytest.raises(ValueError, match="batch_size"):
            pipeline_harness.build_pipeline(triggers).run(
                source_descriptor=str(triggers), batch_size=0
            )

    def test_from_stage_below_one_raises(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        with pytest.raises(ValueError, match="from_stage"):
            pipeline_harness.build_pipeline(triggers).run(
                source_descriptor=str(triggers), from_stage=0
            )

    def test_from_stage_above_five_raises(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        with pytest.raises(ValueError, match="from_stage"):
            pipeline_harness.build_pipeline(triggers).run(
                source_descriptor=str(triggers), from_stage=6
            )

    def test_from_stage_gt_one_without_batch_id_raises(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        with pytest.raises(ValueError, match="batch_id"):
            pipeline_harness.build_pipeline(triggers).run(
                source_descriptor=str(triggers), from_stage=3
            )


# ---------------------------------------------------------------------------
# Group 2 — Fresh full run
# ---------------------------------------------------------------------------


class TestFreshFullRun:
    @respx.mock
    def test_happy_path_two_docs(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001", "TXN_PIPE_002"])
        triggers = _write_trigger_csv(
            tmp_path,
            [("TESTCLIENT01", "123456", "1"), ("TESTCLIENT02", "234567", "1")],
        )
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.s5_done == 2
        assert report.s5_failed == 0
        assert report.s2_failed == 0
        assert report.s3_failed == 0
        assert report.s4_failed == 0
        assert report.total_docs == 2
        assert report.elapsed_seconds >= 0.0
        assert len(report.batch_id) > 0

    @respx.mock
    def test_complete_batch_called(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        assert _batch_completed_at(pipeline_harness.db_path, report.batch_id) is not None

    @respx.mock
    def test_migration_log_row_per_doc(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001", "TXN_PIPE_002"])
        triggers = _write_trigger_csv(
            tmp_path,
            [("TESTCLIENT01", "123456", "1"), ("TESTCLIENT02", "234567", "1")],
        )
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        s5_done_count = _count_rows(pipeline_harness.db_path, report.batch_id, "S5_DONE")
        assert s5_done_count == 2


# ---------------------------------------------------------------------------
# Group 3 — S1 error handling
# ---------------------------------------------------------------------------


class TestS1ErrorHandling:
    @respx.mock
    def test_trigger_not_in_rvabrep_logged_no_row(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pipeline_harness.register_cmis_for_docs([])
        triggers = _write_trigger_csv(tmp_path, [("NO_SUCH_CLIENT", "123456", "1")])
        with caplog.at_level(logging.WARNING, logger="cmcourier.orchestrators.staged"):
            report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.total_docs == 0
        assert report.s1_done == 0
        assert any(r.__dict__.get("shortname") == "NO_SUCH_CLIENT" for r in caplog.records)
        pipeline_harness.tracking_store.flush()
        # No migration_log rows for this batch.
        total = _count_rows(pipeline_harness.db_path, report.batch_id, "S1_DONE")
        assert total == 0

    @respx.mock
    def test_mixed_trigger_results(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(
            tmp_path,
            [("NO_SUCH_CLIENT", "999999", "1"), ("TESTCLIENT01", "123456", "1")],
        )
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.total_triggers == 2
        assert report.s5_done == 1


# ---------------------------------------------------------------------------
# Group 4 — Cross-batch is_uploaded skip
# ---------------------------------------------------------------------------


class TestCrossBatchSkip:
    @respx.mock
    def test_doc_uploaded_in_prior_batch_skipped(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        # First run: upload TXN_PIPE_001 successfully.
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        first = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert first.s5_done == 1
        pipeline_harness.tracking_store.flush()

        # Second run: SAME doc, FRESH batch. Must skip with no CMIS calls.
        # No CMIS stubs registered — if the orchestrator tries to upload, the
        # responses library would raise ConnectionError on the missing stub.
        second = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert second.s1_skipped_cross_batch == 1
        assert second.total_docs == 1
        assert second.s5_done == 0  # didn't upload again
        # 062: the skipped doc now produces an S1_SKIPPED row in the
        # second batch — the DETAIL tab + analyzer can identify it.
        pipeline_harness.tracking_store.flush()
        assert _count_rows(pipeline_harness.db_path, second.batch_id, "S1_SKIPPED") == 1
        conn = sqlite3.connect(pipeline_harness.db_path)
        try:
            row = conn.execute(
                "SELECT rvabrep_txn_num, error_message FROM migration_log "
                "WHERE batch_id = ? AND status = 'S1_SKIPPED'",
                (second.batch_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "TXN_PIPE_001"
        # 148 REQ-004: el mensaje es la razón de la taxonomía, y la columna
        # ``reason_code`` la lleva en su forma canónica.
        assert row[1] == "already_uploaded"

    @respx.mock
    def test_cross_batch_skip_logged(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        with caplog.at_level(logging.INFO, logger="cmcourier.orchestrators.staged"):
            pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        # 148: el log lleva la razón de la taxonomía, la MISMA que la columna.
        assert any(r.__dict__.get("reason") == "already_uploaded" for r in caplog.records)


# ---------------------------------------------------------------------------
# Group 5 — Stage failures
# ---------------------------------------------------------------------------


class TestStageFailures:
    @respx.mock
    def test_s2_unmapped_id_rvi(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        pipeline_harness.register_cmis_for_docs([])  # No upload expected.
        triggers = _write_trigger_csv(tmp_path, [("TESTUNMAPPED", "123456", "1")])
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.s2_failed == 1
        assert report.s5_done == 0
        pipeline_harness.tracking_store.flush()
        assert _count_rows(pipeline_harness.db_path, report.batch_id, "S2_FAILED") == 1

    @respx.mock
    def test_s3_metadata_source_failed(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        # CIF 999999 is not in clients.csv → BAC_Nombre_Cliente cannot resolve.
        pipeline_harness.register_cmis_for_docs([])
        triggers = _write_trigger_csv(tmp_path, [("TESTMETAFAIL", "999999", "1")])
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.s3_failed == 1
        assert report.s5_done == 0
        pipeline_harness.tracking_store.flush()
        assert _count_rows(pipeline_harness.db_path, report.batch_id, "S3_FAILED") == 1

    @respx.mock
    def test_s4_source_file_missing(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        # TESTMISSFILES points to a non-existent image_path.
        pipeline_harness.register_cmis_for_docs([])
        triggers = _write_trigger_csv(tmp_path, [("TESTMISSFILES", "123456", "1")])
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.s4_failed == 1
        assert report.s5_done == 0
        pipeline_harness.tracking_store.flush()
        assert _count_rows(pipeline_harness.db_path, report.batch_id, "S4_FAILED") == 1

    @respx.mock
    def test_s5_cmis_4xx_fail_fast(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        # Warmup + folder OK, but upload returns 400.
        respx.get("http://cmis.example.test:9080/opencmcmis/browser/$x!testrepo").mock(
            return_value=httpx.Response(
                200, json={"repositoryId": "$x!testrepo", "productName": "x"}
            )
        )
        respx.post("http://cmis.example.test:9080/opencmcmis/browser/$x!testrepo/root").mock(
            return_value=httpx.Response(201, json={"ok": True})
        )
        respx.post(
            "http://cmis.example.test:9080/opencmcmis/browser/$x!testrepo/root/$type/BAC_04_01_01_01_01"
        ).mock(return_value=httpx.Response(400, json={"error": "bad request"}))
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.s5_failed == 1
        pipeline_harness.tracking_store.flush()
        assert _count_rows(pipeline_harness.db_path, report.batch_id, "S5_FAILED") == 1


# ---------------------------------------------------------------------------
# Group 6 — Resume
# ---------------------------------------------------------------------------


class TestResume:
    @respx.mock
    def test_resume_from_stage_3_idempotent(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        # First run: complete end-to-end.
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        first = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()

        # Second run with from_stage=3: every is_stage_done skip-check fires.
        # No new CMIS calls (rely on the harness's already-registered stubs,
        # which are exhausted; if the orchestrator tries to upload again,
        # `responses` would raise ConnectionError on missing stub).
        second = pipeline_harness.build_pipeline(triggers).run(
            source_descriptor=str(triggers),
            batch_id=first.batch_id,
            from_stage=3,
        )
        assert second.batch_id == first.batch_id
        # S5 still counts as done (we count skips into s5_done).
        assert second.s5_done == 1

    @respx.mock
    def test_resume_out_of_scope_doc_dropped(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # First run: only TESTCLIENT01 in scope.
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers_v1 = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        first = pipeline_harness.build_pipeline(triggers_v1).run(source_descriptor=str(triggers_v1))
        pipeline_harness.tracking_store.flush()

        # Second run with from_stage=3 and a DIFFERENT trigger CSV that
        # also includes TESTCLIENT02. TESTCLIENT02 is out of scope.
        triggers_v2 = tmp_path / "triggers_v2.csv"
        triggers_v2.write_text(
            "ShortName,CIF,SystemID\nTESTCLIENT01,123456,1\nTESTCLIENT02,234567,1\n"
        )
        with caplog.at_level(logging.INFO, logger="cmcourier.orchestrators.staged"):
            second = pipeline_harness.build_pipeline(triggers_v2).run(
                source_descriptor=str(triggers_v2),
                batch_id=first.batch_id,
                from_stage=3,
            )
        assert second.s5_done == 1  # only the in-scope doc counts
        # 148 REQ-004: the out-of-scope doc stops being a bare `continue` —
        # it leaves a row with its reason, and the log carries the same
        # machine-readable code that lands in the column.
        assert any(r.__dict__.get("reason") == "out_of_scope_resume" for r in caplog.records)
        pipeline_harness.tracking_store.flush()
        conn = sqlite3.connect(pipeline_harness.db_path)
        try:
            rows = conn.execute(
                "SELECT rvabrep_txn_num FROM migration_log "
                "WHERE batch_id = ? AND reason_code = 'OUT_OF_SCOPE_RESUME'",
                (first.batch_id,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1

    @respx.mock
    def test_a_second_resume_does_not_adopt_out_of_scope_docs(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """148: la fila ``OUT_OF_SCOPE_RESUME`` no puede meter al documento
        DENTRO del alcance del resume siguiente — sería adoptar en el batch
        algo que el primer resume decidió justamente no tocar."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers_v1 = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        first = pipeline_harness.build_pipeline(triggers_v1).run(source_descriptor=str(triggers_v1))
        pipeline_harness.tracking_store.flush()
        triggers_v2 = tmp_path / "triggers_v2.csv"
        triggers_v2.write_text(
            "ShortName,CIF,SystemID\nTESTCLIENT01,123456,1\nTESTCLIENT02,234567,1\n"
        )
        for _ in range(2):
            pipeline_harness.build_pipeline(triggers_v2).run(
                source_descriptor=str(triggers_v2),
                batch_id=first.batch_id,
                from_stage=3,
            )
            pipeline_harness.tracking_store.flush()
        conn = sqlite3.connect(pipeline_harness.db_path)
        try:
            rows = conn.execute(
                "SELECT status FROM migration_log "
                "WHERE batch_id = ? AND reason_code = 'OUT_OF_SCOPE_RESUME'",
                (first.batch_id,),
            ).fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == ["S1_FILTERED"]

    @respx.mock
    def test_idempotent_rerun_from_stage_1(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        first = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()

        # Second run with same batch_id, from_stage=1. Every stage skip-checks.
        second = pipeline_harness.build_pipeline(triggers).run(
            source_descriptor=str(triggers),
            batch_id=first.batch_id,
            from_stage=1,
        )
        assert second.s5_done == 1  # counted via skip


# ---------------------------------------------------------------------------
# Group 7 — Heterogeneous batch
# ---------------------------------------------------------------------------


class TestHeterogeneous:
    @respx.mock
    def test_one_success_three_failures(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        # Configure CMIS only for the one success (TESTCLIENT01).
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(
            tmp_path,
            [
                ("TESTCLIENT01", "123456", "1"),  # success
                ("TESTUNMAPPED", "123456", "1"),  # S2 fail
                ("TESTMETAFAIL", "999999", "1"),  # S3 fail
                ("TESTMISSFILES", "123456", "1"),  # S4 fail
            ],
        )
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.s5_done == 1
        assert report.s2_failed == 1
        assert report.s3_failed == 1
        assert report.s4_failed == 1
        pipeline_harness.tracking_store.flush()
        assert _batch_completed_at(pipeline_harness.db_path, report.batch_id) is not None


# ---------------------------------------------------------------------------
# Group 8 — S0 failure
# ---------------------------------------------------------------------------


class TestS0Failure:
    def test_s0_failure_propagates_no_complete_batch(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        # CSV path that doesn't exist → CsvTriggerStrategy raises on iteration.
        with pytest.raises(Exception):  # noqa: B017 — trigger source raises wide
            pipeline_harness.pipeline.run(source_descriptor=str(tmp_path / "nope.csv"))


# ---------------------------------------------------------------------------
# Group 9 — Healed CIF propagates to upload metadata
# ---------------------------------------------------------------------------


class TestHealedCIF:
    @respx.mock
    def test_healed_cif_reaches_upload(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        # TESTHEAL has trigger.cif='' but rvabrep.index2='123456'.
        # After self-healing, BAC_CIF resolves to '123456' and the upload
        # should carry that value.
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_006"])
        triggers = _write_trigger_csv(tmp_path, [("TESTHEAL", "", "1")])
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.s5_done == 1
        # The captured upload request body contains the healed CIF.
        upload_calls = [
            c
            for c in respx.mock.calls
            if c.request.method == "POST" and "BAC_04_01_01_01_01" in str(c.request.url)
        ]
        assert len(upload_calls) == 1
        body = upload_calls[0].request.content
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        assert "123456" in body  # the healed CIF appears as a property value


# ---------------------------------------------------------------------------
# 051 — "filtered at S1" is a first-class outcome (delete-coded RVABREP rows)
# ---------------------------------------------------------------------------


def _rvabrep_row(shortname: str, txn: str, *, delete_code: str = "") -> dict[str, str]:
    return {
        "shortname": shortname,
        "system_id": "1",
        "delete_code": delete_code,
        "txn_num": txn,
        "index2": "123456",
        "index3": "",
        "index4": "",
        "index5": "",
        "index6": "",
        "index7": "CC03",
        "image_type": "B",
        "image_path": "p",
        "file_name": "DAAA.001",
        "creation_date": "1251117",
        "last_view_date": "0",
        "total_pages": "1",
    }


def _row_trigger(row: dict[str, str]) -> RvabrepRowTrigger:
    return RvabrepRowTrigger(
        row=row, col_shortname="shortname", col_cif="index2", col_system_id="system_id"
    )


class TestS1FilteredOutcome051:
    """A delete-coded RvabrepRowTrigger is *filtered* at S1 — counted and
    logged, never a silent drop and never a failure."""

    def test_deleted_rows_counted_as_filtered_with_conservation(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The trigger CSV is only used to build the pipeline; this test
        # drives _stage_s0_s1 directly with hand-built RvabrepRowTriggers.
        triggers_csv = _write_trigger_csv(tmp_path, [("UNUSED", "1", "1")])
        pipeline = pipeline_harness.build_pipeline(triggers_csv)
        batch_id = pipeline._tracking_store.start_batch(total_records=4)  # noqa: SLF001
        triggers = [
            _row_trigger(_rvabrep_row("A", "TXN_A")),
            _row_trigger(_rvabrep_row("B", "TXN_B", delete_code="D")),
            _row_trigger(_rvabrep_row("C", "TXN_C")),
            _row_trigger(_rvabrep_row("D", "TXN_D", delete_code="X")),
        ]
        with caplog.at_level(logging.INFO, logger="cmcourier.orchestrators.staged"):
            items, skipped, filtered = pipeline._stage_s0_s1(  # noqa: SLF001
                triggers, batch_id, None
            )

        assert len(items) == 2  # A, C → real docs
        assert filtered == 2  # B, D → delete-coded, filtered (NOT failed)
        assert skipped == 0
        # Conservation: every trigger is accounted for.
        assert len(items) + filtered + skipped == len(triggers)
        # Each filtered doc logged once, with the machine-readable reason.
        filtered_logs = [
            r for r in caplog.records if r.__dict__.get("reason") == "deleted_at_source"
        ]
        assert len(filtered_logs) == 2
        # 062: each filtered trigger produces a row in migration_log with
        # status=S1_FILTERED. 148 REQ-004: the key is the row's REAL txn_num
        # — the old synthetic FILTERED__{shortname}__{system_id} collided
        # against the unique index, collapsing N deleted docs of one client
        # into a single row.
        pipeline._tracking_store.flush()  # noqa: SLF001
        conn = sqlite3.connect(pipeline_harness.db_path)
        try:
            rows = conn.execute(
                "SELECT rvabrep_txn_num, error_message, reason_code, id_rvi "
                "FROM migration_log "
                "WHERE batch_id = ? AND status = 'S1_FILTERED' "
                "ORDER BY rvabrep_txn_num",
                (batch_id,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 2, f"expected 2 S1_FILTERED rows, got {rows}"
        assert {r[0] for r in rows} == {"TXN_B", "TXN_D"}
        assert {r[2] for r in rows} == {"DELETED_AT_SOURCE"}
        assert all(r[3] for r in rows), "id_rvi must be filled so the census can group by code"


# ---------------------------------------------------------------------------
# Group — 056: configurable prep workers (S2/S3/S4 on a fixed thread pool)
# ---------------------------------------------------------------------------


class TestPrepWorkers056:
    @respx.mock
    @pytest.mark.parametrize("prep_workers", [1, 4])
    def test_prep_outcome_identical_serial_and_parallel(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
        prep_workers: int,
    ) -> None:
        # The rvabrep fixture has six docs with failures targeted at each
        # prep stage: TESTUNMAPPED → S2, TESTMETAFAIL → S3,
        # TESTMISSFILES → S4; CLIENT01 / CLIENT02 / HEAL succeed. The
        # RunReport must be identical whether prep runs serial
        # (prep_workers=1) or on a 4-thread pool — same survivors, same
        # per-stage failure counts, no double-counting, no ordering drift.
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001", "TXN_PIPE_002", "TXN_PIPE_006"])
        triggers = _write_trigger_csv(
            tmp_path,
            [
                ("TESTCLIENT01", "123456", "1"),
                ("TESTUNMAPPED", "123456", "1"),
                ("TESTCLIENT02", "234567", "1"),
                ("TESTMISSFILES", "123456", "1"),
                ("TESTMETAFAIL", "999999", "1"),
                ("TESTHEAL", "123456", "1"),
            ],
        )
        report = pipeline_harness.build_pipeline(triggers, prep_workers=prep_workers).run(
            source_descriptor=str(triggers)
        )

        assert report.total_docs == 6
        assert report.s2_failed == 1  # TESTUNMAPPED — IDRViNotMappedError
        assert report.s3_failed == 1  # TESTMETAFAIL — metadata resolution
        assert report.s4_failed == 1  # TESTMISSFILES — source file missing
        assert report.s5_done == 3  # CLIENT01, CLIENT02, HEAL
        assert report.s5_failed == 0

        pipeline_harness.tracking_store.flush()
        assert _count_rows(pipeline_harness.db_path, report.batch_id, "S5_DONE") == 3
        assert _count_rows(pipeline_harness.db_path, report.batch_id, "S2_FAILED") == 1
        assert _count_rows(pipeline_harness.db_path, report.batch_id, "S3_FAILED") == 1
        assert _count_rows(pipeline_harness.db_path, report.batch_id, "S4_FAILED") == 1


# ---------------------------------------------------------------------------
# Group — 058: staged-file metadata persists to migration_log after S4
# ---------------------------------------------------------------------------


class TestStagedFileMetadataPersistence058:
    @respx.mock
    def test_s4_persists_staged_file_metadata_to_migration_log(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        # Pre-058, file_size_bytes / page_count / source_file_path stayed
        # NULL forever: S1 inserted them as None (item.staged_file was
        # None at S1), and the S4 INSERT-OR-IGNORE never updated the
        # existing row. 058 adds an UPDATE after a successful assemble.
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        assert report.s5_done == 1

        pipeline_harness.tracking_store.flush()
        conn = sqlite3.connect(pipeline_harness.db_path)
        try:
            row = conn.execute(
                "SELECT source_file_path, page_count, file_size_bytes "
                "FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
                ("TXN_PIPE_001", report.batch_id),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        source_file_path, page_count, file_size_bytes = row
        assert source_file_path is not None and source_file_path.endswith(".pdf")
        assert isinstance(page_count, int) and page_count > 0
        assert isinstance(file_size_bytes, int) and file_size_bytes > 0
