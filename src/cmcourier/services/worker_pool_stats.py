"""Contadores `thread-safe` para el `worker pool` de S5 (025).

El `pool` en sí es un
:class:`concurrent.futures.ThreadPoolExecutor` en
:mod:`cmcourier.orchestrators.staged`; este módulo solo es dueño
del *estado visible* que la TUI y el controlador de auto-tune
necesitan leer.

Los snapshots son valores ``WorkerPoolStatsSnapshot`` congelados
(sin referencias de vuelta al estado mutable), de modo que los
consumidores pueden compararlos, loguearlos o renderizarlos de
manera segura desde cualquier `thread`.
"""

from __future__ import annotations

__all__ = ["ResizableSemaphore", "WorkerPoolStats", "WorkerPoolStatsSnapshot"]

import threading
from dataclasses import dataclass
from types import TracebackType


@dataclass(frozen=True, slots=True)
class WorkerPoolStatsSnapshot:
    """Vista read-only del `pool` de S5 en un instante."""

    pool_size: int
    busy: int
    idle: int
    queue_depth: int
    completed: int
    failed: int


class WorkerPoolStats:
    """Contadores mutables y `thread-safe` del `pool` de S5.

    Hooks que llama el orchestrator desde el path de submisión y
    finalización de workers:

    * ``set_pool_size``: una vez al entrar a S5, y otra vez en cada
      resize de auto-tune (Fase 2 de 025).
    * ``mark_busy`` / ``mark_idle``: encierran cada upload.
    * ``mark_completed`` / ``mark_failed``: resultado por doc.
    * ``set_queue_depth``: en cada `tick` de ``as_completed``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool_size = 0
        self._busy = 0
        self._completed = 0
        self._failed = 0
        self._queue_depth = 0

    # ------------------------------------------------------- mutadores

    def set_pool_size(self, n: int) -> None:
        with self._lock:
            self._pool_size = max(0, int(n))

    def mark_busy(self, worker_name: str) -> None:
        del worker_name  # reservado para futuras stats por worker
        with self._lock:
            self._busy += 1

    def mark_idle(self, worker_name: str) -> None:
        del worker_name
        with self._lock:
            self._busy = max(0, self._busy - 1)

    def mark_completed(self) -> None:
        with self._lock:
            self._completed += 1

    def mark_failed(self) -> None:
        with self._lock:
            self._failed += 1

    def set_queue_depth(self, n: int) -> None:
        with self._lock:
            self._queue_depth = max(0, int(n))

    def decrement_queue_depth(self) -> None:
        """122: decremento atómico con piso en 0 — pre-122 el caller
        hacía ``set_queue_depth(snapshot().queue_depth - 1)``: dos
        locks y una dataclass frozen por documento."""
        with self._lock:
            self._queue_depth = max(0, self._queue_depth - 1)

    # ------------------------------------------------------- snapshot

    def snapshot(self) -> WorkerPoolStatsSnapshot:
        with self._lock:
            return WorkerPoolStatsSnapshot(
                pool_size=self._pool_size,
                busy=self._busy,
                idle=max(0, self._pool_size - self._busy),
                queue_depth=self._queue_depth,
                completed=self._completed,
                failed=self._failed,
            )


# ---------------------------------------------------------------------------
# ResizableSemaphore (025 phase 2)
# ---------------------------------------------------------------------------


class ResizableSemaphore:
    """Limitador de concurrencia con `soft-cap` que soporta resize en
    runtime.

    El :class:`threading.Semaphore` de Python tiene un valor inicial
    fijo y no expone API pública de resize. Nuestro controlador de
    auto-tune quiere ajustar la concurrencia efectiva hacia arriba o
    abajo sin desarmar el ``ThreadPoolExecutor`` subyacente. Esta
    clase envuelve una :class:`threading.Condition` y un contador
    para que:

    * :meth:`acquire` bloquee mientras ``in_use >= capacity``.
    * :meth:`release` decremente y despierte a un waiter.
    * :meth:`set_capacity` ajuste el cap y despierte a los waiters
      que ahora entran.

    Usar como `context manager` (``with sem: ...``) para hacer
    acquire/release dentro de un solo bloque.
    """

    def __init__(self, capacity: int) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._capacity = max(1, int(capacity))
        self._in_use = 0

    def acquire(self) -> None:
        with self._cond:
            while self._in_use >= self._capacity:
                self._cond.wait()
            self._in_use += 1

    def release(self) -> None:
        with self._cond:
            self._in_use = max(0, self._in_use - 1)
            self._cond.notify()

    def set_capacity(self, n: int) -> None:
        with self._cond:
            new_cap = max(1, int(n))
            grew = new_cap > self._capacity
            self._capacity = new_cap
            if grew:
                self._cond.notify_all()

    @property
    def capacity(self) -> int:
        with self._lock:
            return self._capacity

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    def __enter__(self) -> ResizableSemaphore:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()
