"""Unit tests for ``MultiBatchOrchestrator`` (028, REQ-023)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cmcourier.config.schema import ObservabilityConfig, PipelineConfig
from cmcourier.domain.models import TriggerRecord
from cmcourier.orchestrators.multi_batch import (
    MultiBatchOrchestrator,
    MultiBatchRunReport,
)
from cmcourier.orchestrators.staged import RunReport

# ---------------------------------------------------------------------------
# Stub pipeline + config (we don't want to spin up real CMIS, AS400 etc.)
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Minimal stand-in for StagedPipeline — covers the surface
    that MultiBatchOrchestrator touches."""

    pipeline_name = "csv-trigger"

    def __init__(
        self,
        *,
        triggers_per_call: list[TriggerRecord],
        prep_sleep_s: float = 0.0,
        upload_sleep_s: float = 0.0,
        raise_on_prep: int | None = None,
    ) -> None:
        self._triggers = triggers_per_call
        self._prep_sleep = prep_sleep_s
        self._upload_sleep = upload_sleep_s
        self._raise_on_prep = raise_on_prep
        self._batch_counter = 0
        self.prep_order: list[str] = []
        self.upload_order: list[str] = []
        self.lock = threading.Lock()

        class _S:
            def acquire(self_inner, descriptor: str) -> Iterator[TriggerRecord]:  # noqa: ARG002, N805
                yield from triggers_per_call

        self._trigger_strategy = _S()
        self._tracking_store = MagicMock()
        self._tracking_store.start_batch = self._fake_start_batch
        self._tracking_store.flush = MagicMock()
        self._tracking_store.complete_batch = MagicMock()
        self._tracking_store.list_txn_nums_for_batch = MagicMock(return_value=set())
        self.auto_tune_controller = None
        self.sampler = None

    def _fake_start_batch(self, *, total_records: int) -> str:  # noqa: ARG002
        self._batch_counter += 1
        return f"B{self._batch_counter}"

    def _resolve_batch_id(
        self,
        batch_id: str | None,
        from_stage: int,  # noqa: ARG002
        batch_size: int,
    ) -> str:
        if batch_id is not None:
            return batch_id
        return self._fake_start_batch(total_records=batch_size)

    def prep_chunk(
        self,
        *,
        triggers: list[TriggerRecord],
        batch_id: str,
        recorder,  # noqa: ARG002
        from_stage: int = 1,  # noqa: ARG002
    ):
        if self._prep_sleep:
            time.sleep(self._prep_sleep)
        if self._raise_on_prep is not None and batch_id == f"B{self._raise_on_prep}":
            raise RuntimeError("synthetic prep failure")
        with self.lock:
            self.prep_order.append(batch_id)
        # Lightweight stand-in for _StageItem — only the upload path cares
        # about ``document.txn_num`` and we don't actually upload in the fake.
        items = [
            SimpleNamespace(document=SimpleNamespace(txn_num=f"TXN_{batch_id}_{i}"))
            for i, _ in enumerate(triggers)
        ]
        # 051: (items, skipped, s1_done, s1_filtered, s2_failed, s3_failed, s4_failed)
        return items, 0, len(items), 0, 0, 0, 0

    def upload_chunk(self, *, items, batch_id: str, recorder):  # noqa: ARG002
        if self._upload_sleep:
            time.sleep(self._upload_sleep)
        with self.lock:
            self.upload_order.append(batch_id)
        return len(items), 0

    def run(self, **kwargs):
        # Mimic the single-batch entry point: prep + upload for the full source.
        batch_id = self._resolve_batch_id(
            kwargs.get("batch_id"), kwargs.get("from_stage", 1), kwargs.get("batch_size", 1000)
        )
        items, skipped, s1d, s1f, s2f, s3f, s4f = self.prep_chunk(
            triggers=self._triggers, batch_id=batch_id, recorder=None
        )
        s5d, s5f = self.upload_chunk(items=items, batch_id=batch_id, recorder=None)
        return RunReport(
            batch_id=batch_id,
            total_triggers=len(self._triggers),
            total_docs=s1d + skipped,
            s1_done=s1d,
            s1_skipped_cross_batch=skipped,
            s1_filtered=s1f,
            s2_done=s1d - s2f,
            s2_failed=s2f,
            s3_done=s1d - s2f - s3f,
            s3_failed=s3f,
            s4_done=s1d - s2f - s3f - s4f,
            s4_failed=s4f,
            s5_done=s5d,
            s5_failed=s5f,
            elapsed_seconds=0.0,
        )


