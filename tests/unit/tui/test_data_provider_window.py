"""Tasa por ventana deslizante + ETA de corrida (134).

Reloj inyectado: nadie duerme. Las muestras se toman en ``snapshot()``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cmcourier.config.schema import CmisConfigModel
from cmcourier.observability.metrics import MetricsRecorder
from cmcourier.orchestrators.multi_batch import ChunkState
from cmcourier.services.worker_pool_stats import ResizableSemaphore, WorkerPoolStats
from cmcourier.tui.data_provider import TUIDataProvider

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _provider(
    tmp_path: Path,
    *,
    planned_total: int | None = None,
    chunks: list[ChunkState] | None = None,
) -> tuple[TUIDataProvider, _Clock, WorkerPoolStats]:
    recorder = MetricsRecorder(
        log_dir=tmp_path / "logs",
        slow_op_threshold_ms=0.0,
        slow_op_top_n=10,
        enabled=True,
        pipeline_metrics_enabled=True,
    )
    pool = WorkerPoolStats()
    uploader = MagicMock()
    uploader._timeout_s = 300.0
    clock = _Clock()
    provider = TUIDataProvider(
        pipeline_name="csv-trigger",
        metrics_recorder=recorder,
        pool_stats=pool,
        concurrency_limit=ResizableSemaphore(4),
        cmis_config=CmisConfigModel(base_url="http://cm.test/cmis", repo_id="r", workers=4),
        uploader=uploader,
        chunks_provider=(lambda: list(chunks)) if chunks is not None else None,
        planned_total=planned_total,
        clock=clock,
    )
    provider.mark_batch_started("b-1")
    return provider, clock, pool


def _complete(pool: WorkerPoolStats, n: int) -> None:
    for _ in range(n):
        pool.mark_completed()


class TestWindowRate:
    def test_single_sample_has_no_window_rate(self, tmp_path: Path) -> None:
        provider, _clock, _pool = _provider(tmp_path)
        snap = provider.snapshot()
        assert snap.throughput_window_docs_per_s is None
        assert snap.eta_run_s is None

    def test_samples_too_close_still_none(self, tmp_path: Path) -> None:
        provider, clock, pool = _provider(tmp_path)
        provider.snapshot()
        clock.now += 0.2
        _complete(pool, 3)
        assert provider.snapshot().throughput_window_docs_per_s is None

    def test_rate_uses_only_the_last_window(self, tmp_path: Path) -> None:
        """E1: t=0 (0), t=10 (20), t=70 (80) → ventana 60 s = 60/60 = 1.0."""
        provider, clock, pool = _provider(tmp_path)
        provider.snapshot()
        clock.now += 10
        _complete(pool, 20)
        provider.snapshot()
        clock.now += 60
        _complete(pool, 60)
        snap = provider.snapshot()
        assert snap.throughput_window_docs_per_s == pytest.approx(1.0)
        assert snap.throughput_docs_per_s == pytest.approx(80 / 70)

    def test_rate_decays_to_zero_after_a_full_window(self, tmp_path: Path) -> None:
        """Pausa: lo hecho hace < 60 s sigue contando; pasada la ventana, 0."""
        provider, clock, pool = _provider(tmp_path)
        provider.snapshot()
        clock.now += 5
        _complete(pool, 10)
        provider.snapshot()
        clock.now += 30
        assert provider.snapshot().throughput_window_docs_per_s == pytest.approx(10 / 35)
        clock.now += 40
        assert provider.snapshot().throughput_window_docs_per_s == pytest.approx(0.0)

    def test_processed_comes_from_chunks_when_present(self, tmp_path: Path) -> None:
        chunks = [
            ChunkState(chunk_idx=0, batch_id="b-0", status="DONE", s5_done=30, s5_failed=2),
            ChunkState(chunk_idx=1, batch_id="b-1", status="PREP", prep_filtered=3),
        ]
        provider, clock, _pool = _provider(tmp_path, chunks=chunks)
        provider.snapshot()
        clock.now += 10
        chunks[1] = ChunkState(
            chunk_idx=1, batch_id="b-1", status="DONE", s5_done=15, prep_filtered=5
        )
        snap = provider.snapshot()
        # de 35 a 52 procesados en 10 s
        assert snap.docs_processed == 52
        assert snap.throughput_window_docs_per_s == pytest.approx(1.7)


class TestEta:
    def test_eta_from_planned_total(self, tmp_path: Path) -> None:
        provider, clock, pool = _provider(tmp_path, planned_total=200)
        provider.snapshot()
        clock.now += 80
        _complete(pool, 80)
        snap = provider.snapshot()
        assert snap.planned_total == 200
        assert snap.eta_run_s == pytest.approx(120.0)

    def test_no_planned_total_no_eta(self, tmp_path: Path) -> None:
        provider, clock, pool = _provider(tmp_path)
        provider.snapshot()
        clock.now += 10
        _complete(pool, 10)
        snap = provider.snapshot()
        assert snap.planned_total is None
        assert snap.eta_run_s is None

    def test_done_or_beyond_total_no_eta(self, tmp_path: Path) -> None:
        provider, clock, pool = _provider(tmp_path, planned_total=10)
        provider.snapshot()
        clock.now += 10
        _complete(pool, 12)
        assert provider.snapshot().eta_run_s is None

    def test_zero_rate_no_eta(self, tmp_path: Path) -> None:
        provider, clock, _pool = _provider(tmp_path, planned_total=10)
        provider.snapshot()
        clock.now += 10
        assert provider.snapshot().eta_run_s is None
