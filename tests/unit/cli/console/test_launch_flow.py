"""Tests pilot del flujo de lanzamiento (124) — guardas y draft/applied."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Select

from cmcourier.cli.console.app import ConfirmScreen, ConsoleApp
from cmcourier.cli.console.config_pane import ConfigPane
from cmcourier.cli.console.run_pane import RunPane
from cmcourier.cli.console.runner import LaunchSpec
from cmcourier.cli.doctor import CheckResult, CheckStatus, DoctorReport
from tests.unit.cli.console.conftest import goto
from tests.unit.cli.console.conftest import wait_for as _wait_for
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = pytest.mark.unit


class _FakeManager:
    """Reemplaza al ConsoleRunManager: registra el launch sin correr nada."""

    def __init__(self) -> None:
        self.launched: list[LaunchSpec] = []
        self.active = False

    def launch(self, spec: LaunchSpec) -> None:
        self.launched.append(spec)

    def cancel(self) -> None:  # pragma: no cover - interfaz
        pass


def _ready_state(app: ConsoleApp) -> None:
    app.state.record_conn_result("cmis", ok=True, message="ok")
    app.state.record_conn_result("as400", ok=True, message="ok")
    app.state.set_doctor_report(
        DoctorReport(
            results=(CheckResult(name="a", status=CheckStatus.PASS, message="ok"),),
            elapsed_seconds=0.1,
        ),
        group="all",
    )


class TestLaunchGuards:
    def test_launch_without_creds_routes_to_credentials(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            fake = _FakeManager()
            app.run_manager = fake  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "5")
                await pilot.press("r")
                from textual.widgets import TabbedContent

                assert await _wait_for(
                    pilot, lambda: app.query_one(TabbedContent).active == "credenciales"
                )
                assert fake.launched == []

        asyncio.run(_run())

    def test_unapproved_doctor_asks_then_launches(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            fake = _FakeManager()
            app.run_manager = fake  # type: ignore[assignment]
            async with app.run_test() as pilot:
                app.state.record_conn_result("cmis", ok=True, message="ok")
                app.state.record_conn_result("as400", ok=True, message="ok")
                await goto(pilot, app, "5")
                await pilot.press("r")
                await pilot.pause()
                assert await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                app.screen.query_one("#yes", Button).press()  # lanzar igual
                assert await _wait_for(pilot, lambda: len(fake.launched) == 1)

        asyncio.run(_run())

    def test_prd_requires_typed_confirmation(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path, environment="prd")
            app = ConsoleApp(config=config, config_path=path)
            fake = _FakeManager()
            app.run_manager = fake  # type: ignore[assignment]
            async with app.run_test() as pilot:
                _ready_state(app)
                await goto(pilot, app, "5")
                await pilot.press("r")
                assert await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                assert await _wait_for(pilot, lambda: bool(app.screen.query("#ctext")))
                # sin tipear PRD, el yes no pasa
                app.screen.query_one("#yes", Button).press()
                await pilot.pause()
                assert fake.launched == []
                app.screen.query_one("#ctext", Input).value = "PRD"
                app.screen.query_one("#yes", Button).press()
                assert await _wait_for(pilot, lambda: len(fake.launched) == 1)

        asyncio.run(_run())

    def test_launch_uses_applied_not_draft(self, tmp_path: Path) -> None:
        """C4: un draft sin promover no cambia la config efectiva."""

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            fake = _FakeManager()
            app.run_manager = fake  # type: ignore[assignment]
            async with app.run_test() as pilot:
                _ready_state(app)
                await goto(pilot, app, "3")
                app.query_one("#ov-workers", Input).value = "63"
                # SIN apretar a → draft dirty, applied vacío
                assert app.draft_dirty()
                assert app.state.overrides.is_empty()
                await goto(pilot, app, "5")
                summary = str(app.query_one(RunPane).query_one("#run-summary").renderable)
                assert "workers S5    4" in summary  # el del YAML (default)
                assert "SIN GUARDAR" in str(
                    app.query_one(RunPane).query_one("#run-guard").renderable
                )
                # promover y verificar que ahora sí
                await goto(pilot, app, "3")
                app.query_one(ConfigPane).apply_draft()
                await pilot.pause()
                assert app.state.overrides.workers == 63
                assert app.state.doctor_stale

        asyncio.run(_run())

    def test_streaming_hides_resume(self, tmp_path: Path) -> None:
        """C1: resume solo en batched."""

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _FakeManager()  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "5")
                pane = app.query_one(RunPane)
                assert pane.query_one("#row-mode").display is True  # yaml default: batched
                app.query_one("#run-mode", Select).value = "resume"
                await pilot.pause()
                # override efectivo a streaming → el modo resume desaparece
                from cmcourier.cli.console.overrides import SessionOverrides

                app.state.overrides = SessionOverrides(mode="streaming")
                pane.refresh_summary()
                assert pane.query_one("#row-mode").display is False
                assert "resume no disponible" in str(pane.query_one("#run-summary").renderable)

        asyncio.run(_run())
