"""Unit tests for ``StreamingOrchestrator`` (063)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cmcourier.config.schema import (
    HeavyLightLanesConfig,
    ObservabilityConfig,
    PipelineConfig,
    ProcessingConfig,
    StreamingConfig,
)
from cmcourier.domain.models import TriggerRecord
from cmcourier.orchestrators.multi_batch import MultiBatchRunReport
from cmcourier.orchestrators.streaming import StreamingOrchestrator, _TriggerIter

# ---------------------------------------------------------------------------
# Stub pipeline
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Stand-in for StagedPipeline — covers the StreamingOrchestrator surface."""

    pipeline_name = "csv-trigger"

    def __init__(
        self,
        *,
        triggers: list[TriggerRecord],
        prep_sleep_s: float = 0.0,
        upload_sleep_s: float = 0.0,
        prep_returns_none_indexes: tuple[int, ...] = (),
        upload_outcome_by_idx: dict[int, str] | None = None,
        pool_ceiling: int = 2,
        size_bytes_by_idx: dict[int, int] | None = None,
    ) -> None:
        self._triggers = triggers
        self._prep_sleep = prep_sleep_s
        self._upload_sleep = upload_sleep_s
        self._prep_returns_none = set(prep_returns_none_indexes)
        self._upload_outcomes = upload_outcome_by_idx or {}
        self._pool_ceiling_value = pool_ceiling
        self._size_bytes_by_idx = size_bytes_by_idx or {}
        self.prep_calls: list[str] = []
        self.upload_calls: list[str] = []
        self.lane_calls: list[tuple[str, str]] = []
        self.warm_calls: list[int] = []
        self.lock = threading.Lock()
        self._batch_counter = 0
        self._idx_for_trigger: dict[int, int] = {id(t): i for i, t in enumerate(triggers)}

        class _S:
            def acquire(self, descriptor: str) -> Iterator[TriggerRecord]:  # noqa: ARG002, PLR6301
                yield from triggers

        self._trigger_strategy = _S()
        self._tracking_store = MagicMock()
        self._tracking_store.start_batch = self._fake_start_batch
        self._tracking_store.flush = MagicMock()
        self._tracking_store.complete_batch = MagicMock()
        self.auto_tune_controller = None
        self.sampler = None
        # 067: streaming orchestrator publishes live pending counts here.
        # Use a real WorkerPoolStats so its snapshot returns the values
        # the orchestrator wrote.
        from cmcourier.services.worker_pool_stats import WorkerPoolStats

        self.pool_stats = WorkerPoolStats()
        # 070: the streaming orchestrator now reuses the pipeline's
        # LaneController instead of constructing its own. The fake needs
        # to expose one when the orchestrator's config has lanes enabled
        # (the build_orch helper toggles it on the YAML side only — the
        # fake supplies the actual controller instance here).
        self.lane_controller = None

    def _fake_start_batch(self, *, total_records: int) -> str:  # noqa: ARG002
        self._batch_counter += 1
        return f"B{self._batch_counter}"

    def _pool_ceiling(self) -> int:
        return self._pool_ceiling_value

    def warm_upload_pool(self, workers: int) -> None:
        self.warm_calls.append(workers)

    def streaming_prep_one(self, trigger, batch_id: str, recorder):  # noqa: ARG002
        if self._prep_sleep:
            time.sleep(self._prep_sleep)
        idx = self._idx_for_trigger.get(id(trigger), -1)
        with self.lock:
            self.prep_calls.append(getattr(trigger, "shortname", str(idx)))
        if idx in self._prep_returns_none:
            return None, 0, 0
        size = self._size_bytes_by_idx.get(idx, 0)
        return (
            SimpleNamespace(
                __idx__=idx,
                document=SimpleNamespace(txn_num=f"TXN_{idx}"),
                staged_file=SimpleNamespace(size_bytes=size),
            ),
            0,
            0,
        )

    def streaming_upload_one(self, item, batch_id: str, recorder, lane=None):  # noqa: ARG002
        if self._upload_sleep:
            time.sleep(self._upload_sleep)
        idx = item.__idx__
        with self.lock:
            self.upload_calls.append(item.document.txn_num)
            if lane is not None:
                self.lane_calls.append((item.document.txn_num, lane))
        return self._upload_outcomes.get(idx, "done")