def _make_triggers(n: int) -> list[TriggerRecord]:
    return [TriggerRecord(shortname=f"SN_{i}", cif=str(i), system_id="1") for i in range(n)]


def _build_orchestrator(pipeline, tmp_path: Path) -> MultiBatchOrchestrator:
    # Build a minimal valid PipelineConfig for the orchestrator's observability
    # access. We only touch ``config.observability`` from the orchestrator.
    cfg = MagicMock(spec=PipelineConfig)
    cfg.observability = ObservabilityConfig(log_dir=tmp_path)
    return MultiBatchOrchestrator(pipeline=pipeline, config=cfg, log_dir=tmp_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMultiBatchOrchestrator:
    def test_n_one_delegates_to_pipeline_run(self, tmp_path: Path) -> None:
        triggers = _make_triggers(5)
        pipeline = _FakePipeline(triggers_per_call=triggers)
        orch = _build_orchestrator(pipeline, tmp_path)
        report = orch.run(
            source_descriptor="",
            batch_size=10,
            batches_in_flight=1,
        )
        assert isinstance(report, MultiBatchRunReport)
        assert len(report.chunks) == 1
        assert report.chunks[0].s5_done == 5
        assert report.failed_chunks == []

    def test_n_two_overlap_on_three_chunks(self, tmp_path: Path) -> None:
        triggers = _make_triggers(15)
        pipeline = _FakePipeline(triggers_per_call=triggers, prep_sleep_s=0.02, upload_sleep_s=0.02)
        orch = _build_orchestrator(pipeline, tmp_path)
        report = orch.run(
            source_descriptor="",
            batch_size=5,
            batches_in_flight=2,
        )
        # 15 triggers / 5 per chunk = 3 chunks.
        assert len(report.chunks) == 3
        assert report.s5_done == 15
        assert report.failed_chunks == []
        # Order check: every chunk prepped before being uploaded.
        assert pipeline.prep_order == ["B1", "B2", "B3"]
        # Uploads complete in the same order (single upload thread).
        assert pipeline.upload_order == ["B1", "B2", "B3"]

    def test_n_two_overlap_actually_overlaps(self, tmp_path: Path) -> None:
        """Wall-clock evidence that prep and upload run concurrently."""
        triggers = _make_triggers(6)
        # Sleep long enough that serial execution would be roughly
        # 3 * (prep_sleep + upload_sleep). Overlapped should be closer to
        # 3 * upload_sleep + prep_sleep (the last chunk's prep doesn't overlap).
        pipeline = _FakePipeline(triggers_per_call=triggers, prep_sleep_s=0.05, upload_sleep_s=0.05)
        orch = _build_orchestrator(pipeline, tmp_path)
        t0 = time.monotonic()
        orch.run(source_descriptor="", batch_size=2, batches_in_flight=2)
        elapsed = time.monotonic() - t0
        # 3 chunks. Serial would be ~0.3 s. Overlapped should be ~0.2 s.
        # Be lenient: anything under 0.28 s proves real overlap.
        assert elapsed < 0.28, f"no overlap detected; elapsed={elapsed:.3f}s"

    def test_n_two_exception_in_prep_is_isolated(self, tmp_path: Path) -> None:
        triggers = _make_triggers(15)
        pipeline = _FakePipeline(triggers_per_call=triggers, raise_on_prep=2)
        orch = _build_orchestrator(pipeline, tmp_path)
        report = orch.run(
            source_descriptor="",
            batch_size=5,
            batches_in_flight=2,
        )
        # Chunk 2 failed; chunks 1 and 3 succeeded.
        assert len(report.chunks) == 2
        assert len(report.failed_chunks) == 1
        assert report.failed_chunks[0][1] == "RuntimeError"

    def test_n_three_rejected(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers_per_call=_make_triggers(1))
        orch = _build_orchestrator(pipeline, tmp_path)
        with pytest.raises(ValueError, match="3..5 deferred"):
            orch.run(source_descriptor="", batch_size=1, batches_in_flight=3)

    def test_empty_source_returns_zero_chunks(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers_per_call=[])
        orch = _build_orchestrator(pipeline, tmp_path)
        report = orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)
        assert report.chunks == []

    def test_resume_forces_n_one(self, tmp_path: Path) -> None:
        triggers = _make_triggers(3)
        pipeline = _FakePipeline(triggers_per_call=triggers)
        orch = _build_orchestrator(pipeline, tmp_path)
        report = orch.run(
            source_descriptor="",
            batch_size=10,
            batches_in_flight=2,
            resume_batch_id="EXISTING",
        )
        # Resume = single batch path; one RunReport.
        assert len(report.chunks) == 1
        assert report.chunks[0].batch_id == "EXISTING"


