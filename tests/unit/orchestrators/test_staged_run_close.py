"""144: el ``StagedPipeline`` (batched) publica la pasada final del
reconciliador como fase de cierre en ``pool_stats``."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cmcourier.orchestrators.staged import StagedPipeline
from cmcourier.services.recovery import SyncProgress
from cmcourier.services.worker_pool_stats import WorkerPoolStats

pytestmark = pytest.mark.unit


class _FakePeriodicReconciler:
    def __init__(self, pool_stats: WorkerPoolStats) -> None:
        self._pool_stats = pool_stats
        self.started = False
        self.seen: list[tuple[str, int, int] | None] = []

    def start(self) -> None:
        self.started = True

    def _peek(self) -> None:
        c = self._pool_stats.snapshot().closing
        self.seen.append((c.label, c.done, c.total) if c is not None else None)

    def stop(self, *, on_progress=None):  # noqa: ANN001, ANN202
        self._peek()
        assert on_progress is not None
        on_progress(SyncProgress("sincronizando AS400", 100, 250))
        self._peek()
        return


def _pipeline(recon: _FakePeriodicReconciler, stats: WorkerPoolStats) -> StagedPipeline:
    strategy = MagicMock()
    strategy.acquire.return_value = iter([])  # corrida vacía: sólo importa el cierre
    tracking = MagicMock()
    tracking.start_batch.return_value = "B1"
    return StagedPipeline(
        trigger_strategy=strategy,
        indexing_service=MagicMock(),
        mapping_service=MagicMock(),
        metadata_service=MagicMock(),
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=tracking,
        pool_stats=stats,
        periodic_reconciler=recon,  # type: ignore[arg-type]
    )


def test_batched_run_publishes_closing_phase_around_final_pass() -> None:
    stats = WorkerPoolStats()
    recon = _FakePeriodicReconciler(stats)
    pipeline = _pipeline(recon, stats)

    pipeline.run(source_descriptor="whatever")

    assert recon.started
    assert recon.seen == [("sincronizando AS400", 0, 0), ("sincronizando AS400", 100, 250)]
    assert stats.snapshot().closing is None