def _make_triggers(n: int) -> list[TriggerRecord]:
    return [TriggerRecord(shortname=f"SN_{i}", cif=str(i), system_id="1") for i in range(n)]


def _build_orch(
    pipeline,
    tmp_path: Path,
    *,
    bucket_size: int = 8,
    prep_workers: int = 1,
    lanes_enabled: bool = False,
    heavy_threshold_bytes: int = 10 * 1024 * 1024,
) -> StreamingOrchestrator:
    cfg = MagicMock(spec=PipelineConfig)
    cfg.observability = ObservabilityConfig(log_dir=tmp_path)
    cfg.processing = ProcessingConfig(
        mode="streaming",
        streaming=StreamingConfig(bucket_size=bucket_size),
        prep_workers=prep_workers,
        heavy_light_lanes=HeavyLightLanesConfig(
            enabled=lanes_enabled,
            heavy_threshold_bytes=heavy_threshold_bytes,
        ),
    )
    # 070: the orchestrator now reads pipeline.lane_controller instead
    # of building its own. Provision the fake to match production
    # wiring (StagedPipeline owns the controller).
    if lanes_enabled:
        from cmcourier.services.lane_controller import LaneController

        pipeline.lane_controller = LaneController(
            total_budget=pipeline._pool_ceiling(),
            heavy_initial_ratio=cfg.processing.heavy_light_lanes.heavy_initial_ratio,
            rebalance_interval_s=cfg.processing.heavy_light_lanes.rebalance_interval_s,
            idle_threshold_s=cfg.processing.heavy_light_lanes.idle_threshold_s,
        )
    return StreamingOrchestrator(pipeline=pipeline, config=cfg, log_dir=tmp_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTriggerIter:
    def test_concurrent_consumers_each_see_each_trigger_exactly_once(self) -> None:
        triggers = list(range(200))
        shared = _TriggerIter(iter(triggers))
        seen: list[int] = []
        seen_lock = threading.Lock()

        def consumer() -> None:
            while True:
                try:
                    value = next(shared)
                except StopIteration:
                    return
                with seen_lock:
                    seen.append(value)

        threads = [threading.Thread(target=consumer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(seen) == triggers
        assert shared.count == len(triggers)


class TestStreamingOrchestrator:
    def test_happy_path_uploads_all(self, tmp_path: Path) -> None:
        triggers = _make_triggers(10)
        pipeline = _FakePipeline(triggers=triggers, pool_ceiling=4)
        orch = _build_orch(pipeline, tmp_path, bucket_size=4, prep_workers=2)

        report = orch.run(
            source_descriptor="",
            batch_size=100,
            batches_in_flight=2,
        )
        assert isinstance(report, MultiBatchRunReport)
        assert len(report.chunks) == 1
        run = report.chunks[0]
        assert run.s5_done == 10
        assert run.s5_failed == 0
        assert run.total_triggers == 10
        assert pipeline.warm_calls == [4]
        # Every trigger went through prep + upload exactly once.
        assert sorted(pipeline.upload_calls) == [f"TXN_{i}" for i in range(10)]

    def test_empty_source_drains_cleanly(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers=[], pool_ceiling=3)
        orch = _build_orch(pipeline, tmp_path, bucket_size=2, prep_workers=2)
        report = orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)
        assert report.chunks[0].s5_done == 0
        assert report.chunks[0].total_triggers == 0

    def test_rejects_from_stage_gt_one(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers=_make_triggers(1))
        orch = _build_orch(pipeline, tmp_path)
        with pytest.raises(ValueError, match="from-stage"):
            orch.run(
                source_descriptor="",
                batch_size=1,
                batches_in_flight=2,
                from_stage=3,
            )

    def test_rejects_explicit_resume_batch_id(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers=_make_triggers(1))
        orch = _build_orch(pipeline, tmp_path)
        with pytest.raises(ValueError, match="batch-id"):
            orch.run(
                source_descriptor="",
                batch_size=1,
                batches_in_flight=2,
                resume_batch_id="B-existing",
            )

    def test_prep_failures_drop_silently(self, tmp_path: Path) -> None:
        # First and third triggers' prep returns None (filtered / cross-batch
        # skipped / failed); they never reach the bucket.
        triggers = _make_triggers(5)
        pipeline = _FakePipeline(
            triggers=triggers,
            prep_returns_none_indexes=(0, 2),
            pool_ceiling=2,
        )
        orch = _build_orch(pipeline, tmp_path, bucket_size=2, prep_workers=2)
        report = orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)
        run = report.chunks[0]
        assert run.s5_done == 3
        uploaded = sorted(pipeline.upload_calls)
        assert uploaded == ["TXN_1", "TXN_3", "TXN_4"]

    def test_bucket_caps_memory(self, tmp_path: Path) -> None:
        # 50 triggers, bucket_size=3. Slow consumers so the producers
        # stay ahead → bucket repeatedly fills to its cap.
        triggers = _make_triggers(50)
        pipeline = _FakePipeline(
            triggers=triggers,
            upload_sleep_s=0.005,
            pool_ceiling=2,
        )
        orch = _build_orch(pipeline, tmp_path, bucket_size=3, prep_workers=4)
        orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)
        # Peak qsize is sampled inside producers right after put(). Allow
        # the sampler one slot of head-room: a producer can observe the
        # post-put qsize before a consumer takes the just-pushed item, so
        # the witnessed peak can be exactly bucket_size.
        assert orch.peak_qsize <= 3

    def test_outcome_skipped_and_failed_counted(self, tmp_path: Path) -> None:
        triggers = _make_triggers(4)
        pipeline = _FakePipeline(
            triggers=triggers,
            pool_ceiling=2,
            upload_outcome_by_idx={1: "skipped", 2: "failed"},
        )
        orch = _build_orch(pipeline, tmp_path, bucket_size=4, prep_workers=2)
        report = orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)
        run = report.chunks[0]
        assert run.s5_done == 2
        assert run.s5_failed == 1
        assert run.total_docs == 4