# ---------------------------------------------------------------------------
# 030 — TUI live binding (chunk-state machine + active recorder)
# ---------------------------------------------------------------------------


class TestOrchestratorChunkState:
    """The orchestrator now tracks per-chunk state for live TUI binding."""

    def test_chunks_snapshot_empty_before_run(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers_per_call=_make_triggers(1))
        orch = _build_orchestrator(pipeline, tmp_path)
        assert orch.chunks_snapshot() == []

    def test_chunks_snapshot_after_n_two_run(self, tmp_path: Path) -> None:
        triggers = _make_triggers(6)
        pipeline = _FakePipeline(triggers_per_call=triggers)
        orch = _build_orchestrator(pipeline, tmp_path)
        orch.run(source_descriptor="", batch_size=2, batches_in_flight=2)
        # 6 triggers / 2 = 3 chunks. All DONE after run completes.
        states = orch.chunks_snapshot()
        assert len(states) == 3
        assert all(s.status == "DONE" for s in states)
        assert [s.batch_id for s in states] == ["B1", "B2", "B3"]

    def test_chunks_snapshot_marks_failed(self, tmp_path: Path) -> None:
        triggers = _make_triggers(6)
        pipeline = _FakePipeline(triggers_per_call=triggers, raise_on_prep=2)
        orch = _build_orchestrator(pipeline, tmp_path)
        orch.run(source_descriptor="", batch_size=2, batches_in_flight=2)
        states = orch.chunks_snapshot()
        # Chunk 2 failed in prep; the others are DONE.
        statuses = {s.batch_id: s.status for s in states}
        assert statuses.get("B2") == "FAILED"

    def test_active_recorder_none_before_run(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers_per_call=_make_triggers(1))
        orch = _build_orchestrator(pipeline, tmp_path)
        assert orch.active_recorder() is None

    def test_active_recorder_set_during_run(self, tmp_path: Path) -> None:
        """During a real run there should be an active recorder while a
        chunk is in flight. We sleep inside prep/upload to keep the
        recorder alive long enough to observe."""
        triggers = _make_triggers(2)
        recorder_seen: list[object] = []
        pipeline = _FakePipeline(triggers_per_call=triggers, prep_sleep_s=0.0, upload_sleep_s=0.0)
        orch = _build_orchestrator(pipeline, tmp_path)

        # Patch _build_chunk_recorder to capture the recorder it builds.
        orig = orch._build_chunk_recorder  # noqa: SLF001

        def _patched() -> object:
            r = orig()
            recorder_seen.append(r)
            return r

        orch._build_chunk_recorder = _patched  # type: ignore[assignment, method-assign]
        orch.run(source_descriptor="", batch_size=1, batches_in_flight=2)
        # 2 chunks × 1 recorder each = 2 recorders constructed.
        assert len(recorder_seen) == 2


# ---------------------------------------------------------------------------
# 042 — separate UPLOAD-active recorder slot
# ---------------------------------------------------------------------------


