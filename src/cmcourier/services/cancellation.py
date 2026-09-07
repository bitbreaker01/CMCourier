"""Token de cancelación cooperativa del pipeline (097) + compuerta de pausa (132).

El TUI prende el token cuando el operador confirma la cancelación con
``"q"``; los orchestrators lo chequean en puntos seguros (al inicio de
cada método per-doc) para frenar de manera ordenada — *drain*: dejan de
tomar trabajo nuevo y esperan que lo que ya está en vuelo termine.

Desde 132 el mismo token lleva una compuerta de pausa: ``pause()`` cierra
la compuerta y los workers que llegan a ``checkpoint()`` se quedan
esperando ANTES de tomar trabajo nuevo; ``resume()`` los suelta. La
cancelación sigue siendo de un solo sentido y también abre la compuerta,
para que nadie quede esperando una reanudación que no va a llegar.

El token se crea siempre, incluso en corridas headless sin TUI; en ese
caso simplemente nunca se prende. Eso evita ramas ``if token is not
None`` desparramadas por los orchestrators.
"""

from __future__ import annotations

__all__ = ["CancellationToken"]

import threading


class CancellationToken:
    """Flag thread-safe de cancelación cooperativa + compuerta de pausa.

    Lo prende el thread del TUI (``cancel`` / ``pause`` / ``resume``); lo
    leen los worker threads del pipeline (``is_cancelled`` /
    ``checkpoint``). Respaldado por dos :class:`threading.Event`: uno de
    cancelación (transición de un solo sentido) y uno de "corriendo"
    (seteado por defecto; ``pause`` lo limpia, ``resume`` lo vuelve a
    setear)."""

    __slots__ = ("_cancelled", "_running")

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._running = threading.Event()
        self._running.set()

    def cancel(self) -> None:
        """Marca la corrida como cancelada y abre la compuerta. Idempotente."""
        self._cancelled.set()
        self._running.set()

    def is_cancelled(self) -> bool:
        """True si se pidió la cancelación."""
        return self._cancelled.is_set()

    # ------------------------------------------------------------ pausa (132)

    def pause(self) -> None:
        """Cierra la compuerta: los workers se detienen en el próximo
        ``checkpoint``. Idempotente; no hace nada si ya está cancelado."""
        if not self._cancelled.is_set():
            self._running.clear()

    def resume(self) -> None:
        """Abre la compuerta. Idempotente."""
        self._running.set()

    def is_paused(self) -> bool:
        """True si la compuerta está cerrada (pausado y no cancelado)."""
        return not self._running.is_set() and not self._cancelled.is_set()

    def checkpoint(self) -> bool:
        """Punto seguro de los workers: bloquea mientras la corrida esté
        pausada; devuelve ``False`` si fue cancelada (antes o durante la
        espera) y ``True`` si puede seguir."""
        self._running.wait()
        return not self._cancelled.is_set()