class TestStreamingSnapshot:
    def test_initial_snapshot_is_zero(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers=_make_triggers(1), pool_ceiling=3)
        orch = _build_orch(pipeline, tmp_path, bucket_size=10, prep_workers=2)
        snap = orch.streaming_snapshot()
        assert snap.bucket_level == 0
        assert snap.bucket_cap == 10
        assert snap.bucket_peak == 0
        assert snap.prep_workers == 2
        assert snap.upload_workers == 3
        assert snap.prep_in_flight == 0
        assert snap.prep_docs_per_s == 0.0
        assert snap.upload_docs_per_s == 0.0

    def test_snapshot_after_run_records_peak_and_throughput(self, tmp_path: Path) -> None:
        triggers = _make_triggers(20)
        pipeline = _FakePipeline(triggers=triggers, pool_ceiling=4)
        orch = _build_orch(pipeline, tmp_path, bucket_size=4, prep_workers=2)
        orch.run(source_descriptor="", batch_size=100, batches_in_flight=2)
        snap = orch.streaming_snapshot()
        # Peak qsize is sampled after each producer put; over 20 docs
        # against a small bucket it must have been observed > 0 at least
        # once.
        assert snap.bucket_peak >= 1
        assert snap.bucket_level == 0  # drained at end-of-run
        # Throughput is rate-over-window; we just assert the windows ran.
        assert snap.prep_docs_per_s >= 0.0
        assert snap.upload_docs_per_s >= 0.0

    def test_prep_in_flight_visible_mid_run(self, tmp_path: Path) -> None:
        # Sleep producers; sample snapshot during the run by polling.
        triggers = _make_triggers(8)
        pipeline = _FakePipeline(
            triggers=triggers,
            pool_ceiling=2,
            prep_sleep_s=0.05,
        )
        orch = _build_orch(pipeline, tmp_path, bucket_size=2, prep_workers=3)
        observed_in_flight: list[int] = []

        def _poll() -> None:
            for _ in range(60):
                observed_in_flight.append(orch.prep_in_flight())
                time.sleep(0.01)

        t = threading.Thread(target=_poll)
        t.start()
        orch.run(source_descriptor="", batch_size=100, batches_in_flight=2)
        t.join()
        # At some point during the run, prep_in_flight was > 0.
        assert max(observed_in_flight) >= 1


