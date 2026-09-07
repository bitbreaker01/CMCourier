"""Unit tests for the metrics module.

Covers the percentile helper, batch summary builder, slow-op
aggregator, and ``StageTimer`` context manager outcome semantics.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from cmcourier.observability.metrics import (
    MetricsRecorder,
    SlowOpAggregator,
    StageTimer,
    _percentile,
    _StageBucket,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_returns_zero(self) -> None:
        assert _percentile([], 0.5) == 0.0

    def test_single_value(self) -> None:
        assert _percentile([42.0], 0.50) == 42.0
        assert _percentile([42.0], 0.99) == 42.0

    def test_p50_odd_count(self) -> None:
        # 1, 2, 3, 4, 5 → p50 = 3
        assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.50) == 3.0

    def test_p99_picks_top_for_small_lists(self) -> None:
        # 10 values 1..10; p99 = top (nearest-rank ceiling)
        assert _percentile([float(i) for i in range(1, 11)], 0.99) == 10.0

    def test_p0_picks_first(self) -> None:
        assert _percentile([1.0, 2.0, 3.0], 0.0) == 1.0


# ---------------------------------------------------------------------------
# _StageBucket / BatchSummary
# ---------------------------------------------------------------------------


class TestStageBucket:
    def test_empty_bucket_zeros(self) -> None:
        bucket = _StageBucket()
        s = bucket.summary()
        assert s == {
            "count": 0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "sum_ms": 0.0,
        }

    def test_records_aggregated(self) -> None:
        bucket = _StageBucket()
        for v in (10.0, 20.0, 30.0, 40.0, 50.0):
            bucket.record(v)
        s = bucket.summary()
        assert s["count"] == 5
        assert s["sum_ms"] == 150.0
        assert s["p50_ms"] == 30.0


# ---------------------------------------------------------------------------
# SlowOpAggregator
# ---------------------------------------------------------------------------


class TestSlowOpAggregator:
    def test_below_threshold_dropped(self) -> None:
        agg = SlowOpAggregator(threshold_ms=100.0, top_n=10)
        agg.consider(kind="cmis_upload", duration_ms=50.0)
        agg.consider(kind="cmis_upload", duration_ms=99.99)
        assert agg.top() == []

    def test_above_threshold_kept(self) -> None:
        agg = SlowOpAggregator(threshold_ms=100.0, top_n=10)
        agg.consider(kind="cmis_upload", duration_ms=150.0, txn_num="T1")
        agg.consider(kind="as400_query", duration_ms=200.0)
        top = agg.top()
        assert len(top) == 2
        assert top[0]["rank"] == 1
        assert top[0]["kind"] == "as400_query"
        assert top[0]["duration_ms"] == 200.0
        assert top[1]["kind"] == "cmis_upload"

    def test_top_n_caps_results(self) -> None:
        agg = SlowOpAggregator(threshold_ms=0.0, top_n=3)
        for i in range(10):
            agg.consider(kind="x", duration_ms=float(i + 1))
        top = agg.top()
        assert len(top) == 3
        # Largest values, ranked desc.
        assert [e["duration_ms"] for e in top] == [10.0, 9.0, 8.0]
        assert [e["rank"] for e in top] == [1, 2, 3]

    def test_optional_fields_passed_through(self) -> None:
        agg = SlowOpAggregator(threshold_ms=0.0, top_n=1)
        agg.consider(
            kind="cmis_upload",
            duration_ms=100.0,
            txn_num="TXN_042",
            stage="S5_UPLOAD",
            size_bytes=1024,
            url_prefix="http://cmis.example",
        )
        entry = agg.top()[0]
        assert entry["txn_num"] == "TXN_042"
        assert entry["stage"] == "S5_UPLOAD"
        assert entry["size_bytes"] == 1024


# ---------------------------------------------------------------------------
# StageTimer
# ---------------------------------------------------------------------------


def _make_recorder(tmp_path: Path) -> MetricsRecorder:
    return MetricsRecorder(
        log_dir=tmp_path / "logs",
        slow_op_threshold_ms=0.0,
        slow_op_top_n=10,
        enabled=True,
        pipeline_metrics_enabled=True,
    )


class TestStageTimer:
    def test_records_on_exit(self, tmp_path: Path) -> None:
        recorder = _make_recorder(tmp_path)
        recorder.start_batch(pipeline="csv-trigger", batch_id="b1")
        with StageTimer(
            recorder,
            pipeline="csv-trigger",
            stage="S2_MAPPING",
            batch_id="b1",
            txn_num="TXN_001",
        ):
            time.sleep(0.001)
        bucket = recorder._stage_buckets["S2_MAPPING"]  # type: ignore[attr-defined]
        assert bucket.summary()["count"] == 1
        assert bucket.summary()["sum_ms"] > 0

    def test_outcome_fail_on_exception(self, tmp_path: Path, caplog) -> None:
        recorder = _make_recorder(tmp_path)
        recorder.start_batch(pipeline="csv-trigger", batch_id="b1")
        with (
            caplog.at_level(logging.INFO, logger="cmcourier"),
            pytest.raises(RuntimeError),
            StageTimer(
                recorder,
                pipeline="csv-trigger",
                stage="S5_UPLOAD",
                batch_id="b1",
                txn_num="TXN_001",
            ),
        ):
            raise RuntimeError("boom")
        # The stage_complete event was emitted with outcome=FAIL.
        outcomes = [
            getattr(r, "outcome", None) for r in caplog.records if r.message == "stage_complete"
        ]
        assert "FAIL" in outcomes

    def test_exclude_discounts_wall_time(self, tmp_path: Path) -> None:
        """132/I3: la espera de re-auth no es trabajo de la etapa."""
        recorder = _make_recorder(tmp_path)
        recorder.start_batch(pipeline="csv-trigger", batch_id="b1")
        with StageTimer(recorder, pipeline="csv-trigger", stage="S5", batch_id="b1") as t:
            time.sleep(0.05)
            t.exclude(0.05)
        summary = recorder._stage_buckets["S5"].summary()  # type: ignore[attr-defined]
        assert summary["count"] == 1
        assert summary["sum_ms"] < 20

    def test_exclude_never_goes_negative(self, tmp_path: Path) -> None:
        recorder = _make_recorder(tmp_path)
        recorder.start_batch(pipeline="csv-trigger", batch_id="b1")
        with StageTimer(recorder, pipeline="csv-trigger", stage="S5", batch_id="b1") as t:
            t.exclude(10.0)
            t.exclude(-5.0)
        summary = recorder._stage_buckets["S5"].summary()  # type: ignore[attr-defined]
        assert summary["sum_ms"] == 0.0


# ---------------------------------------------------------------------------
# MetricsRecorder.close_batch
# ---------------------------------------------------------------------------


class _RecordCollector(logging.Handler):
    """Test-only handler — direct capture from a specific logger.

    ``caplog`` attaches at root and only sees records that propagate
    up. The ``cmcourier.metrics.*`` loggers intentionally set
    ``propagate=False`` (to avoid duplicating metrics into the app
    log), so a direct handler is the reliable capture path for unit
    tests on those streams.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Ensure record.message is populated before downstream assertions.
        # Direct handlers (not chained through a Formatter) don't auto-fill
        # this; only Formatter.format() does. We materialize it here so
        # tests can rely on `record.message`.
        record.message = record.getMessage()
        self.records.append(record)


