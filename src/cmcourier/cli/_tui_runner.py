"""Runner que combina un pipeline en vuelo con la TUI en vivo (025).

El pipeline es CPU/IO-bound y prefiere correr en un worker thread;
el event loop del ``App`` de textual corre en el thread principal
(textual espera el thread principal para su loop de asyncio por
defecto + manejo de senales). Los dos se comunican en una direccion
a traves de un :class:`TUIDataProvider`.

Semantica para el operador:

* La TUI arranca antes que el pipeline. ``mark_batch_started`` se
  dispara desde el worker thread en el momento en que la corrida
  comienza.
* Cuando el pipeline termina (exito o excepcion), el worker llama
  ``mark_batch_complete`` y la TUI pasa al modo "run complete".
  El operador presiona ``[Q]`` para salir; presionar antes sale de
  la TUI pero el worker igual hace join (asi el pipeline nunca queda
  abandonado a mitad de vuelo).
* Las excepciones levantadas dentro del thread del pipeline son
  re-elevadas en el thread principal una vez que el operador sale
  de la TUI.
"""

from __future__ import annotations

__all__ = ["TUIRunOutcome", "run_orchestrator_with_tui"]

import sys
import threading
from dataclasses import dataclass
from typing import Any

from cmcourier.orchestrators.multi_batch import (
    MultiBatchOrchestrator,
    MultiBatchRunReport,
)
from cmcourier.orchestrators.streaming import StreamingOrchestrator
from cmcourier.tui import CMCourierTUI, TUIDataProvider


@dataclass(slots=True)
class TUIRunOutcome:
    """Resultado del worker-thread devuelto al caller despues de que la TUI sale."""

    report: MultiBatchRunReport | None = None
    exception: BaseException | None = None


def tty_available() -> bool:
    """Indica si el proceso actual puede renderizar una UI de textual.

    textual escribe la TUI en ``stderr``; ese es el que chequeamos.
    ``stdin`` / ``stdout`` pueden estar redirigidos sin romper el render
    (los operadores a veces hacen ``cmcourier ... | grep s5_done``).
    """
    return bool(sys.stderr.isatty())


def run_orchestrator_with_tui(
    *,
    orchestrator: MultiBatchOrchestrator | StreamingOrchestrator,
    data_provider: TUIDataProvider,
    orchestrator_kwargs: dict[str, Any],
) -> TUIRunOutcome:
    """Corre un orchestrator en un worker thread mientras la TUI duena el thread main.

    ``orchestrator_kwargs`` se hace splat en ``orchestrator.run(**kwargs)``.
    Tanto :class:`MultiBatchOrchestrator` (modo por batches) como
    :class:`StreamingOrchestrator` (modo streaming de 063) exponen la
    misma forma de ``.run(...)`` y devuelven un :class:`MultiBatchRunReport`.
    """
    outcome = TUIRunOutcome()

    def _worker() -> None:
        try:
            data_provider.mark_batch_started(
                batch_id=orchestrator_kwargs.get("resume_batch_id") or ""
            )
            outcome.report = orchestrator.run(**orchestrator_kwargs)
        except BaseException as exc:  # noqa: BLE001 — re-elevada en el thread main
            outcome.exception = exc
        finally:
            data_provider.mark_batch_complete()

    worker = threading.Thread(target=_worker, name="cmcourier-pipeline", daemon=False)
    worker.start()
    # 097: el TUI comparte el `cancel_token` del orchestrator. Apretar
    # "q" durante la corrida abre un modal de confirmación; al confirmar,
    # se prende el token y el pipeline drena de forma ordenada.
    app = CMCourierTUI(data_provider, cancel_token=orchestrator.cancel_token)
    try:
        app.run()
    finally:
        # Siempre esperamos a que el pipeline termine antes de volver.
        # Pre-097 apretar "q" cerraba el viewer y el join bloqueaba hasta
        # que la corrida terminaba sola. Con 097, "q" confirmado cancela
        # el pipeline — el join ahora espera solo el drain (segundos).
        worker.join()
    return outcome