class TestStreamingHeavyLightLanes:
    def test_dispatcher_routes_by_size(self, tmp_path: Path) -> None:
        # 10 triggers, sizes interleaving: half heavy (20 MB), half light (1 MB)
        triggers = _make_triggers(10)
        sizes = {i: (20 * 1024 * 1024 if i % 2 == 0 else 1 * 1024 * 1024) for i in range(10)}
        pipeline = _FakePipeline(triggers=triggers, pool_ceiling=4, size_bytes_by_idx=sizes)
        orch = _build_orch(
            pipeline,
            tmp_path,
            bucket_size=4,
            prep_workers=2,
            lanes_enabled=True,
            heavy_threshold_bytes=10 * 1024 * 1024,
        )
        report = orch.run(
            source_descriptor="",
            batch_size=100,
            batches_in_flight=2,
        )
        assert report.chunks[0].s5_done == 10
        # Each upload call was tagged with a lane: heavy items in heavy, light in light.
        heavy_txns = {txn for txn, lane in pipeline.lane_calls if lane == "heavy"}
        light_txns = {txn for txn, lane in pipeline.lane_calls if lane == "light"}
        assert heavy_txns == {f"TXN_{i}" for i in range(0, 10, 2)}
        assert light_txns == {f"TXN_{i}" for i in range(1, 10, 2)}

    def test_clean_shutdown_with_lanes_empty_source(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers=[], pool_ceiling=2)
        orch = _build_orch(pipeline, tmp_path, bucket_size=2, prep_workers=2, lanes_enabled=True)
        report = orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)
        assert report.chunks[0].s5_done == 0
        assert report.chunks[0].total_triggers == 0

    def test_streaming_snapshot_carries_lane_snapshot_when_enabled(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers=_make_triggers(2), pool_ceiling=2)
        orch = _build_orch(pipeline, tmp_path, lanes_enabled=True)
        snap = orch.streaming_snapshot()
        assert snap.lane_snapshot is not None
        assert snap.lane_snapshot.total_budget >= 2

    def test_streaming_snapshot_lane_none_when_disabled(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers=_make_triggers(2), pool_ceiling=2)
        orch = _build_orch(pipeline, tmp_path, lanes_enabled=False)
        snap = orch.streaming_snapshot()
        assert snap.lane_snapshot is None

    def test_streaming_reuses_pipeline_lane_controller_when_enabled(self, tmp_path: Path) -> None:
        # 070: pre-070 the orchestrator built its own LaneController and
        # the pipeline kept its own — TUI saw the dead one. Now both
        # paths must point at the same instance.
        pipeline = _FakePipeline(triggers=_make_triggers(2), pool_ceiling=2)
        orch = _build_orch(pipeline, tmp_path, lanes_enabled=True)
        assert pipeline.lane_controller is not None
        assert orch.lane_controller is pipeline.lane_controller

    def test_streaming_lane_controller_none_when_disabled(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers=_make_triggers(2), pool_ceiling=2)
        orch = _build_orch(pipeline, tmp_path, lanes_enabled=False)
        assert orch.lane_controller is None
        assert pipeline.lane_controller is None


