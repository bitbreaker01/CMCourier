"""128: pestaña SYNC — disponibilidad (E3) y recover gated por dry-run (E4)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input, Static

import cmcourier.cli.console.sync_pane as sync_pane_module
from cmcourier.cli.console.app import ConfirmScreen, ConsoleApp
from cmcourier.cli.console.state import SessionCredentials
from cmcourier.cli.console.sync_pane import SyncPane
from cmcourier.config.schema import As400ConnectionConfig, As400SyncConfig, PipelineConfig
from cmcourier.services.recovery import RecoveryResult, SyncProgress
from tests.unit.cli.console.conftest import goto
from tests.unit.cli.console.conftest import wait_for as _wait_for
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = pytest.mark.unit

_BUTTONS = ("#sy-status", "#sy-dry", "#sy-apply", "#sy-resolve")


def _with_sync(config: PipelineConfig) -> PipelineConfig:
    sync = As400SyncConfig(enabled=True, connection=As400ConnectionConfig(host="as400.test"))
    tracking = config.tracking.model_copy(update={"as400_sync": sync})
    return config.model_copy(update={"tracking": tracking})


def _as400_creds() -> SessionCredentials:
    creds = SessionCredentials()
    creds.set("cmis", "c", "c")
    creds.set("as400", "u", "p")  # alias implícito de la conexión inline (129)
    return creds


class TestAvailability:
    def test_local_yaml_shows_reason_and_disables_controls(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "8")
                pane = app.query_one(SyncPane)
                avail = str(pane.query_one("#sy-avail", Static).renderable)
                assert "as400_sync" in avail
                for wid in _BUTTONS:
                    assert pane.query_one(wid, Button).disabled is True

        asyncio.run(_run())

    def test_missing_creds_routes_to_credentials_tab(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=_with_sync(config), config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "8")
                pane = app.query_one(SyncPane)
                avail = str(pane.query_one("#sy-avail", Static).renderable)
                assert "[2]" in avail
                assert pane.query_one("#sy-status", Button).disabled is True
                # Cargar credenciales y volver a la pestaña la habilita.
                app.state.creds = _as400_creds()
                await goto(pilot, app, "1")
                await goto(pilot, app, "8")
                assert await _wait_for(
                    pilot, lambda: pane.query_one("#sy-status", Button).disabled is False
                )

        asyncio.run(_run())


class TestRecover:
    def test_apply_gated_by_dry_run_and_confirm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        def fake_recover(
            config: Any, secrets: Any, *, batch_id: Any, apply: bool, on_progress: Any = None
        ) -> Any:
            calls.append({"batch_id": batch_id, "apply": apply})
            return RecoveryResult(recovered=["t1", "t2"], already_present=[], unrecoverable=[])

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            monkeypatch.setattr(sync_pane_module, "sync_recover", fake_recover)
            app = ConsoleApp(config=_with_sync(config), config_path=path)
            async with app.run_test() as pilot:
                app.state.creds = _as400_creds()
                await goto(pilot, app, "8")
                pane = app.query_one(SyncPane)
                apply_btn = pane.query_one("#sy-apply", Button)
                assert apply_btn.disabled is True
                pane.query_one("#sy-batch", Input).value = "batch-1"
                # aplicar sin dry-run previo no hace nada
                pane.apply_recover()
                await pilot.pause()
                assert calls == []
                pane.query_one("#sy-dry", Button).press()
                assert await _wait_for(pilot, lambda: apply_btn.disabled is False)
                assert calls == [{"batch_id": "batch-1", "apply": False}]
                assert "a recuperar=2" in pane.output_text()
                # cambiar el batch_id invalida el dry-run
                pane.query_one("#sy-batch", Input).value = "batch-2"
                assert await _wait_for(pilot, lambda: apply_btn.disabled is True)
                pane.query_one("#sy-batch", Input).value = "batch-1"
                assert await _wait_for(pilot, lambda: apply_btn.disabled is False)
                apply_btn.press()
                assert await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                app.screen.query_one("#yes", Button).press()
                assert await _wait_for(pilot, lambda: len(calls) == 2)
                assert calls[1] == {"batch_id": "batch-1", "apply": True}
                # tras aplicar, hace falta un dry-run nuevo
                assert await _wait_for(pilot, lambda: apply_btn.disabled is True)

        asyncio.run(_run())

    def test_dry_run_without_rows_keeps_apply_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_recover(
            config: Any, secrets: Any, *, batch_id: Any, apply: bool, on_progress: Any = None
        ) -> Any:
            return RecoveryResult(recovered=[], already_present=["x"], unrecoverable=[])

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            monkeypatch.setattr(sync_pane_module, "sync_recover", fake_recover)
            app = ConsoleApp(config=_with_sync(config), config_path=path)
            async with app.run_test() as pilot:
                app.state.creds = _as400_creds()
                await goto(pilot, app, "8")
                pane = app.query_one(SyncPane)
                pane.query_one("#sy-dry", Button).press()
                assert await _wait_for(pilot, lambda: "already_present=1" in pane.output_text())
                assert pane.query_one("#sy-apply", Button).disabled is True

        asyncio.run(_run())

    def test_progress_line_is_shown_live_and_replaced_per_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """144: cada evento pinta UNA línea viva (``⋯ fase k/N``) marshalada
        al hilo UI; la de la misma fase se reemplaza, no se acumula; al
        terminar la operación la línea desaparece."""
        release = threading.Event()

        def fake_recover(
            config: Any, secrets: Any, *, batch_id: Any, apply: bool, on_progress: Any = None
        ) -> Any:
            on_progress(SyncProgress("consultando RVABREP", 0, 100))
            on_progress(SyncProgress("insertando", 0, 100))
            on_progress(SyncProgress("insertando", 50, 100))
            release.wait(5)
            on_progress(SyncProgress("insertando", 100, 100))
            return RecoveryResult(recovered=["t1"], already_present=[], unrecoverable=[])

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            monkeypatch.setattr(sync_pane_module, "sync_recover", fake_recover)
            app = ConsoleApp(config=_with_sync(config), config_path=path)
            async with app.run_test() as pilot:
                app.state.creds = _as400_creds()
                await goto(pilot, app, "8")
                pane = app.query_one(SyncPane)
                assert pane.progress_text() == ""
                pane.query_one("#sy-dry", Button).press()
                assert await _wait_for(pilot, lambda: "insertando 50/100" in pane.progress_text())
                # una sola línea viva: la fase previa y el 0/100 fueron reemplazados
                assert pane.progress_text() == "⋯ insertando 50/100"
                assert "insertando" not in pane.output_text()
                release.set()
                assert await _wait_for(pilot, lambda: "a recuperar=1" in pane.output_text())
                assert await _wait_for(pilot, lambda: pane.progress_text() == "")

        asyncio.run(_run())


class TestResolve:
    def test_prefer_local_confirms_then_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        def fake_resolve(
            config: Any, secrets: Any, *, txn: str, prefer: str, cm_object_id: Any
        ) -> str:
            calls.append({"txn": txn, "prefer": prefer, "cm_object_id": cm_object_id})
            return f"resolved {txn}"

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            monkeypatch.setattr(sync_pane_module, "sync_resolve", fake_resolve)
            app = ConsoleApp(config=_with_sync(config), config_path=path)
            async with app.run_test() as pilot:
                app.state.creds = _as400_creds()
                await goto(pilot, app, "8")
                pane = app.query_one(SyncPane)
                pane.query_one("#sy-txn", Input).value = "0000007"
                assert pane.query_one("#sy-objid", Input).display is False
                pane.query_one("#sy-prefer").value = "local"  # type: ignore[attr-defined]
                assert await _wait_for(pilot, lambda: pane.query_one("#sy-objid", Input).display)
                # sin cm_object_id: no llama y avisa
                pane.query_one("#sy-resolve", Button).press()
                await pilot.pause()
                assert calls == []
                pane.query_one("#sy-objid", Input).value = "cmis-9"
                pane.query_one("#sy-resolve", Button).press()
                assert await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                app.screen.query_one("#yes", Button).press()
                assert await _wait_for(pilot, lambda: len(calls) == 1)
                assert calls[0] == {"txn": "0000007", "prefer": "local", "cm_object_id": "cmis-9"}
                assert await _wait_for(pilot, lambda: "resolved 0000007" in pane.output_text())

        asyncio.run(_run())
