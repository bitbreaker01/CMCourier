"""Progreso del sync con AS400 (144).

Valor compartido entre :mod:`cmcourier.services.recovery` (``sync
recover``) y :mod:`cmcourier.services.reconciler` (pasada final de la
corrida). Vive en su propio módulo sin dependencias para que ninguno
de los dos servicios tenga que importar al otro por una dataclass.
"""

from __future__ import annotations

__all__ = ["ProgressEmitter", "SyncProgress"]

import logging
from collections.abc import Callable
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncProgress:
    """144: un evento de progreso del sync con AS400.

    ``phase`` es texto para el operador (``"insertando"``); ``done`` /
    ``total`` son ``0/0`` en fases sin conteo previo."""

    phase: str
    done: int
    total: int


class ProgressEmitter:
    """144: envuelve ``on_progress`` para que un callback roto (UI
    muerta, widget que ya no existe) no aborte el trabajo que reporta.

    Se invoca SIEMPRE desde el hilo que llamó al servicio — nunca desde
    los hilos de un pool — porque el callback suele tocar UI."""

    def __init__(self, on_progress: Callable[[SyncProgress], None] | None) -> None:
        self._cb = on_progress

    def __call__(self, phase: str, done: int, total: int) -> None:
        if self._cb is None:
            return
        try:
            self._cb(SyncProgress(phase, done, total))
        except Exception:  # noqa: BLE001 — el progreso es cosmético, el trabajo no
            _log.exception("sync: on_progress falló en fase %r (%d/%d)", phase, done, total)