class TestStreaming067LiveTUIBindings:
    def test_chunk_state_status_is_upload_during_run(self, tmp_path: Path) -> None:
        # 067: streaming must set status="UPLOAD" with upload_started_monotonic
        # the moment threads spawn — otherwise the UPLOAD-tab chunk
        # timer stays at 0 forever.
        triggers = _make_triggers(20)
        pipeline = _FakePipeline(triggers=triggers, pool_ceiling=2, prep_sleep_s=0.01)
        orch = _build_orch(pipeline, tmp_path, bucket_size=2, prep_workers=2)

        observed_statuses: list[str] = []
        observed_stamps: list[float | None] = []

        def _poll() -> None:
            for _ in range(60):
                snap_list = orch.chunks_snapshot()
                if snap_list:
                    observed_statuses.append(snap_list[0].status)
                    observed_stamps.append(snap_list[0].upload_started_monotonic)
                time.sleep(0.005)

        t = threading.Thread(target=_poll)
        t.start()
        orch.run(source_descriptor="", batch_size=100, batches_in_flight=2)
        t.join()

        assert "UPLOAD" in observed_statuses
        # The monotonic stamp must be a float (not None) once status flipped.
        upload_stamps = [s for s in observed_stamps if isinstance(s, float)]
        assert upload_stamps, "upload_started_monotonic was never set during run"

    def test_pool_stats_queue_depth_published_during_run(self, tmp_path: Path) -> None:
        # 067: ``pool_stats.queue_depth`` must reflect live pending so
        # the UPLOAD-tab progress bar shows real progress instead of
        # ``count/count``.
        triggers = _make_triggers(30)
        pipeline = _FakePipeline(triggers=triggers, pool_ceiling=2, upload_sleep_s=0.01)
        orch = _build_orch(pipeline, tmp_path, bucket_size=4, prep_workers=4)

        observed_depths: list[int] = []

        def _poll() -> None:
            for _ in range(80):
                observed_depths.append(pipeline.pool_stats.snapshot().queue_depth)
                time.sleep(0.005)

        t = threading.Thread(target=_poll)
        t.start()
        orch.run(source_descriptor="", batch_size=100, batches_in_flight=2)
        t.join()

        # At least once during the run we must have observed pending > 0.
        assert max(observed_depths) > 0, (
            "pool_stats.queue_depth never went above 0 — UPLOAD bar would show count/count"
        )

    def test_chunk_state_s5_done_grows_during_run(self, tmp_path: Path) -> None:
        # 067: the synthetic chunk_state's s5_done must grow during the
        # run so the CHUNKS tab shows live progress.
        triggers = _make_triggers(15)
        pipeline = _FakePipeline(triggers=triggers, pool_ceiling=2, upload_sleep_s=0.005)
        orch = _build_orch(pipeline, tmp_path, bucket_size=4, prep_workers=2)

        observed_s5_done: list[int] = []

        def _poll() -> None:
            for _ in range(80):
                snap_list = orch.chunks_snapshot()
                if snap_list:
                    observed_s5_done.append(snap_list[0].s5_done)
                time.sleep(0.005)

        t = threading.Thread(target=_poll)
        t.start()
        orch.run(source_descriptor="", batch_size=100, batches_in_flight=2)
        t.join()

        # s5_done must have reached a mid-run value > 0 (not just final).
        observed_mid = [v for v in observed_s5_done if 0 < v < 15]
        assert observed_mid, (
            "chunk_state.s5_done never grew mid-run — CHUNKS tab would show 0 forever"
        )

    def test_lane_queue_depth_never_exceeds_bucket_size(self, tmp_path: Path) -> None:
        # 067: dispatcher and consumer report lane_queue.qsize() now, not
        # a monotonic counter — depth must never exceed bucket_size.
        triggers = _make_triggers(50)
        sizes = {i: (20 * 1024 * 1024 if i % 2 == 0 else 1 * 1024 * 1024) for i in range(50)}
        pipeline = _FakePipeline(
            triggers=triggers,
            pool_ceiling=4,
            size_bytes_by_idx=sizes,
            upload_sleep_s=0.002,
        )
        orch = _build_orch(
            pipeline,
            tmp_path,
            bucket_size=4,
            prep_workers=2,
            lanes_enabled=True,
            heavy_threshold_bytes=10 * 1024 * 1024,
        )

        observed_heavy: list[int] = []
        observed_light: list[int] = []

        def _poll() -> None:
            for _ in range(120):
                if orch.lane_controller is None:
                    return
                snap = orch.lane_controller.snapshot()
                observed_heavy.append(snap.heavy.queue_depth)
                observed_light.append(snap.light.queue_depth)
                time.sleep(0.003)

        t = threading.Thread(target=_poll)
        t.start()
        orch.run(source_descriptor="", batch_size=100, batches_in_flight=2)
        t.join()

        # The dispatcher routes 25 heavy + 25 light items. Pre-067 the
        # depth would reach 25 (monotonic). Post-067 it caps at the
        # lane queue's maxsize = bucket_size = 4.
        assert max(observed_heavy) <= 4
        assert max(observed_light) <= 4
        # Sanity: it actually got SOME items routed (test isn't trivially passing).
        assert max(observed_heavy + observed_light) >= 1


