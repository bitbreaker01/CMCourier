"""Modal de confirmación para cancelar una corrida en progreso (097).

El operador aprieta ``"q"`` con el pipeline corriendo → el TUI abre
este modal. ``s`` confirma la cancelación, ``n`` (o ``escape``) la
descarta y la corrida sigue. El resultado se devuelve vía
``dismiss(bool)``.
"""

from __future__ import annotations

__all__ = ["ConfirmCancelScreen"]

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static


class ConfirmCancelScreen(ModalScreen[bool]):
    """Modal sí/no — confirma cancelar la corrida en progreso."""

    BINDINGS = [
        Binding("s", "confirm", "Sí, cancelar"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "keep_running", "No, seguir"),
        Binding("escape", "keep_running", "No"),
    ]

    DEFAULT_CSS = """
    ConfirmCancelScreen {
        align: center middle;
    }
    ConfirmCancelScreen #confirm_box {
        width: 64;
        height: auto;
        border: thick $warning;
        background: $panel;
        color: $text;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            "¿Cancelar la corrida en progreso?\n\n"
            "Se frena de forma ordenada: los uploads en vuelo terminan,\n"
            "y los documentos sin procesar quedan pendientes para un\n"
            "resume. El tracking queda consistente.\n\n"
            "[s] Sí, cancelar      [n] No, seguir",
            id="confirm_box",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_keep_running(self) -> None:
        self.dismiss(False)
