"""Servicio de coordinación de lanes duales `heavy`/`light` (POST-MVP §1, 036).

Es dueño de dos :class:`ResizableSemaphore` y de dos contadores
:class:`WorkerPoolStats` (uno por lane). Se acopla al `AIMD` vía
:meth:`set_total_budget` (el `AIMD` es dueño de la cantidad TOTAL de
workers; este controlador es dueño de la distribución). Un `thread`
demonio chequea periódicamente lanes drenados y migra capacidad
hacia el lane que aún tenga trabajo.

`Floor`: cada lane retiene una capacidad mínima de 1 (coincide con
la garantía ``max(1, n)`` del ``ResizableSemaphore``). Un evento
"completamente drenado al otro lane" deja al lado drenado con 1
worker de reserva: inofensivo, porque sin ítems no hay llamadas a
acquire.

`Thread-safety`: todo mutator público toma el lock interno. Los
métodos de snapshot toman el lock brevemente para capturar
contadores y lo liberan antes de devolver los valores congelados.
"""

from __future__ import annotations

__all__ = ["Lane", "LaneController", "LaneSnapshot"]

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cmcourier.services.lane_splitter import Lane
from cmcourier.services.worker_pool_stats import (
    ResizableSemaphore,
    WorkerPoolStats,
    WorkerPoolStatsSnapshot,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LaneSnapshot:
    """Vista read-only de ambos lanes en un instante. La consume la TUI."""

    heavy: WorkerPoolStatsSnapshot
    light: WorkerPoolStatsSnapshot
    total_budget: int


class LaneController:
    """Dos `ResizableSemaphore` + daemon de rebalance dirigido por drenaje.

    Ciclo de vida:

    1. El constructor fija el split inicial `heavy`/`light` a partir
       de ``heavy_initial_ratio``.
    2. :meth:`start` lanza el daemon de rebalance. Es idempotente.
    3. :meth:`acquire` / :meth:`release` para la concurrencia por
       doc.
    4. :meth:`set_total_budget` desde el controlador `AIMD`.
    5. :meth:`stop` une el `thread` demonio (se llama al apagar el
       `pipeline`).
    """

    def __init__(
        self,
        *,
        total_budget: int,
        heavy_initial_ratio: float,
        rebalance_interval_s: float,
        idle_threshold_s: float,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._total = max(2, int(total_budget))
        self._heavy_initial_ratio = float(heavy_initial_ratio)
        heavy_cap, light_cap = self._initial_split(self._total, heavy_initial_ratio)
        self._heavy_sem = ResizableSemaphore(heavy_cap)
        self._light_sem = ResizableSemaphore(light_cap)
        self._heavy_stats = WorkerPoolStats()
        self._light_stats = WorkerPoolStats()
        self._heavy_stats.set_pool_size(heavy_cap)
        self._light_stats.set_pool_size(light_cap)
        self._rebalance_interval_s = float(rebalance_interval_s)
        self._idle_threshold_s = float(idle_threshold_s)
        self._clock = clock
        self._log = logger or _logger
        # Tracking de drenaje: cuando la `queue` de un lane llega a
        # cero por primera vez se estampa ``_*_first_empty_at``. La
        # heurística de rebalance migra recién después de que el lane
        # permanece vacío durante ``idle_threshold_s``. ``None``
        # significa que el lane actualmente no está vacío (o que
        # todavía no fue observado).
        self._heavy_first_empty_at: float | None = None
        self._light_first_empty_at: float | None = None
        # 115: lane que cedió su capacidad en la última migración por
        # drenaje. Cuando ``set_queue_depth`` le reporta trabajo nuevo,
        # el split inicial se restituye inmediatamente — pre-115 la
        # única vía de vuelta era que la OTRA lane se vaciara
        # ``idle_threshold_s``, algo que con flujo continuo no pasa
        # nunca (la lane drenada quedaba en capacidad 1 para siempre).
        self._migrated_from: Lane | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- split inicial -----

    @staticmethod
    def _initial_split(total: int, ratio: float) -> tuple[int, int]:
        """Devuelve ``(heavy, light)`` con ambos >= 1 y suma == total."""
        if total < 2:
            return (1, 1)  # degenerado: aplica el `floor`, no sumará total
        heavy = max(1, math.ceil(total * ratio))
        heavy = min(heavy, total - 1)  # dejar al menos 1 para `light`
        return (heavy, total - heavy)

    # ----- ciclo de vida -----

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._rebalance_loop,
            name="cmcourier-lane-rebalance",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._rebalance_interval_s * 2 + 1.0)

    # ----- concurrencia por lane -----

    def acquire(self, lane: Lane) -> None:
        sem = self._sem_for(lane)
        sem.acquire()
        stats = self._stats_for(lane)
        stats.mark_busy(threading.current_thread().name)

    def release(self, lane: Lane) -> None:
        stats = self._stats_for(lane)
        stats.mark_idle(threading.current_thread().name)
        self._sem_for(lane).release()

    def mark_completed(self, lane: Lane) -> None:
        self._stats_for(lane).mark_completed()

    def mark_failed(self, lane: Lane) -> None:
        self._stats_for(lane).mark_failed()

    def set_queue_depth(self, lane: Lane, depth: int) -> None:
        """Registra una actualización de profundidad de `queue` para
        el lane indicado.

        Efecto colateral: trackea el momento en que la `queue` llegó
        a cero por primera vez para la heurística de drenaje. El lane
        permanece "actualmente vacío" hasta la próxima actualización
        con profundidad positiva, momento en el cual se limpia el
        sello. ``rebalance_tick`` migra capacidad cuando
        ``now - first_empty_at >= idle_threshold_s``.
        """
        self._stats_for(lane).set_queue_depth(depth)
        restored: dict[str, int] | None = None
        with self._lock:
            if depth > 0:
                if lane == "heavy":
                    self._heavy_first_empty_at = None
                else:
                    self._light_first_empty_at = None
                # 115: la lane drenada recibió trabajo → restituir el
                # split inicial sobre el budget vigente, sin esperar
                # ningún tick de rebalance.
                if self._migrated_from == lane:
                    restored = self._restore_split_locked()
            else:
                now = self._clock()
                if lane == "heavy" and self._heavy_first_empty_at is None:
                    self._heavy_first_empty_at = now
                elif lane == "light" and self._light_first_empty_at is None:
                    self._light_first_empty_at = now
        if restored is not None:
            self._log.info(
                "lane restitution: %s has work again",
                lane,
                extra={"event": "lane_restitution", "lane": lane, **restored},
            )

    def _restore_split_locked(self) -> dict[str, int]:
        """115: vuelve al split inicial (ratio configurado sobre el
        budget vigente del AIMD). Llamar con ``self._lock`` tomado."""
        heavy_cap, light_cap = self._initial_split(self._total, self._heavy_initial_ratio)
        self._heavy_sem.set_capacity(heavy_cap)
        self._light_sem.set_capacity(light_cap)
        self._heavy_stats.set_pool_size(heavy_cap)
        self._light_stats.set_pool_size(light_cap)
        self._migrated_from = None
        return {"new_heavy": heavy_cap, "new_light": light_cap}

    # ----- acoplamiento con `AIMD` -----

    def set_total_budget(self, new_total: int) -> None:
        """Hook del `AIMD`: redistribuye proporcionalmente preservando el ratio.

        Cada lane retiene al menos un slot cuando ``new_total >= 2``.
        """
        new_total = max(2, int(new_total))
        with self._lock:
            heavy_cap = self._heavy_sem.capacity
            light_cap = self._light_sem.capacity
            current_total = heavy_cap + light_cap
            ratio = self._heavy_initial_ratio if current_total <= 0 else heavy_cap / current_total
            new_heavy = max(1, round(new_total * ratio))
            new_heavy = min(new_heavy, new_total - 1)
            new_light = new_total - new_heavy
            self._heavy_sem.set_capacity(new_heavy)
            self._light_sem.set_capacity(new_light)
            self._heavy_stats.set_pool_size(new_heavy)
            self._light_stats.set_pool_size(new_light)
            self._total = new_total

    # ----- snapshots -----

    def snapshot(self) -> LaneSnapshot:
        return LaneSnapshot(
            heavy=self._heavy_stats.snapshot(),
            light=self._light_stats.snapshot(),
            total_budget=self._total,
        )

    @property
    def heavy_capacity(self) -> int:
        return self._heavy_sem.capacity

    @property
    def light_capacity(self) -> int:
        return self._light_sem.capacity

    # ----- loop de rebalance -----

    def rebalance_tick(self) -> None:
        """Una iteración de la heurística de drenaje. Pública para testing."""
        with self._lock:
            now = self._clock()
            heavy_idle_s = (
                now - self._heavy_first_empty_at if self._heavy_first_empty_at is not None else 0.0
            )
            light_idle_s = (
                now - self._light_first_empty_at if self._light_first_empty_at is not None else 0.0
            )
            heavy_cap = self._heavy_sem.capacity
            light_cap = self._light_sem.capacity
            total = heavy_cap + light_cap
            new_heavy = heavy_cap
            new_light = light_cap
            migrated_from: Lane | None = None
            # Cuando un lane permaneció vacío durante el tiempo
            # suficiente, migra TODA la capacidad al otro. El lane
            # drenado conserva el `floor` de 1 del
            # ``ResizableSemaphore``, pero como su `queue` está vacía
            # no va a haber acquires futuros sobre él, así que ese
            # slot queda efectivamente libre.
            if heavy_idle_s >= self._idle_threshold_s and heavy_cap > 1:
                new_heavy = 1
                new_light = total
                migrated_from = "heavy"
            elif light_idle_s >= self._idle_threshold_s and light_cap > 1:
                new_light = 1
                new_heavy = total
                migrated_from = "light"
            if migrated_from is None:
                return
            self._heavy_sem.set_capacity(new_heavy)
            self._light_sem.set_capacity(new_light)
            self._heavy_stats.set_pool_size(new_heavy)
            self._light_stats.set_pool_size(new_light)
            # 115: recordar quién cedió — la restitución se dispara
            # cuando esta lane vuelva a reportar trabajo.
            self._migrated_from = migrated_from
            event = {
                "event": "lane_rebalance",
                "from": migrated_from,
                "to": "light" if migrated_from == "heavy" else "heavy",
                "previous_heavy": heavy_cap,
                "previous_light": light_cap,
                "new_heavy": new_heavy,
                "new_light": new_light,
            }
        self._log.info("lane rebalance: %s -> other", migrated_from, extra=event)

    def _rebalance_loop(self) -> None:
        while not self._stop_event.is_set():
            self.rebalance_tick()
            self._stop_event.wait(self._rebalance_interval_s)

    # ----- internos -----

    def _sem_for(self, lane: Lane) -> ResizableSemaphore:
        return self._heavy_sem if lane == "heavy" else self._light_sem

    def _stats_for(self, lane: Lane) -> WorkerPoolStats:
        return self._heavy_stats if lane == "heavy" else self._light_stats