# ---------------------------------------------------------------------------
# 144 — cierre de corrida visible + cola sin píldoras
# ---------------------------------------------------------------------------


class _FakePeriodicReconciler:
    """Doble del PeriodicReconciler: ``stop`` emite progreso y registra
    qué veía ``pool_stats.closing`` en cada momento."""

    def __init__(self, pool_stats) -> None:  # noqa: ANN001
        self._pool_stats = pool_stats
        self.started = False
        self.seen: list[tuple[str, int, int] | None] = []

    def start(self) -> None:
        self.started = True

    def _peek(self) -> None:
        c = self._pool_stats.snapshot().closing
        self.seen.append((c.label, c.done, c.total) if c is not None else None)

    def stop(self, *, on_progress=None):  # noqa: ANN001, ANN202
        from cmcourier.services.recovery import SyncProgress

        self._peek()
        assert on_progress is not None
        on_progress(SyncProgress("sincronizando AS400", 50, 120))
        self._peek()
        return


class TestRunClose144:
    def test_final_pass_is_published_as_closing_phase(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers=_make_triggers(3))
        recon = _FakePeriodicReconciler(pipeline.pool_stats)
        pipeline._periodic_reconciler = recon
        orch = _build_orch(pipeline, tmp_path)

        orch.run(source_descriptor="", batch_size=100, batches_in_flight=2)

        assert recon.started
        assert recon.seen == [("sincronizando AS400", 0, 0), ("sincronizando AS400", 50, 120)]
        assert pipeline.pool_stats.snapshot().closing is None

    @pytest.mark.parametrize("lanes_enabled", [False, True])
    def test_queue_depth_is_zero_after_run_pills_excluded(
        self, tmp_path: Path, lanes_enabled: bool
    ) -> None:
        # Pre-144 las N `_POISON` quedaban contadas por qsize() en la última
        # publicación → el monitor mostraba `30 pending · idle 30` para siempre.
        pipeline = _FakePipeline(triggers=_make_triggers(6), pool_ceiling=2, upload_sleep_s=0.02)
        orch = _build_orch(pipeline, tmp_path, bucket_size=8, lanes_enabled=lanes_enabled)

        orch.run(source_descriptor="", batch_size=100, batches_in_flight=2)

        assert len(pipeline.upload_calls) == 6
        assert pipeline.pool_stats.snapshot().queue_depth == 0

    def test_published_depth_never_counts_pills_mid_run(self, tmp_path: Path) -> None:
        # Con 2 consumers dormidos y 1 doc en cola, las 2 píldoras ya
        # encoladas no pueden inflar el pending por encima de 1.
        pipeline = _FakePipeline(triggers=_make_triggers(4), pool_ceiling=2, upload_sleep_s=0.03)
        orch = _build_orch(pipeline, tmp_path, bucket_size=8)
        observed: list[int] = []

        def _poll() -> None:
            for _ in range(60):
                observed.append(pipeline.pool_stats.snapshot().queue_depth)
                time.sleep(0.003)

        t = threading.Thread(target=_poll)
        t.start()
        orch.run(source_descriptor="", batch_size=100, batches_in_flight=2)
        t.join()

        # 4 docs, 2 consumers: nunca hay más de 4 pendientes reales; con
        # píldoras contadas se vería hasta 6.
        assert max(observed) <= 4
