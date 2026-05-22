"""Watchdog de deadline del pipeline (103).

Un :class:`DeadlineWatchdog` corre un thread daemon que, transcurrido
``duration_s`` de tiempo de pared, **prende el** :class:`CancellationToken`
del orchestrator — exactamente la misma señal de drain cooperativo que
dispara la tecla "q" del TUI (097). El pipeline drena de forma ordenada:
deja de tomar trabajo nuevo y espera que lo in-flight termine.

El watchdog NO mata el proceso. Reusa el mecanismo de cancelación de
097; no lo puentea.

Funciona headless (sin TUI): el thread no depende de la UI. Es el caso
de uso principal — un soak desatendido de 24 h corre con ``--no-tui``.

Diseño thread-safe respaldado por :class:`threading.Event`:

* ``stop()`` prende un event de parada; si el pipeline terminó antes del
  deadline, el thread sale sin disparar.
* el disparo es de un solo sentido y ``CancellationToken.cancel()`` ya
  es idempotente, así que ``stop()`` tras el disparo es inofensivo.
"""

from __future__ import annotations

__all__ = ["DeadlineWatchdog"]

import logging
import threading
import time

from cmcourier.services.cancellation import CancellationToken

_log = logging.getLogger(__name__)


class DeadlineWatchdog:
    """Prende un :class:`CancellationToken` tras ``duration_s`` segundos."""

    __slots__ = ("_token", "_duration_s", "_stop", "_fired", "_thread", "_started_at")

    def __init__(self, token: CancellationToken, duration_s: float) -> None:
        if duration_s <= 0:
            raise ValueError(f"duration_s must be positive, got {duration_s!r}")
        self._token = token
        self._duration_s = float(duration_s)
        self._stop = threading.Event()
        self._fired = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None

    def start(self) -> None:
        """Lanza el thread daemon que cuenta hacia el deadline."""
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name="cmcourier-deadline",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Detiene el watchdog. Idempotente y seguro antes de ``start()``.

        Si el pipeline terminó antes del deadline, esto impide el
        disparo. Si el deadline ya disparó, es un no-op.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    @property
    def fired(self) -> bool:
        """``True`` si el deadline venció y se prendió el token."""
        return self._fired.is_set()

    def _run(self) -> None:
        # ``Event.wait`` devuelve True si lo prendió ``stop()``, o False si
        # se agotó el timeout — ese False es el deadline vencido.
        stopped = self._stop.wait(self._duration_s)
        if stopped:
            return
        elapsed = time.monotonic() - (self._started_at or time.monotonic())
        _log.warning(
            "pipeline stopped by --max-duration deadline",
            extra={
                "event": "pipeline_stopped_by_deadline",
                "max_duration_s": self._duration_s,
                "elapsed_s": round(elapsed, 3),
            },
        )
        # ``_fired`` se prende ÚLTIMO: que ``fired`` sea True le garantiza
        # al observador que el token ya quedó cancelado.
        self._token.cancel()
        self._fired.set()
