"""Unit tests for :class:`TUIDataProvider` (025 phase 3)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cmcourier.config.schema import AutoTuneConfig, CmisConfigModel
from cmcourier.observability.metrics import MetricsRecorder
from cmcourier.orchestrators.multi_batch import ChunkState
from cmcourier.services.worker_pool_stats import ResizableSemaphore, WorkerPoolStats
from cmcourier.tui.data_provider import PREP_STAGES, UPLOAD_STAGE, TUIDataProvider

pytestmark = pytest.mark.unit


def _make_provider(
    tmp_path: Path,
    *,
    tracking_store: object | None = None,
) -> tuple[
    TUIDataProvider,
    MetricsRecorder,
    WorkerPoolStats,
    ResizableSemaphore,
]:
    recorder = MetricsRecorder(
        log_dir=tmp_path / "logs",
        slow_op_threshold_ms=0.0,
        slow_op_top_n=10,
        enabled=True,
        pipeline_metrics_enabled=True,
    )
    pool_stats = WorkerPoolStats()
    sem = ResizableSemaphore(4)
    cmis = CmisConfigModel(
        base_url="http://cmis.bank.test:9080/cmis",
        repo_id="$x!t",
        workers=4,
        max_bandwidth_mbps=50.0,
        auto_tune=AutoTuneConfig(enabled=True, target_p95_ms=5000.0),
    )
    uploader = MagicMock()
    uploader._timeout_s = 300.0
    provider = TUIDataProvider(
        pipeline_name="csv-trigger",
        metrics_recorder=recorder,
        pool_stats=pool_stats,
        concurrency_limit=sem,
        cmis_config=cmis,
        uploader=uploader,
        auto_tune=None,
        tracking_store=tracking_store,  # type: ignore[arg-type]
    )
    return provider, recorder, pool_stats, sem


class TestTUIDataProvider:
    def test_snapshot_baseline_fields(self, tmp_path: Path) -> None:
        provider, _r, _p, _s = _make_provider(tmp_path)
        snap = provider.snapshot()
        assert snap.pipeline == "csv-trigger"
        assert snap.is_complete is False
        assert snap.pool_capacity == 4
        # 081: max_bandwidth_mbps es Mbps real; el chart muestra MB/s
        # (Mbps / 8). 50 Mbps = 6.25 MB/s.
        assert snap.bandwidth_ceiling_mbps == 6.25
        assert PREP_STAGES == ("S0", "S1", "S2", "S3", "S4")
        assert UPLOAD_STAGE == "S5"

    def test_stages_reflect_recorded_data(self, tmp_path: Path) -> None:
        provider, recorder, _p, _s = _make_provider(tmp_path)
        recorder.record_stage(stage="S1", duration_ms=10.0)
        recorder.record_stage(stage="S5", duration_ms=500.0)
        snap = provider.snapshot()
        assert int(snap.stages["S1"]["count"]) == 1
        assert float(snap.stages["S5"]["p95_ms"]) == 500.0

    def test_closing_phase_passes_through_from_pool_stats(self, tmp_path: Path) -> None:
        # 144: el monitor lee la fase de cierre desde el snapshot.
        from cmcourier.services.worker_pool_stats import ClosingPhase

        provider, _r, pool, _s = _make_provider(tmp_path)
        assert provider.snapshot().closing is None
        pool.set_closing("sincronizando AS400", 50, 120)
        assert provider.snapshot().closing == ClosingPhase("sincronizando AS400", 50, 120)
        pool.clear_closing()
        assert provider.snapshot().closing is None

    def test_pool_in_use_reflects_busy_workers(self, tmp_path: Path) -> None:
        provider, _r, pool, sem = _make_provider(tmp_path)
        sem.set_capacity(6)
        pool.set_pool_size(6)
        pool.mark_busy("w1")
        pool.mark_busy("w2")
        snap = provider.snapshot()
        assert snap.pool_capacity == 6
        assert snap.pool_in_use == 2
        assert snap.pool_idle == 4

    def test_auto_tune_disabled_when_no_controller(self, tmp_path: Path) -> None:
        provider, _r, _p, _s = _make_provider(tmp_path)
        snap = provider.snapshot()
        # cmis.auto_tune.enabled=True in the test config, but the controller
        # itself is None — so the "last move" fields are placeholders.
        assert snap.auto_tune_last_action == "—"
        assert snap.auto_tune_seconds_since_last_decision is None

    def test_bandwidth_ceiling_zero_means_auto_scale(self, tmp_path: Path) -> None:
        cmis = CmisConfigModel(
            base_url="x",
            repo_id="y",
            workers=4,
            max_bandwidth_mbps=0.0,
        )
        recorder = MetricsRecorder(
            log_dir=tmp_path / "logs",
            slow_op_threshold_ms=0.0,
            slow_op_top_n=10,
        )
        uploader = MagicMock()
        uploader._timeout_s = 300.0
        provider = TUIDataProvider(
            pipeline_name="x",
            metrics_recorder=recorder,
            pool_stats=WorkerPoolStats(),
            concurrency_limit=ResizableSemaphore(2),
            cmis_config=cmis,
            uploader=uploader,
        )
        snap = provider.snapshot()
        assert snap.bandwidth_ceiling_mbps == 0.0

    def test_mark_batch_lifecycle(self, tmp_path: Path) -> None:
        provider, _r, _p, _s = _make_provider(tmp_path)
        provider.mark_batch_started("batch_xyz")
        snap = provider.snapshot()
        assert snap.batch_id == "batch_xyz"
        assert snap.is_complete is False
        provider.mark_batch_complete()
        assert provider.snapshot().is_complete is True

    def test_elapsed_ticks_while_running(self, tmp_path: Path) -> None:
        provider, _r, _p, _s = _make_provider(tmp_path)
        provider.mark_batch_started("b")
        e1 = provider.snapshot().elapsed_s
        time.sleep(0.05)
        e2 = provider.snapshot().elapsed_s
        assert e2 > e1  # still running → the clock advances

    def test_elapsed_frozen_after_complete(self, tmp_path: Path) -> None:
        # 052: the run timer must FREEZE at completion, not tick forever.
        provider, _r, _p, _s = _make_provider(tmp_path)
        provider.mark_batch_started("b")
        provider.mark_batch_complete()
        e1 = provider.snapshot().elapsed_s
        time.sleep(0.05)
        e2 = provider.snapshot().elapsed_s
        assert e1 == e2

    def test_docs_for_batch_delegates_to_tracking_store(self, tmp_path: Path) -> None:
        # 052: the DETAIL drill-down reads per-doc detail from the store.
        from cmcourier.domain.models import DocDetail

        store = MagicMock()
        store.list_docs_for_batch.return_value = [
            DocDetail(
                txn_num="T1",
                file_name="f.001",
                status="S5_DONE",
                error_message="",
                file_size_bytes=10,
            )
        ]
        provider, _r, _p, _s = _make_provider(tmp_path, tracking_store=store)
        docs = provider.docs_for_batch("B1")
        assert [d.txn_num for d in docs] == ["T1"]
        store.list_docs_for_batch.assert_called_once_with("B1")

    def test_docs_for_batch_empty_without_store(self, tmp_path: Path) -> None:
        # No store wired (e.g. monolithic resume path) → empty, never crashes.
        provider, _r, _p, _s = _make_provider(tmp_path)
        assert provider.docs_for_batch("B1") == []
        assert provider.docs_for_batch("") == []

    def test_mode_defaults_to_batched(self, tmp_path: Path) -> None:
        provider, *_ = _make_provider(tmp_path)
        assert provider.mode == "batched"
        snap = provider.snapshot()
        assert snap.mode == "batched"
        assert snap.bucket is None

    def test_mode_streaming_propagates(self, tmp_path: Path) -> None:
        from cmcourier.orchestrators.streaming import StreamingSnapshot

        snap_value = StreamingSnapshot(
            bucket_level=3,
            bucket_cap=10,
            bucket_peak=7,
            prep_workers=4,
            prep_in_flight=2,
            upload_workers=8,
            prep_docs_per_s=15.5,
            upload_docs_per_s=14.0,
        )
        recorder = MetricsRecorder(
            log_dir=tmp_path / "logs",
            slow_op_threshold_ms=0.0,
            slow_op_top_n=10,
            enabled=True,
            pipeline_metrics_enabled=True,
        )
        cmis = CmisConfigModel(
            base_url="http://x",
            repo_id="r",
            workers=4,
            max_bandwidth_mbps=0.0,
            auto_tune=AutoTuneConfig(enabled=False),
        )
        uploader = MagicMock()
        uploader._timeout_s = 300.0
        provider = TUIDataProvider(
            pipeline_name="csv-trigger",
            metrics_recorder=recorder,
            pool_stats=WorkerPoolStats(),
            concurrency_limit=ResizableSemaphore(4),
            cmis_config=cmis,
            uploader=uploader,
            mode="streaming",
            bucket_provider=lambda: snap_value,
        )
        snap = provider.snapshot()
        assert snap.mode == "streaming"
        assert snap.bucket is not None
        assert snap.bucket.bucket_level == 3
        assert snap.bucket.bucket_cap == 10
        assert snap.bucket.prep_in_flight == 2

    def test_slow_ops_passes_through_aggregator(self, tmp_path: Path) -> None:
        provider, recorder, _p, _s = _make_provider(tmp_path)
        recorder.start_batch(pipeline="csv-trigger", batch_id="b1")
        # Inject a slow-op record by routing through the network logger.
        # Force INFO level — earlier tests may have set it to CRITICAL+1 via
        # observability.setup.configure.
        import logging as _logging

        net_log = _logging.getLogger("cmcourier.metrics.network")
        prev_level = net_log.level
        net_log.setLevel(_logging.INFO)
        try:
            net_log.info(
                "cmis_upload",
                extra={
                    "batch_id": "b1",
                    "kind": "cmis_upload",
                    "duration_ms": 9999.0,
                    "txn_num": "TXN_S",
                    "worker": "cmcourier-s5_3",
                    "size_bytes": 1024,
                },
            )
            snap = provider.snapshot()
            assert any(op.get("txn_num") == "TXN_S" for op in snap.slow_ops_all)
        finally:
            net_log.setLevel(prev_level)
            recorder.close_batch(pipeline="csv-trigger", batch_id="b1", total_docs=0, elapsed_s=1.0)


# ---------------------------------------------------------------------------
# 054: UPLOAD-tab recorder wiring — the multi-batch shape where the PREP-side
# and UPLOAD-side recorders DIVERGE. Pre-054 the single-recorder helper above
# could never exercise this, so two wiring bugs shipped (042 fallout).
# ---------------------------------------------------------------------------


def _make_dual_provider(
    tmp_path: Path,
    *,
    chunks: list[object] | None = None,
) -> tuple[TUIDataProvider, MetricsRecorder, MetricsRecorder]:
    """Provider wired with a PREP recorder (`recorder_provider`) and a
    distinct UPLOAD recorder (`upload_recorder_provider`) — the N=2 shape."""

    def _rec(name: str) -> MetricsRecorder:
        return MetricsRecorder(
            log_dir=tmp_path / f"logs-{name}",
            slow_op_threshold_ms=0.0,
            slow_op_top_n=10,
            enabled=True,
            pipeline_metrics_enabled=True,
        )

    prep_rec = _rec("prep")
    upload_rec = _rec("upload")
    cmis = CmisConfigModel(
        base_url="http://cmis.bank.test:9080/cmis",
        repo_id="$x!t",
        workers=4,
        max_bandwidth_mbps=50.0,
    )
    uploader = MagicMock()
    uploader._timeout_s = 300.0
    provider = TUIDataProvider(
        pipeline_name="csv-trigger",
        metrics_recorder=prep_rec,
        pool_stats=WorkerPoolStats(),
        concurrency_limit=ResizableSemaphore(4),
        cmis_config=cmis,
        uploader=uploader,
        recorder_provider=lambda: prep_rec,
        upload_recorder_provider=lambda: upload_rec,
        chunks_provider=(lambda: list(chunks)) if chunks is not None else None,
    )
    return provider, prep_rec, upload_rec


class TestUploadRecorderWiring054:
    def test_bandwidth_reads_upload_recorder_not_prep(self, tmp_path: Path) -> None:
        # Bytes land in the UPLOAD recorder's sampler; the PREP recorder stays
        # empty. Pre-054 the snapshot read the PREP recorder → 0 / blank.
        provider, _prep, upload_rec = _make_dual_provider(tmp_path)
        now = int(time.time())
        for bucket in (now - 2, now - 1, now):
            # 069: signature is now (size, *, started_at, completed_at).
            # Same-second upload → all bytes land in `bucket`.
            upload_rec.bandwidth.record_upload(
                8_000_000,
                started_at=float(bucket),
                completed_at=float(bucket) + 0.5,
            )
        snap = provider.snapshot()
        assert snap.bandwidth_peak_mbps > 0.0
        assert snap.bandwidth_current_mbps > 0.0
        assert any(v > 0.0 for _, v in snap.bandwidth_series)

    def test_slow_ops_read_upload_recorder_not_prep(self, tmp_path: Path) -> None:
        provider, _prep, upload_rec = _make_dual_provider(tmp_path)
        upload_rec.start_batch(pipeline="csv-trigger", batch_id="UB")
        import logging as _logging

        net_log = _logging.getLogger("cmcourier.metrics.network")
        prev = net_log.level
        net_log.setLevel(_logging.INFO)
        try:
            net_log.info(
                "cmis_upload",
                extra={
                    "batch_id": "UB",
                    "kind": "cmis_upload",
                    "duration_ms": 9999.0,
                    "txn_num": "TXN_UP",
                    "worker": "cmcourier-s5_1",
                    "size_bytes": 4096,
                },
            )
            snap = provider.snapshot()
            assert any(op.get("txn_num") == "TXN_UP" for op in snap.slow_ops_all)
        finally:
            net_log.setLevel(prev)
            upload_rec.close_batch(
                pipeline="csv-trigger", batch_id="UB", total_docs=0, elapsed_s=1.0
            )

    def test_current_chunk_elapsed_measures_from_upload_start(self, tmp_path: Path) -> None:
        # PREP began 10 min ago, S5 began 5 s ago. The timer must show the
        # S5 window, not prep+upload.
        now = time.monotonic()
        chunk = ChunkState(
            chunk_idx=0,
            batch_id="B0",
            status="UPLOAD",
            total_bytes=10_000_000,
            prep_started_monotonic=now - 600.0,
            upload_started_monotonic=now - 5.0,
        )
        provider, _prep, _upload = _make_dual_provider(tmp_path, chunks=[chunk])
        snap = provider.snapshot()
        assert 5.0 <= snap.current_chunk_elapsed_s < 60.0  # the S5 window, not 600 s

    def test_current_chunk_elapsed_done_uses_frozen_upload_elapsed(self, tmp_path: Path) -> None:
        chunk = ChunkState(
            chunk_idx=0,
            batch_id="B0",
            status="DONE",
            total_bytes=10_000_000,
            prep_started_monotonic=time.monotonic() - 600.0,
            upload_started_monotonic=time.monotonic() - 500.0,
            upload_elapsed_s=42.0,
        )
        provider, _prep, _upload = _make_dual_provider(tmp_path, chunks=[chunk])
        snap = provider.snapshot()
        assert snap.current_chunk_elapsed_s == 42.0  # the frozen S5 duration

    def test_current_chunk_elapsed_prep_is_zero(self, tmp_path: Path) -> None:
        chunk = ChunkState(
            chunk_idx=0,
            batch_id="B0",
            status="PREP",
            total_bytes=10_000_000,
            prep_started_monotonic=time.monotonic() - 30.0,
        )
        provider, _prep, _upload = _make_dual_provider(tmp_path, chunks=[chunk])
        snap = provider.snapshot()
        assert snap.current_chunk_elapsed_s == 0.0  # S5 hasn't started yet

    def test_current_chunk_avg_mbps_uses_upload_window(self, tmp_path: Path) -> None:
        # 10 MB uploaded over a ~5 s S5 window → ~2 MB/s. If the timer still
        # used the 600 s prep window the average would be a tiny fraction.
        now = time.monotonic()
        chunk = ChunkState(
            chunk_idx=0,
            batch_id="B0",
            status="UPLOAD",
            total_bytes=20_000_000,
            prep_started_monotonic=now - 600.0,
            upload_started_monotonic=now - 5.0,
        )
        provider, _prep, upload_rec = _make_dual_provider(tmp_path, chunks=[chunk])
        # 069 signature.
        _t = time.time()
        upload_rec.bandwidth.record_upload(10 * 1_048_576, started_at=_t - 0.5, completed_at=_t)
        snap = provider.snapshot()
        assert snap.current_chunk_avg_mbps > 0.5  # ~2 MB/s, not 10MB/600s