class TestMetricsRecorderCloseBatch:
    def test_close_batch_emits_summary_with_throughput(self, tmp_path: Path) -> None:
        recorder = _make_recorder(tmp_path)
        recorder.start_batch(pipeline="csv-trigger", batch_id="b1")
        recorder.record_stage(stage="S0", duration_ms=10.0)
        recorder.record_stage(stage="S0", duration_ms=20.0)
        recorder.record_stage(stage="S5", duration_ms=200.0)

        capture = _RecordCollector()
        pipeline_logger = logging.getLogger("cmcourier.metrics.pipeline")
        pipeline_logger.addHandler(capture)
        previous_level = pipeline_logger.level
        previous_disabled = pipeline_logger.disabled
        pipeline_logger.setLevel(logging.INFO)
        pipeline_logger.disabled = False
        try:
            recorder.close_batch(
                pipeline="csv-trigger",
                batch_id="b1",
                total_docs=2,
                elapsed_s=4.0,
            )
        finally:
            pipeline_logger.removeHandler(capture)
            pipeline_logger.setLevel(previous_level)
            pipeline_logger.disabled = previous_disabled

        summaries = [r for r in capture.records if r.message == "batch_summary"]
        assert len(summaries) == 1
        rec = summaries[0]
        assert rec.__dict__["pipeline"] == "csv-trigger"
        assert rec.__dict__["batch_id"] == "b1"
        assert rec.__dict__["total_docs"] == 2
        assert rec.__dict__["throughput_docs_per_s"] == pytest.approx(0.5)
        stages = rec.__dict__["stages"]
        assert stages["S0"]["count"] == 2
        assert stages["S5"]["count"] == 1

    def test_close_batch_disabled_no_emit(self, tmp_path: Path) -> None:
        recorder = MetricsRecorder(
            log_dir=tmp_path / "logs",
            slow_op_threshold_ms=0.0,
            slow_op_top_n=10,
            enabled=False,
            pipeline_metrics_enabled=False,
        )
        recorder.start_batch(pipeline="x", batch_id="b1")

        capture = _RecordCollector()
        pipeline_logger = logging.getLogger("cmcourier.metrics.pipeline")
        pipeline_logger.addHandler(capture)
        pipeline_logger.setLevel(logging.INFO)
        try:
            recorder.close_batch(pipeline="x", batch_id="b1", total_docs=0, elapsed_s=0.0)
        finally:
            pipeline_logger.removeHandler(capture)

        assert [r for r in capture.records if r.message == "batch_summary"] == []


