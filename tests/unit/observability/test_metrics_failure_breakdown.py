"""Tests del desglose de fallas por tipo en el MetricsRecorder (104, REQ-003/005)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcourier.observability.metrics import BatchSummary, MetricsRecorder

pytestmark = pytest.mark.unit


def _recorder(tmp_path: Path) -> MetricsRecorder:
    return MetricsRecorder(
        log_dir=tmp_path,
        slow_op_threshold_ms=1000.0,
        slow_op_top_n=5,
    )


class TestRecordUploadFailed:
    def test_counts_total_type_and_status(self, tmp_path: Path) -> None:
        rec = _recorder(tmp_path)
        rec.record_upload_failed("http_5xx", 503)
        rec.record_upload_failed("http_5xx", 503)
        rec.record_upload_failed("timeout", None)
        total, by_type, by_status = rec.failure_breakdown()
        assert total == 3
        assert by_type == {"http_5xx": 2, "timeout": 1}
        assert by_status == {503: 2}

    def test_upload_failed_count_stays_backward_compatible(self, tmp_path: Path) -> None:
        rec = _recorder(tmp_path)
        rec.record_upload_failed("http_4xx", 400)
        rec.record_upload_failed("app_error", None)
        assert rec.upload_failed_count() == 2

    def test_status_none_is_not_recorded_in_by_status(self, tmp_path: Path) -> None:
        rec = _recorder(tmp_path)
        rec.record_upload_failed("transport", None)
        _, _, by_status = rec.failure_breakdown()
        assert by_status == {}

    def test_clean_batch_has_empty_breakdown(self, tmp_path: Path) -> None:
        total, by_type, by_status = _recorder(tmp_path).failure_breakdown()
        assert total == 0
        assert by_type == {}
        assert by_status == {}


class TestBatchSummaryBreakdown:
    def test_to_record_includes_failure_fields(self) -> None:
        summary = BatchSummary(
            pipeline="p",
            batch_id="b",
            total_docs=100,
            elapsed_s=10.0,
            throughput_docs_per_s=10.0,
            stages={},
            failed_total=5,
            failures_by_type={"http_5xx": 5},
            failures_by_status={503: 5},
        )
        record = summary.to_record()
        assert record["failed_total"] == 5
        assert record["failures_by_type"] == {"http_5xx": 5}
        assert record["failures_by_status"] == {503: 5}

    def test_failure_fields_default_empty(self) -> None:
        summary = BatchSummary(
            pipeline="p",
            batch_id="b",
            total_docs=0,
            elapsed_s=0.0,
            throughput_docs_per_s=0.0,
            stages={},
        )
        assert summary.failed_total == 0
        assert summary.to_record()["failures_by_type"] == {}
        assert summary.to_record()["failures_by_status"] == {}

    def test_build_summary_carries_recorded_failures(self, tmp_path: Path) -> None:
        rec = _recorder(tmp_path)
        rec.record_upload_failed("http_5xx", 503)
        rec.record_upload_failed("timeout", None)
        summary = rec._build_summary(  # noqa: SLF001 — test del builder interno
            pipeline="p",
            batch_id="b",
            total_docs=10,
            elapsed_s=2.0,
        )
        assert summary.failed_total == 2
        assert summary.failures_by_type == {"http_5xx": 1, "timeout": 1}
        assert summary.failures_by_status == {503: 1}
