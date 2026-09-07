"""Tests pilot del monitor y batches (125)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from cmcourier.cli.console.app import ConfirmScreen, ConsoleApp
from cmcourier.cli.console.batches_pane import BatchesPane
from cmcourier.cli.console.monitor_pane import MonitorPane
from cmcourier.tui.data_provider import TUISnapshot
from tests.unit.cli.console.conftest import goto
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = pytest.mark.unit


class _FakeProvider:
    def __init__(self, *, complete: bool = False) -> None:
        self._complete = complete

    def snapshot(self) -> TUISnapshot:
        return TUISnapshot(
            pipeline="csv-trigger",
            batch_id="b-mock",
            elapsed_s=12.0,
            throughput_docs_per_s=3.4,
            is_complete=self._complete,
            stages={"S5": {"count": 42}, "S4": {"count": 60}},
            failed_total=2,
            failures_by_type={"503 server": 2},
            s1_filtered=1,
        )


class _FakeManager:
    def __init__(self, provider: object | None, *, paused: bool = False) -> None:
        self.provider = provider
        self.active = provider is not None
        self.paused = paused
        self.reauth_pending = False
        self.calls: list[str] = []

    def cancel(self) -> None:
        self.calls.append("cancel")

    def pause(self) -> None:
        self.calls.append("pause")
        self.paused = True

    def resume(self) -> None:
        self.calls.append("resume")
        self.paused = False
        self.reauth_pending = False


def _seed_batch(tmp_path: Path, path: Path, *, fail: bool = True) -> str:
    """Crea un batch real en el tracking del config para BATCHES."""
    from datetime import datetime

    from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore
    from cmcourier.config.loader import load_config
    from cmcourier.domain.models import MigrationRecord, StageStatus

    cfg = load_config(path)
    store = SQLiteTrackingStore(cfg.tracking.db_path)
    batch_id = store.start_batch(total_records=2)
    store.record_batch_audit(
        batch_id,
        operator="gmaker",
        station="MIGRA-01",
        pipeline_kind="csv",
        environment="staging",
        config_hash="abc123",
        overrides_json="{}",
        doctor_verdict="aprobado",
    )

    def _rec(txn: str) -> MigrationRecord:
        return MigrationRecord(
            trigger_shortname="S1",
            trigger_cif="1",
            trigger_system_id="1",
            rvabrep_txn_num=txn,
            rvabrep_file_name=f"{txn}.001",
            batch_id=batch_id,
            status=StageStatus.S1_PENDING,
            created_at=datetime.now(),
        )

    store.mark_stage_pending(_rec("0000001"), StageStatus.S1_PENDING)
    store.mark_stage_done("0000001", batch_id, StageStatus.S5_DONE, cm_object_id="cm-1")
    if fail:
        store.mark_stage_pending(_rec("0000002"), StageStatus.S1_PENDING)
        store.mark_stage_failed("0000002", batch_id, StageStatus.S5_FAILED, "503")
    store.set_batch_outcome(batch_id, "completed")
    store.flush()
    store.close()
    return batch_id


class TestMonitor:
    def test_monitor_renders_progress_when_active(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _FakeManager(_FakeProvider())  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                app.query_one(MonitorPane).refresh_monitor()
                await pilot.pause()
                header = str(app.query_one("#mon-header", Static).renderable)
                assert "subidos 42" in header
                assert "503 server" in header

        asyncio.run(_run())

    def test_x_cancels_with_confirm_and_stays(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _FakeManager(_FakeProvider())  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                await pilot.press("x")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmScreen)

        asyncio.run(_run())


class TestPauseResume132:
    def test_p_pauses_after_confirm(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider())
            app.run_manager = mgr  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                await pilot.press("p")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmScreen)
                app.screen.dismiss(True)
                await pilot.pause()
                assert mgr.calls == ["pause"]
                header = str(app.query_one("#mon-header", Static).renderable)
                assert "PAUSADA" in header
                assert "⏸ pausada" in str(app.query_one("#top-status", Static).renderable)

        asyncio.run(_run())

    def test_r_on_monitor_resumes(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider(), paused=True)
            app.run_manager = mgr  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                await pilot.press("r")
                await pilot.pause()
                assert mgr.calls == ["resume"]
                assert not isinstance(app.screen, ConfirmScreen)

        asyncio.run(_run())

    def test_r_with_reauth_pending_and_untested_cmis_asks_first(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider(), paused=True)
            mgr.reauth_pending = True
            app.run_manager = mgr  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                await pilot.press("r")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmScreen)
                assert mgr.calls == []
                app.screen.dismiss(True)
                await pilot.pause()
                assert mgr.calls == ["resume"]

        asyncio.run(_run())

    def test_r_with_reauth_pending_and_tested_cmis_resumes_directly(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider(), paused=True)
            mgr.reauth_pending = True
            app.run_manager = mgr  # type: ignore[assignment]
            app.state.creds.set("cmis", "u", "p")
            app.state.record_conn_result("cmis", ok=True, message="ok")
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                await pilot.press("r")
                await pilot.pause()
                assert mgr.calls == ["resume"]

        asyncio.run(_run())

    def test_on_auth_expired_jumps_to_creds_with_hint(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider(), paused=True)
            mgr.reauth_pending = True
            app.run_manager = mgr  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                app.on_auth_expired(mgr)  # type: ignore[arg-type]
                await pilot.pause()
                await pilot.pause()
                assert app.query_one(TabbedContent).active == "credenciales"
                hint = str(app.query_one("#creds-hint", Static).renderable)
                assert "PAUSADA" in hint and "cmis" in hint.lower()
                header_call = app.query_one(MonitorPane)
                header_call.refresh_monitor()
                header = str(app.query_one("#mon-header", Static).renderable)
                assert "esperando credenciales CMIS" in header

        asyncio.run(_run())


class TestBatches:
    def test_lists_batches_with_audit(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            _seed_batch(tmp_path, path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "7")
                app.query_one(BatchesPane).reload()
                await pilot.pause()
                table = app.query_one("#bt-table", DataTable)
                assert table.row_count == 1
                # la columna 'por' trae el operador auditado
                row = table.get_row_at(0)
                assert "gmaker" in row

        asyncio.run(_run())

    def test_retry_on_failed_batch_opens_confirm(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            _seed_batch(tmp_path, path, fail=True)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "7")
                pane = app.query_one(BatchesPane)
                pane.reload()
                await pilot.pause()
                app.query_one("#bt-table", DataTable).move_cursor(row=0)
                pane.retry_selected()
                await pilot.pause()
                assert isinstance(app.screen, ConfirmScreen)

        asyncio.run(_run())

    def test_clean_batch_is_not_resumable(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            _seed_batch(tmp_path, path, fail=False)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "7")
                pane = app.query_one(BatchesPane)
                pane.reload()
                await pilot.pause()
                info = pane._rows[0]  # noqa: SLF001
                assert pane.is_resumable(info) is False

        asyncio.run(_run())
