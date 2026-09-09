"""Tests unitarios para :class:`WorkerPoolStats` (025)."""

from __future__ import annotations

import threading

import pytest

from cmcourier.services.worker_pool_stats import (
    ClosingPhase,
    WorkerPoolStats,
    WorkerPoolStatsSnapshot,
)

pytestmark = pytest.mark.unit


class TestClosingPhase144:
    """144: fase de cierre visible (pasada final del reconciliador)."""

    def test_closing_is_none_by_default(self) -> None:
        assert WorkerPoolStats().snapshot().closing is None

    def test_set_closing_is_visible_in_snapshot(self) -> None:
        stats = WorkerPoolStats()
        stats.set_closing("sincronizando AS400", 50, 3000)
        assert stats.snapshot().closing == ClosingPhase("sincronizando AS400", 50, 3000)

    def test_set_closing_overwrites_previous_phase(self) -> None:
        stats = WorkerPoolStats()
        stats.set_closing("sincronizando AS400", 0, 0)
        stats.set_closing("sincronizando AS400", 100, 3000)
        closing = stats.snapshot().closing
        assert closing is not None
        assert (closing.done, closing.total) == (100, 3000)

    def test_clear_closing_resets_to_none(self) -> None:
        stats = WorkerPoolStats()
        stats.set_closing("sincronizando AS400", 1, 2)
        stats.clear_closing()
        assert stats.snapshot().closing is None

    def test_snapshot_constructor_keeps_working_without_closing(self) -> None:
        # Campo trailing con default — los callers pre-144 no cambian.
        snap = WorkerPoolStatsSnapshot(
            pool_size=1, busy=0, idle=1, queue_depth=0, completed=0, failed=0
        )
        assert snap.closing is None


class TestWorkerPoolStats:
    def test_initial_snapshot_is_zero(self) -> None:
        stats = WorkerPoolStats()
        snap = stats.snapshot()
        assert snap.pool_size == 0
        assert snap.busy == 0
        assert snap.idle == 0
        assert snap.queue_depth == 0
        assert snap.completed == 0
        assert snap.failed == 0

    def test_busy_idle_balance(self) -> None:
        stats = WorkerPoolStats()
        stats.set_pool_size(4)
        stats.mark_busy("w1")
        stats.mark_busy("w2")
        snap = stats.snapshot()
        assert snap.busy == 2
        assert snap.idle == 2  # `pool_size` - `busy`

        stats.mark_idle("w1")
        snap = stats.snapshot()
        assert snap.busy == 1
        assert snap.idle == 3

    def test_idle_never_negative(self) -> None:
        """Si `busy` excede `pool_size` (race durante shrink), `idle` se clampea a 0."""
        stats = WorkerPoolStats()
        stats.set_pool_size(2)
        stats.mark_busy("w1")
        stats.mark_busy("w2")
        stats.mark_busy("w3")
        snap = stats.snapshot()
        assert snap.busy == 3
        assert snap.idle == 0

    def test_completed_and_failed_counters(self) -> None:
        stats = WorkerPoolStats()
        for _ in range(5):
            stats.mark_completed()
        for _ in range(2):
            stats.mark_failed()
        snap = stats.snapshot()
        assert snap.completed == 5
        assert snap.failed == 2

    def test_set_queue_depth(self) -> None:
        stats = WorkerPoolStats()
        stats.set_queue_depth(42)
        assert stats.snapshot().queue_depth == 42
        stats.set_queue_depth(-5)
        assert stats.snapshot().queue_depth == 0

    def test_thread_safety_under_concurrency(self) -> None:
        """Martillea los contadores desde 8 `thread`s; los totales finales deben coincidir."""
        stats = WorkerPoolStats()
        stats.set_pool_size(8)
        n_ops = 1000

        def hammer() -> None:
            for _ in range(n_ops):
                stats.mark_busy("w")
                stats.mark_completed()
                stats.mark_idle("w")

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = stats.snapshot()
        assert snap.completed == 8 * n_ops
        assert snap.busy == 0  # cada `mark_busy` apareado con `mark_idle`

    def test_snapshot_is_frozen(self) -> None:
        stats = WorkerPoolStats()
        snap = stats.snapshot()
        assert isinstance(snap, WorkerPoolStatsSnapshot)
        with pytest.raises(AttributeError):
            snap.pool_size = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ResizableSemaphore (025 phase 2)
# ---------------------------------------------------------------------------


class TestResizableSemaphore:
    def test_acquire_release_roundtrip(self) -> None:
        from cmcourier.services.worker_pool_stats import ResizableSemaphore

        sem = ResizableSemaphore(2)
        sem.acquire()
        assert sem.in_use == 1
        sem.acquire()
        assert sem.in_use == 2
        sem.release()
        assert sem.in_use == 1
        sem.release()
        assert sem.in_use == 0

    def test_acquire_blocks_at_capacity(self) -> None:
        import threading as _t
        import time as _time

        from cmcourier.services.worker_pool_stats import ResizableSemaphore

        sem = ResizableSemaphore(1)
        sem.acquire()
        acquired = _t.Event()

        def second() -> None:
            sem.acquire()
            acquired.set()

        t = _t.Thread(target=second, daemon=True)
        t.start()
        # Le da tiempo al `thread` para intentar y bloquearse.
        _time.sleep(0.05)
        assert not acquired.is_set()
        sem.release()
        t.join(timeout=1.0)
        assert acquired.is_set()
        sem.release()

    def test_set_capacity_grow_wakes_waiters(self) -> None:
        import threading as _t
        import time as _time

        from cmcourier.services.worker_pool_stats import ResizableSemaphore

        sem = ResizableSemaphore(1)
        sem.acquire()
        results: list[str] = []

        def waiter(name: str) -> None:
            sem.acquire()
            results.append(name)

        t1 = _t.Thread(target=waiter, args=("a",), daemon=True)
        t2 = _t.Thread(target=waiter, args=("b",), daemon=True)
        t1.start()
        t2.start()
        _time.sleep(0.05)
        assert results == []
        # Crece capacidad a 3 — ambos `waiter`s deberían proceder.
        sem.set_capacity(3)
        t1.join(timeout=1.0)
        t2.join(timeout=1.0)
        assert sorted(results) == ["a", "b"]
        # Limpieza.
        sem.release()
        sem.release()
        sem.release()

    def test_set_capacity_shrink_does_not_revoke(self) -> None:
        """Bajar el `cap` con `worker`s ya `in-flight` no los corta."""
        from cmcourier.services.worker_pool_stats import ResizableSemaphore

        sem = ResizableSemaphore(4)
        for _ in range(3):
            sem.acquire()
        sem.set_capacity(2)
        # 3 `worker`s siguen `in-flight`; `capacity` reporta 2 pero
        # `in_use` es 3.
        assert sem.capacity == 2
        assert sem.in_use == 3
        # Los `release` subsiguientes bajan `in_use` de nuevo.
        sem.release()
        sem.release()
        sem.release()
        assert sem.in_use == 0

    def test_context_manager(self) -> None:
        from cmcourier.services.worker_pool_stats import ResizableSemaphore

        sem = ResizableSemaphore(2)
        with sem:
            assert sem.in_use == 1
        assert sem.in_use == 0
