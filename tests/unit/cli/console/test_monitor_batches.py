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
    def __init__(
        self,
        *,
        complete: bool = False,
        planned_total: int | None = None,
        window_rate: float | None = 1.0,
        eta_run_s: float | None = None,
        closing: object | None = None,
    ) -> None:
        self._complete = complete
        self._planned_total = planned_total
        self._window_rate = window_rate
        self._eta_run_s = eta_run_s
        self._closing = closing

    def snapshot(self) -> TUISnapshot:
        return TUISnapshot(
            closing=self._closing,  # type: ignore[arg-type]
            pipeline="csv-trigger",
            batch_id="b-mock",
            elapsed_s=12.0,
            throughput_docs_per_s=3.4,
            is_complete=self._complete,
            stages={"S5": {"count": 42}, "S4": {"count": 60}},
            failed_total=2,
            failures_by_type={"503 server": 2},
            s1_filtered=1,
            pool_capacity=4,
            pool_in_use=3,
            docs_processed=45,
            throughput_window_docs_per_s=self._window_rate,
            planned_total=self._planned_total,
            eta_run_s=self._eta_run_s,
        )


class _FakeManager:
    def __init__(self, provider: object | None, *, paused: bool = False) -> None:
        self.provider = provider
        self.active = provider is not None
        self.paused = paused
        self.reauth_pending = False
        self.report = None
        self.exception: BaseException | None = None
        self.worker_cap: int | None = None
        self.aimd_total = 4  # presupuesto del AIMD (sin AIMD = techo del pool)
        self.pool_ceiling = 4
        self.calls: list[str] = []

    @property
    def effective_workers(self) -> int | None:
        if not self.active:
            return None
        cap = self.worker_cap if self.worker_cap is not None else self.pool_ceiling
        return max(1, min(cap, self.aimd_total, self.pool_ceiling))

    def outcome(self) -> str:
        return "cancelled"

    def cancel(self) -> None:
        self.calls.append("cancel")

    def pause(self) -> None:
        self.calls.append("pause")
        self.paused = True

    def resume(self) -> None:
        self.calls.append("resume")
        self.paused = False
        self.reauth_pending = False

    def adjust_workers(self, delta: int) -> int | None:
        # 133: emula el clamp del pipeline (workers=4, sin AIMD)
        if not self.active:
            return None
        self.calls.append(f"adjust{delta:+d}")
        current = self.effective_workers or self.pool_ceiling
        self.worker_cap = max(1, min(current + delta, self.pool_ceiling))
        return self.effective_workers


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

    def test_monitor_shows_closing_phase_instead_of_corriendo(self, tmp_path: Path) -> None:
        # 144: mientras la pasada final del reconciliador corre, el estado
        # es `cerrando · sincronizando AS400 k/N`, no "corriendo".
        from cmcourier.services.worker_pool_stats import ClosingPhase

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            provider = _FakeProvider(closing=ClosingPhase("sincronizando AS400", 50, 120))
            app.run_manager = _FakeManager(provider)  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                app.query_one(MonitorPane).refresh_monitor()
                await pilot.pause()
                header = str(app.query_one("#mon-header", Static).renderable)
                assert "cerrando · sincronizando AS400 50/120" in header
                assert "corriendo" not in header

        asyncio.run(_run())

    def test_monitor_says_corriendo_without_closing_phase(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _FakeManager(_FakeProvider())  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                app.query_one(MonitorPane).refresh_monitor()
                await pilot.pause()
                header = str(app.query_one("#mon-header", Static).renderable)
                assert "corriendo" in header

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

    def test_on_auth_expired_marks_the_cmis_card_as_rejected(self, tmp_path: Path) -> None:
        """Antagonista I6: la tarjeta CMIS seguía "ok · probada hace 3 min"
        después del 401, y `r` reanudaba SIN preguntar porque el status era
        ok. El servidor rechazó la sesión: la tarjeta lo tiene que decir."""

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
                app.on_auth_expired(mgr)  # type: ignore[arg-type]
                await pilot.pause()
                await pilot.pause()
                cmis = app.state.conn["cmis"]
                assert cmis.status == "err"
                assert "401" in cmis.message
                card = str(app.query_one("#msg-cmis", Static).renderable)
                assert "401" in card
                app.set_focus(None)  # el hint enfocó el Input de password
                await goto(pilot, app, "6")
                await pilot.press("r")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmScreen), "reanudar sin reprobar pregunta"
                app.screen.dismiss(False)
                await pilot.pause()

        asyncio.run(_run())

    def test_run_finished_clears_reauth_pending(self, tmp_path: Path) -> None:
        """Antagonista I10: si la corrida termina (cancelada) con el 401
        abierto, la pista de re-auth quedaba viva para la corrida siguiente."""

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider(), paused=True)
            mgr.reauth_pending = True
            app.run_manager = mgr  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                app.on_run_finished(mgr)  # type: ignore[arg-type]
                await pilot.pause()
                assert mgr.reauth_pending is False

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