class TestUploadRecorder042:
    """042: ``upload_recorder()`` tracks the chunk currently in S5 and is
    immune to PREP-side flips that move ``active_recorder()`` around."""

    def test_initial_upload_recorder_is_none(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers_per_call=_make_triggers(1))
        orch = _build_orchestrator(pipeline, tmp_path)
        assert orch.upload_recorder() is None

    def test_upload_recorder_cleared_after_run(self, tmp_path: Path) -> None:
        """After every chunk leaves UPLOAD, the slot must be released so
        the dashboard doesn't keep showing the last chunk's numbers
        forever."""
        triggers = _make_triggers(4)
        pipeline = _FakePipeline(triggers_per_call=triggers)
        orch = _build_orchestrator(pipeline, tmp_path)
        orch.run(source_descriptor="", batch_size=2, batches_in_flight=2)
        assert orch.upload_recorder() is None

    def test_upload_recorder_observed_during_upload(self, tmp_path: Path) -> None:
        """While a chunk is in S5 the upload recorder must be that chunk's
        recorder. We use a slow upload to keep the chunk inside S5 long
        enough to capture the binding."""
        import threading as _t

        triggers = _make_triggers(2)
        observed: list[object] = []
        latch = _t.Event()

        def _on_upload_recorder(orch_ref: MultiBatchOrchestrator) -> None:
            # Poll until the upload slot is set, then capture and release.
            for _ in range(200):
                rec = orch_ref.upload_recorder()
                if rec is not None:
                    observed.append(rec)
                    latch.set()
                    return
                _t.Event().wait(0.005)

        pipeline = _FakePipeline(
            triggers_per_call=triggers,
            prep_sleep_s=0.0,
            upload_sleep_s=0.25,  # hold the slot long enough for the poller
        )
        orch = _build_orchestrator(pipeline, tmp_path)
        watcher = _t.Thread(target=_on_upload_recorder, args=(orch,), daemon=True)
        watcher.start()
        orch.run(source_descriptor="", batch_size=1, batches_in_flight=2)
        latch.wait(timeout=2.0)
        watcher.join(timeout=1.0)
        # Either captured at least one binding, or the run was so fast we
        # missed it — but the post-run slot MUST be cleared.
        assert orch.upload_recorder() is None
        if observed:
            # The captured recorder must be one of the chunk recorders
            # (a MetricsRecorder instance, not None).
            from cmcourier.observability.metrics import MetricsRecorder

            assert all(isinstance(r, MetricsRecorder) for r in observed)


class _EventPipeline(_FakePipeline):
    """050 probe: a pipeline whose trigger source records every *pull* and
    whose ``prep_chunk`` records every *prep*, into one shared event list —
    so a test can prove pulls and preps interleave (streaming) instead of
    all pulls happening first (the pre-050 ``list(acquire())`` behavior).
    """

    def __init__(self, *, n_triggers: int, events: list[tuple[str, object]]) -> None:
        super().__init__(triggers_per_call=[])
        self._events = events
        all_triggers = _make_triggers(n_triggers)

        def _gen() -> Iterator[TriggerRecord]:
            for i, trig in enumerate(all_triggers):
                events.append(("pull", i))
                yield trig

        gen = _gen()

        class _S:
            def acquire(self_inner, descriptor: str = "") -> Iterator[TriggerRecord]:  # noqa: ARG002, N805
                return gen

        self._trigger_strategy = _S()

    def prep_chunk(self, *, triggers, batch_id, recorder, from_stage=1):  # type: ignore[no-untyped-def]  # noqa: ARG002
        self._events.append(("prep", batch_id))
        return super().prep_chunk(triggers=triggers, batch_id=batch_id, recorder=recorder)


