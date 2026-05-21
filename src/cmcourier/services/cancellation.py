"""Token de cancelación cooperativa del pipeline (097).

El TUI prende el token cuando el operador confirma la cancelación con
``"q"``; los orchestrators lo chequean en puntos seguros (al inicio de
cada método per-doc) para frenar de manera ordenada — *drain*: dejan de
tomar trabajo nuevo y esperan que lo que ya está en vuelo termine.

El token se crea siempre, incluso en corridas headless sin TUI; en ese
caso simplemente nunca se prende. Eso evita ramas ``if token is not
None`` desparramadas por los orchestrators.
"""

from __future__ import annotations

__all__ = ["CancellationToken"]

import threading


class CancellationToken:
    """Flag thread-safe de cancelación cooperativa.

    Lo prende el thread del TUI (``cancel``); lo leen los worker
    threads del pipeline (``is_cancelled``). Respaldado por un
    :class:`threading.Event`, así que es seguro entre threads y la
    transición es de un solo sentido (una vez cancelado, queda
    cancelado)."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Marca la corrida como cancelada. Idempotente."""
        self._event.set()

    def is_cancelled(self) -> bool:
        """True si se pidió la cancelación."""
        return self._event.is_set()
