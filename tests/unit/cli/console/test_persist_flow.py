"""[3] CONFIG → `w` escribe los overrides aplicados al YAML (135, E4)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input

from cmcourier.cli.console.app import ConfirmScreen, ConsoleApp
from cmcourier.cli.console.config_pane import ConfigPane
from tests.unit.cli.console.conftest import goto
from tests.unit.cli.console.test_console_app import _fake_report, _make_config

pytestmark = pytest.mark.unit


def _notes(app: Any) -> list[str]:
    return [n.message for n in app._notifications]


class TestPersistFlow:
    def test_w_writes_applied_overrides_and_clears_session(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                app.state.set_doctor_report(_fake_report(), group="all")
                await goto(pilot, app, "3")
                app.query_one("#ov-workers", Input).value = "8"
                app.query_one(ConfigPane).apply_draft()
                await pilot.pause()
                assert app.state.overrides.workers == 8
                app.state.doctor_stale_reason = ""  # el doctor volvió a correr

                await pilot.press("w")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmScreen)
                app.screen.query_one("#yes", Button).press()
                await pilot.pause()
                await pilot.pause()

                assert "  workers: 8\n" in path.read_text()
                assert app.config.cmis.workers == 8
                assert app.state.overrides.is_empty()
                assert app.state.doctor_stale
                assert list(tmp_path.glob("config.yaml.bak-*"))
                assert app.query_one("#ov-workers", Input).placeholder == "(yaml) 8"
                assert app.query_one("#ov-workers", Input).value == ""
                assert any("config.yaml.bak-" in m for m in _notes(app))

        asyncio.run(_run())

    def test_w_with_dirty_draft_notifies_and_does_not_open_modal(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            before = path.read_text()
            async with app.run_test() as pilot:
                await goto(pilot, app, "3")
                app.query_one("#ov-workers", Input).value = "8"  # sin `a`
                await pilot.press("w")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert any("primero" in m.lower() for m in _notes(app))
            assert path.read_text() == before

        asyncio.run(_run())

    def test_w_with_nothing_applied_notifies(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "3")
                await pilot.press("w")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert any("nada" in m.lower() for m in _notes(app))

        asyncio.run(_run())

    def test_w_outside_config_tab_is_ignored(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "3")
                app.query_one("#ov-workers", Input).value = "8"
                app.query_one(ConfigPane).apply_draft()
                await goto(pilot, app, "1")
                await pilot.press("w")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)

        asyncio.run(_run())