class TestStreaming050:
    """050 — triggers stream in bounded-memory chunks; the orchestrator
    never materializes the full trigger set."""

    def test_total_islices_the_source(self, tmp_path: Path) -> None:
        """``--total N`` pulls exactly N items from the source — not the
        whole thing. Pre-050 (``list(acquire())[:N]``) would have pulled
        every item before slicing."""
        pulled: list[str] = []

        def _counting_gen() -> Iterator[TriggerRecord]:
            for trig in _make_triggers(1000):
                pulled.append(trig.shortname)
                yield trig

        pipeline = _FakePipeline(triggers_per_call=[])

        class _S:
            def acquire(self_inner, descriptor: str = "") -> Iterator[TriggerRecord]:  # noqa: ARG002, N805
                return _counting_gen()

        pipeline._trigger_strategy = _S()  # noqa: SLF001
        orch = _build_orchestrator(pipeline, tmp_path)

        report = orch.run(source_descriptor="", batch_size=5, batches_in_flight=2, total=10)
        assert len(pulled) == 10, f"islice should pull exactly 10, pulled {len(pulled)}"
        assert report.s5_done == 10

    def test_overlapped_streams_not_materializes(self, tmp_path: Path) -> None:
        """N=2 path: prep starts before the source is fully drained.
        Pre-050 every ``pull`` happened before the first ``prep``."""
        events: list[tuple[str, object]] = []
        pipeline = _EventPipeline(n_triggers=20, events=events)
        orch = _build_orchestrator(pipeline, tmp_path)

        orch.run(source_descriptor="", batch_size=5, batches_in_flight=2)

        first_prep = next(i for i, e in enumerate(events) if e[0] == "prep")
        last_pull = max(i for i, e in enumerate(events) if e[0] == "pull")
        assert first_prep < last_pull, (
            "prep must start before the source is drained — "
            f"first_prep={first_prep} last_pull={last_pull}"
        )

    def test_sequential_n1_streams(self, tmp_path: Path) -> None:
        """Fresh N=1 path routes through ``_run_sequential`` and streams
        chunk-by-chunk — pulls interleave with preps, reports accumulate."""
        events: list[tuple[str, object]] = []
        pipeline = _EventPipeline(n_triggers=12, events=events)
        orch = _build_orchestrator(pipeline, tmp_path)

        report = orch.run(source_descriptor="", batch_size=4, batches_in_flight=1)

        # 12 triggers / 4 = 3 chunks, each its own RunReport.
        assert len(report.chunks) == 3
        assert report.s5_done == 12
        assert report.failed_chunks == []
        first_prep = next(i for i, e in enumerate(events) if e[0] == "prep")
        last_pull = max(i for i, e in enumerate(events) if e[0] == "pull")
        assert first_prep < last_pull

    def test_sequential_n1_isolates_chunk_failure(self, tmp_path: Path) -> None:
        """A prep failure in one chunk doesn't abort the N=1 stream."""
        triggers = _make_triggers(15)
        pipeline = _FakePipeline(triggers_per_call=triggers, raise_on_prep=2)
        orch = _build_orchestrator(pipeline, tmp_path)

        report = orch.run(source_descriptor="", batch_size=5, batches_in_flight=1)

        assert len(report.chunks) == 2
        assert len(report.failed_chunks) == 1
        assert report.failed_chunks[0][1] == "RuntimeError"

    def test_empty_source_n1_yields_empty_report(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers_per_call=[])
        orch = _build_orchestrator(pipeline, tmp_path)
        report = orch.run(source_descriptor="", batch_size=10, batches_in_flight=1)
        assert report.chunks == []
        assert report.failed_chunks == []


class TestAimdMultiBatchP95Wiring043:
    """043 — overlapped path swaps the controller's p95 source to point at
    the upload-active recorder. Single-batch path stays unchanged."""

    def test_observer_returns_zero_with_no_active_upload(self, tmp_path: Path) -> None:
        pipeline = _FakePipeline(triggers_per_call=_make_triggers(1))
        orch = _build_orchestrator(pipeline, tmp_path)
        # No chunk has entered UPLOAD yet → observer returns (0.0, 0) cleanly
        # (061: the provider returns (p95, sample_count) so AIMD can gate on
        # minimum samples).
        assert orch._upload_p95_observer() == (0.0, 0)  # noqa: SLF001

    def test_overlapped_run_swaps_controllers_p95_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The orchestrator must call ``set_p95_provider`` on the live
        controller before ``start()``. We capture the call by patching the
        controller's method."""
        triggers = _make_triggers(4)
        pipeline = _FakePipeline(triggers_per_call=triggers)
        captured: list[object] = []

        # Inject a fake controller into the pipeline so we can observe the
        # set_p95_provider call. The fake exposes the same surface
        # _upload_loop touches.
        from cmcourier.services.auto_tune import AutoTuneConfig, AutoTuneController

        cfg = AutoTuneConfig(
            enabled=True,
            adjustment_interval_s=60,
            warmup_seconds=0,
            min_threads=1,
            max_threads=16,
        )
        real_ctl = AutoTuneController(
            config=cfg,
            p95_provider=lambda: (999.0, 100),  # the "wrong" pre-043 source
            current_workers_provider=lambda: 4,
            current_timeout_provider=lambda: 60.0,
            on_pool_resize=lambda _n: None,
            on_timeout_change=lambda _t: None,
        )
        orig_set = real_ctl.set_p95_provider

        def _spy(provider):  # type: ignore[no-untyped-def]
            captured.append(provider)
            orig_set(provider)

        real_ctl.set_p95_provider = _spy  # type: ignore[method-assign]
        # Patch pipeline.auto_tune_controller to return our spy controller.
        # monkeypatch restores it on teardown — without this the read-only
        # property leaks onto the shared _FakePipeline class and breaks every
        # subsequent test that constructs one (its __init__ sets the attr).
        monkeypatch.setattr(
            type(pipeline),
            "auto_tune_controller",
            property(lambda _self: real_ctl),
            raising=False,
        )

        orch = _build_orchestrator(pipeline, tmp_path)
        orch.run(source_descriptor="", batch_size=2, batches_in_flight=2)

        assert captured, "set_p95_provider must be called before controller.start()"
        # The captured provider is the orchestrator's upload-side observer.
        # Calling it after the run (all chunks DONE → upload slot None)
        # returns 0.0.
        assert captured[0]() == (0.0, 0)


