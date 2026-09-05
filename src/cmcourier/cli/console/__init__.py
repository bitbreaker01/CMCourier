"""Consola de operación interactiva (123+).

Vive en ``cli/`` porque es composition root: construye adapters,
pipelines y orchestrators igual que los comandos ``run``, pero desde
una TUI Textual. Reusa los renderers de :mod:`cmcourier.tui` para el
monitoreo.
"""

from cmcourier.cli.console.state import ConsoleState, SessionCredentials

__all__ = ["ConsoleState", "SessionCredentials"]
