"""Helpers compartidos de los tests pilot de la consola."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any


async def wait_for(pilot: Any, cond: Callable[[], bool], timeout: float = 4.0) -> bool:
    """Espera activa a una condición de UI — push_screen y los workers
    son asíncronos; un pause() suelto no alcanza siempre."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001 — la condición puede consultar widgets aún no montados
            pass
        # pause() bombea el mensaje-loop de Textual, incluyendo los
        # call_from_thread que postean los workers; el sleep cede al
        # scheduler para que el worker thread avance.
        await asyncio.sleep(0.03)
        await pilot.pause()
    return cond()


_TAB_BY_KEY = {
    "1": "inicio",
    "2": "credenciales",
    "3": "config",
    "4": "doctor",
    "5": "correr",
    "6": "monitor",
    "7": "batches",
    "f1": "inicio",
    "f2": "credenciales",
    "f3": "config",
    "f4": "doctor",
    "f5": "correr",
    "f6": "monitor",
    "f7": "batches",
}


async def goto(pilot: Any, app: Any, key: str) -> None:
    """Cambia de tab y ESPERA a que el cambio se asiente — el switch es
    asíncrono y una tecla inmediata puede llegar al tab anterior."""
    from textual.widgets import TabbedContent

    await pilot.press(key)
    target = _TAB_BY_KEY[key]
    assert await wait_for(pilot, lambda: app.query_one(TabbedContent).active == target), (
        f"el tab no llegó a {target}"
    )