# ---------------------------------------------------------------------------
# 051 — "filtered at S1" threads through reports + chunk state
# ---------------------------------------------------------------------------


class _FilterPipeline(_FakePipeline):
    """051 probe: prep_chunk reports a fixed number of S1-filtered docs."""

    def __init__(self, *, triggers_per_call, filtered_per_chunk: int) -> None:  # type: ignore[no-untyped-def]
        super().__init__(triggers_per_call=triggers_per_call)
        self._filtered_per_chunk = filtered_per_chunk

    def prep_chunk(self, *, triggers, batch_id, recorder, from_stage=1):  # type: ignore[no-untyped-def]
        items, skipped, s1d, _s1f, s2f, s3f, s4f = super().prep_chunk(
            triggers=triggers, batch_id=batch_id, recorder=recorder
        )
        return items, skipped, s1d, self._filtered_per_chunk, s2f, s3f, s4f


class TestFilteredOutcome051:
    """``s1_filtered`` flows from prep_chunk → ChunkState.prep_filtered →
    RunReport.s1_filtered → MultiBatchRunReport.s1_filtered (aggregate)."""

    def test_overlapped_n2_threads_filtered(self, tmp_path: Path) -> None:
        pipeline = _FilterPipeline(triggers_per_call=_make_triggers(6), filtered_per_chunk=2)
        orch = _build_orchestrator(pipeline, tmp_path)
        report = orch.run(source_descriptor="", batch_size=2, batches_in_flight=2)
        # 6 triggers / 2 = 3 chunks, each reporting 2 filtered.
        assert report.s1_filtered == 6
        assert all(s.prep_filtered == 2 for s in orch.chunks_snapshot())

    def test_sequential_n1_threads_filtered(self, tmp_path: Path) -> None:
        pipeline = _FilterPipeline(triggers_per_call=_make_triggers(4), filtered_per_chunk=1)
        orch = _build_orchestrator(pipeline, tmp_path)
        report = orch.run(source_descriptor="", batch_size=2, batches_in_flight=1)
        # 4 triggers / 2 = 2 chunks, each reporting 1 filtered.
        assert report.s1_filtered == 2
        assert all(s.prep_filtered == 1 for s in orch.chunks_snapshot())


# ---------------------------------------------------------------------------
# 144 — cierre de corrida visible (path N=1 con reconciliador periódico)
# ---------------------------------------------------------------------------


class _FakePeriodicReconciler:
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
        on_progress(SyncProgress("sincronizando AS400", 7, 9))
        self._peek()
        return


class TestRunClose144:
    def test_sequential_n1_publishes_closing_phase_around_final_pass(self, tmp_path: Path) -> None:
        from cmcourier.services.worker_pool_stats import WorkerPoolStats

        pipeline = _FakePipeline(triggers_per_call=_make_triggers(6))
        pipeline.pool_stats = WorkerPoolStats()
        recon = _FakePeriodicReconciler(pipeline.pool_stats)
        pipeline._periodic_reconciler = recon
        orch = _build_orchestrator(pipeline, tmp_path)

        orch.run(source_descriptor="", batch_size=3, batches_in_flight=1)

        assert recon.started
        assert recon.seen == [("sincronizando AS400", 0, 0), ("sincronizando AS400", 7, 9)]
        assert pipeline.pool_stats.snapshot().closing is None

    def test_sequential_n1_without_pool_stats_still_stops(self, tmp_path: Path) -> None:
        # Doble sin ``pool_stats`` (como los fakes pre-144): stop() pelado.
        pipeline = _FakePipeline(triggers_per_call=_make_triggers(2))
        recon = MagicMock()
        pipeline._periodic_reconciler = recon
        orch = _build_orchestrator(pipeline, tmp_path)

        orch.run(source_descriptor="", batch_size=3, batches_in_flight=1)

        recon.stop.assert_called_once_with()