# ---------------------------------------------------------------------------
# 028 — concurrent-batch isolation
# ---------------------------------------------------------------------------


class TestConcurrentBatchIsolation:
    """Two recorders alive simultaneously must not see each other's
    slow ops. The handler filters by ``record.batch_id``."""

    def test_two_recorders_route_by_batch_id(self, tmp_path: Path) -> None:
        rec_a = _make_recorder(tmp_path / "a")
        rec_b = _make_recorder(tmp_path / "b")
        rec_a.start_batch(pipeline="p", batch_id="A")
        rec_b.start_batch(pipeline="p", batch_id="B")

        net_log = logging.getLogger("cmcourier.metrics.network")
        prev_level = net_log.level
        prev_disabled = net_log.disabled
        prev_propagate = net_log.propagate
        # Match production: setup.py sets propagate=False so the handler
        # on the parent ``cmcourier`` logger doesn't double-count.
        net_log.propagate = False
        net_log.setLevel(logging.INFO)
        net_log.disabled = False
        try:
            net_log.info(
                "cmis_upload",
                extra={"batch_id": "A", "kind": "cmis_upload", "duration_ms": 9000.0},
            )
            net_log.info(
                "cmis_upload",
                extra={"batch_id": "B", "kind": "cmis_upload", "duration_ms": 8000.0},
            )
        finally:
            net_log.setLevel(prev_level)
            net_log.disabled = prev_disabled
            net_log.propagate = prev_propagate

        top_a = rec_a.aggregator_snapshot()
        top_b = rec_b.aggregator_snapshot()
        rec_a.close_batch(pipeline="p", batch_id="A", total_docs=0, elapsed_s=0.0)
        rec_b.close_batch(pipeline="p", batch_id="B", total_docs=0, elapsed_s=0.0)

        # Each recorder only sees its own batch's record.
        assert len(top_a) == 1
        assert top_a[0]["duration_ms"] == 9000.0
        assert len(top_b) == 1
        assert top_b[0]["duration_ms"] == 8000.0

    def test_record_without_batch_id_dropped(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        rec.start_batch(pipeline="p", batch_id="A")
        net_log = logging.getLogger("cmcourier.metrics.network")
        prev_level = net_log.level
        prev_disabled = net_log.disabled
        prev_propagate = net_log.propagate
        # Match production: setup.py sets propagate=False so the handler
        # on the parent ``cmcourier`` logger doesn't double-count.
        net_log.propagate = False
        net_log.setLevel(logging.INFO)
        net_log.disabled = False
        try:
            # No batch_id in extras → handler drops it.
            net_log.info(
                "cmis_upload",
                extra={"kind": "cmis_upload", "duration_ms": 5000.0},
            )
        finally:
            net_log.setLevel(prev_level)
            net_log.disabled = prev_disabled
            net_log.propagate = prev_propagate
        snapshot = rec.aggregator_snapshot()
        rec.close_batch(pipeline="p", batch_id="A", total_docs=0, elapsed_s=0.0)
        assert snapshot == []

    def test_bandwidth_handler_filters_by_batch_id(self, tmp_path: Path) -> None:
        """042 — bandwidth handler must drop records from other batches.

        Pre-042 the handler only filtered by ``kind=="cmis_upload"``, so
        with ``batches_in_flight>1`` two live handlers were attached to
        ``cmcourier.metrics.network`` and each cmis_upload event bled into
        both samplers. This test asserts a foreign-batch record does NOT
        advance the recorder's own cumulative byte counter.
        """
        rec = _make_recorder(tmp_path)
        rec.start_batch(pipeline="p", batch_id="A")
        net_log = logging.getLogger("cmcourier.metrics.network")
        prev_level = net_log.level
        prev_disabled = net_log.disabled
        prev_propagate = net_log.propagate
        net_log.propagate = False
        net_log.setLevel(logging.INFO)
        net_log.disabled = False
        try:
            net_log.info(
                "cmis_upload",
                extra={
                    "batch_id": "A",
                    "kind": "cmis_upload",
                    "duration_ms": 100.0,
                    "size_bytes": 1024,
                },
            )
            net_log.info(
                "cmis_upload",
                extra={
                    "batch_id": "OTHER",  # foreign chunk, must be ignored
                    "kind": "cmis_upload",
                    "duration_ms": 200.0,
                    "size_bytes": 2048,
                },
            )
        finally:
            net_log.setLevel(prev_level)
            net_log.disabled = prev_disabled
            net_log.propagate = prev_propagate
        # Only the matching record reaches the sampler.
        assert rec.bandwidth.cumulative_bytes() == 1024
        # Slow-op aggregator filter (pre-042 behavior) still works.
        snapshot = rec.aggregator_snapshot()
        rec.close_batch(pipeline="p", batch_id="A", total_docs=0, elapsed_s=0.0)
        assert len(snapshot) == 1
        assert snapshot[0]["duration_ms"] == 100.0

    def test_bandwidth_handler_accepts_matching_batch_id(self, tmp_path: Path) -> None:
        """042 — multiple matching records aggregate into cumulative_bytes."""
        rec = _make_recorder(tmp_path)
        rec.start_batch(pipeline="p", batch_id="A")
        net_log = logging.getLogger("cmcourier.metrics.network")
        prev_level = net_log.level
        prev_disabled = net_log.disabled
        prev_propagate = net_log.propagate
        net_log.propagate = False
        net_log.setLevel(logging.INFO)
        net_log.disabled = False
        try:
            for size in (1024, 2048, 4096):
                net_log.info(
                    "cmis_upload",
                    extra={
                        "batch_id": "A",
                        "kind": "cmis_upload",
                        "duration_ms": 100.0,
                        "size_bytes": size,
                    },
                )
        finally:
            net_log.setLevel(prev_level)
            net_log.disabled = prev_disabled
            net_log.propagate = prev_propagate
        assert rec.bandwidth.cumulative_bytes() == 1024 + 2048 + 4096
        rec.close_batch(pipeline="p", batch_id="A", total_docs=0, elapsed_s=0.0)


# ---------------------------------------------------------------------------
# 042 — per-recorder live S5 outcome counters
# ---------------------------------------------------------------------------


class TestUploadOutcomeCounters042:
    """The counters added in 042 Phase 2 let the data_provider surface live
    s5_done / s5_failed on the CHUNKS row mid-flight, instead of waiting
    for the DONE transition to write them."""

    def test_initial_counts_are_zero(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        assert rec.upload_done_count() == 0
        assert rec.upload_failed_count() == 0
        assert rec.upload_skipped_count() == 0  # 041 parity

    def test_record_upload_done_increments(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        for _ in range(7):
            rec.record_upload_done()
        assert rec.upload_done_count() == 7
        assert rec.upload_failed_count() == 0

    def test_record_upload_failed_increments(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        for _ in range(3):
            rec.record_upload_failed()
        assert rec.upload_failed_count() == 3
        assert rec.upload_done_count() == 0

    def test_done_counter_thread_safe(self, tmp_path: Path) -> None:
        import threading as _t

        rec = _make_recorder(tmp_path)
        n_workers = 32
        per_worker = 100

        def _hammer() -> None:
            for _ in range(per_worker):
                rec.record_upload_done()

        threads = [_t.Thread(target=_hammer) for _ in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert rec.upload_done_count() == n_workers * per_worker


class TestCurrentStageP95WithCount061:
    """061 — the AIMD reads (p95, count) atomically so it can gate on a
    minimum sample count. ``current_stage_p95_with_count`` returns both
    in one shot, holding the bucket lock once."""

    def test_empty_stage_returns_zero_tuple(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        assert rec.current_stage_p95_with_count("S5") == (0.0, 0)

    def test_populated_stage_returns_p95_and_count(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        for d in (100.0, 200.0, 300.0):
            rec.record_stage(stage="S5", duration_ms=d)
        p95, count = rec.current_stage_p95_with_count("S5")
        assert count == 3
        assert p95 == 300.0  # nearest-rank p95 of 3 sorted samples


class TestBandwidthSamplerDistribution069:
    """069: record_upload distributes bytes uniformly over the
    transmission window instead of crediting all to the completion
    second. Restores faithful current_mbps / peak / sparkline."""

    def _new_sampler(self):  # type: ignore[no-untyped-def]
        from cmcourier.observability.metrics import _BandwidthSampler

        return _BandwidthSampler()

    def test_whole_second_buckets_get_equal_share(self) -> None:
        # 30 MB over exactly 3 seconds: 10 MB per bucket.
        sampler = self._new_sampler()
        sampler.record_upload(30_000_000, started_at=10.0, completed_at=13.0)
        with sampler._lock:  # noqa: SLF001 — test inspection only
            buckets = dict(sampler._buckets)  # noqa: SLF001
        assert buckets == {10: 10_000_000, 11: 10_000_000, 12: 10_000_000}

    def test_fractional_interval_distributes_proportionally(self) -> None:
        # 30 MB from t=10.5 to t=13.5 (3.0s span):
        #   bucket 10: 0.5s × 10 MB/s = 5 MB
        #   bucket 11: 1.0s × 10 MB/s = 10 MB
        #   bucket 12: 1.0s × 10 MB/s = 10 MB
        #   bucket 13: 0.5s × 10 MB/s = 5 MB
        sampler = self._new_sampler()
        sampler.record_upload(30_000_000, started_at=10.5, completed_at=13.5)
        with sampler._lock:  # noqa: SLF001
            buckets = dict(sampler._buckets)  # noqa: SLF001
        assert buckets == {
            10: 5_000_000,
            11: 10_000_000,
            12: 10_000_000,
            13: 5_000_000,
        }

    def test_sub_second_upload_lands_entirely_in_one_bucket(self) -> None:
        # 1 MB transmitted in 0.5s entirely within bucket 10.
        sampler = self._new_sampler()
        sampler.record_upload(1_000_000, started_at=10.0, completed_at=10.5)
        with sampler._lock:  # noqa: SLF001
            buckets = dict(sampler._buckets)  # noqa: SLF001
        assert buckets == {10: 1_000_000}

    def test_zero_duration_falls_back_to_completion_bucket(self) -> None:
        # Defensive: when duration is zero, credit at completion.
        sampler = self._new_sampler()
        sampler.record_upload(5_000_000, started_at=10.0, completed_at=10.0)
        with sampler._lock:  # noqa: SLF001
            buckets = dict(sampler._buckets)  # noqa: SLF001
        assert buckets == {10: 5_000_000}

    def test_cumulative_bytes_preserved_across_uploads(self) -> None:
        sampler = self._new_sampler()
        sampler.record_upload(10_000_000, started_at=10.0, completed_at=11.0)
        sampler.record_upload(20_000_000, started_at=12.0, completed_at=14.0)
        sampler.record_upload(5_000_000, started_at=15.0, completed_at=15.5)
        assert sampler.cumulative_bytes() == 35_000_000

    def test_peak_reflects_sustained_throughput_not_completion_spike(self) -> None:
        # 30 MB over 3s → max single-bucket rate is 10 MB/s, NOT 30 MB/s.
        sampler = self._new_sampler()
        sampler.record_upload(30_000_000, started_at=10.0, completed_at=13.0)
        assert sampler.peak_mbps() == 10.0


class TestBandwidthHandlerDerivesStartedAt069:
    """069: _BandwidthHandler reads ``duration_ms`` off the log record
    and derives ``started_at = completed_at - duration_ms / 1000``."""

    def _new_handler(self):  # type: ignore[no-untyped-def]
        from cmcourier.observability.metrics import _BandwidthHandler, _BandwidthSampler

        sampler = _BandwidthSampler()
        handler = _BandwidthHandler(sampler, batch_id="B1")
        return sampler, handler

    def test_handler_derives_window_from_duration_ms(self) -> None:
        import logging as _l

        sampler, handler = self._new_handler()
        record = _l.LogRecord(
            name="cmcourier.metrics.network",
            level=_l.INFO,
            pathname="x",
            lineno=1,
            msg="cmis_upload",
            args=(),
            exc_info=None,
        )
        record.kind = "cmis_upload"
        record.batch_id = "B1"
        record.size_bytes = 30_000_000
        record.duration_ms = 3000.0  # 3 seconds
        record.created = 13.0  # completion at t=13
        handler.emit(record)
        with sampler._lock:  # noqa: SLF001
            buckets = dict(sampler._buckets)  # noqa: SLF001
        # started_at = 13.0 - 3.0 = 10.0 → 10 MB to each of {10, 11, 12}.
        assert buckets == {10: 10_000_000, 11: 10_000_000, 12: 10_000_000}

    def test_handler_falls_back_when_duration_missing(self) -> None:
        import logging as _l

        sampler, handler = self._new_handler()
        record = _l.LogRecord(
            name="cmcourier.metrics.network",
            level=_l.INFO,
            pathname="x",
            lineno=1,
            msg="cmis_upload",
            args=(),
            exc_info=None,
        )
        record.kind = "cmis_upload"
        record.batch_id = "B1"
        record.size_bytes = 5_000_000
        record.created = 13.0
        # No duration_ms set → defensive fallback credits all at completion.
        handler.emit(record)
        with sampler._lock:  # noqa: SLF001
            buckets = dict(sampler._buckets)  # noqa: SLF001
        assert buckets == {13: 5_000_000}