class TestWorkerCap133:
    def test_minus_lowers_the_cap_and_header_shows_it(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider())
            app.run_manager = mgr  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                await pilot.press("minus")
                await pilot.pause()
                assert mgr.calls == ["adjust-1"]
                header = str(app.query_one("#mon-header", Static).renderable)
                assert "workers 3/3" in header  # en uso / presupuesto efectivo
                assert "techo manual 3" in header

        asyncio.run(_run())

    def test_plus_and_equals_raise_the_cap(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider())
            mgr.worker_cap = 2
            app.run_manager = mgr  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                await pilot.press("plus")
                await pilot.press("equals_sign")
                await pilot.pause()
                assert mgr.calls == ["adjust+1", "adjust+1"]
                assert mgr.worker_cap == 4

        asyncio.run(_run())

    def test_notify_explains_who_holds_the_workers(self, tmp_path: Path) -> None:
        """Antagonista I1: el mensaje dice la CAUSA del efectivo — techo del
        pool, AIMD sosteniendo por debajo del techo manual, o techo manual."""

        def _last_notice(app: ConsoleApp) -> str:
            return list(app._notifications)[-1].message  # noqa: SLF001

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider())
            app.run_manager = mgr  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                await pilot.press("minus")  # cap 3 < techo 4, AIMD 4 → manual
                await pilot.pause()
                assert _last_notice(app) == "workers: 3 (techo manual 3)"
                mgr.aimd_total = 2  # el AIMD bajó por debajo del techo manual
                await pilot.press("minus")  # cap = efectivo(2) - 1 = 1
                await pilot.pause()
                assert _last_notice(app) == "workers: 1 (techo manual 1)"
                mgr.worker_cap = 3
                mgr.aimd_total = 2
                await pilot.press("plus")  # el fake no empuja el AIMD: cap 3, efectivo 2
                await pilot.pause()
                assert _last_notice(app) == "workers: 2 (techo manual 3 · el AIMD sostiene 2)"
                mgr.aimd_total = 4
                await pilot.press("plus")  # cap 4 = techo del pool
                await pilot.pause()
                assert _last_notice(app) == "workers: 4 · techo del pool"

        asyncio.run(_run())

    def test_keys_are_inert_outside_monitor_or_without_run(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            mgr = _FakeManager(_FakeProvider())
            app.run_manager = mgr  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "1")
                await pilot.press("minus")
                await pilot.pause()
                assert mgr.calls == []
                idle = _FakeManager(None)
                app.run_manager = idle  # type: ignore[assignment]
                await goto(pilot, app, "6")
                await pilot.press("minus")
                await pilot.pause()
                assert idle.calls == []

        asyncio.run(_run())

    def test_header_without_manual_cap_shows_only_pool(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _FakeManager(_FakeProvider())  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                header = str(app.query_one("#mon-header", Static).renderable)
                assert "workers 3/4" in header
                assert "techo manual" not in header

        asyncio.run(_run())


class TestWindowEta134:
    def _header(self, tmp_path: Path, provider: _FakeProvider) -> str:
        async def _run() -> str:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _FakeManager(provider)  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "6")
                return str(app.query_one("#mon-header", Static).renderable)

        return asyncio.run(_run())

    def test_header_shows_window_rate_and_eta(self, tmp_path: Path) -> None:
        header = self._header(
            tmp_path, _FakeProvider(planned_total=200, window_rate=1.25, eta_run_s=120.0)
        )
        assert "3.4 docs/s" in header
        assert "1.2 docs/s (60 s)" in header or "1.3 docs/s (60 s)" in header
        assert "ETA 0:02:00 de 200" in header

    def test_header_without_total_says_so(self, tmp_path: Path) -> None:
        header = self._header(tmp_path, _FakeProvider(window_rate=None))
        assert "— docs/s (60 s)" in header
        assert "ETA — (sin total)" in header

    def test_header_complete_has_no_eta(self, tmp_path: Path) -> None:
        header = self._header(tmp_path, _FakeProvider(complete=True, planned_total=200))
        assert "ETA" not in header

    def test_header_complete_drops_window_and_workers(self, tmp_path: Path) -> None:
        """MINOR del antagonista: con la corrida terminada la "tasa de 60 s"
        decae a 0 tick a tick y el pool ya no existe — ninguno informa nada."""
        header = self._header(
            tmp_path, _FakeProvider(complete=True, planned_total=200, window_rate=0.4)
        )
        assert "(60 s)" not in header
        assert "workers" not in header
        assert "3.4 docs/s" in header  # el promedio de la corrida sí queda
