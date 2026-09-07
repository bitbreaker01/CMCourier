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
    # 131: la config CSV de estos tests no usa conexiones del registro —
    # sólo CMIS entra en el guard de credenciales.
    app.state.record_conn_result("cmis", ok=True, message="ok")
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

    def test_pipeline_selector_applies_trigger_override(self, tmp_path: Path) -> None:
        """127 / E2: elegir rvabrep aplica el override y muestra sus filas;
        volver a csv con el path del YAML lo deja en None."""

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _FakeManager()  # type: ignore[assignment]
            async with app.run_test() as pilot:
                _ready_state(app)
                await goto(pilot, app, "5")
                pane = app.query_one(RunPane)
                assert str(pane.query_one("#run-kind", Select).value) == "csv"
                assert pane.query_one("#kind-csv").display is True
                assert pane.query_one("#kind-rvabrep").display is False
                pane.query_one("#run-kind", Select).value = "rvabrep"
                await pilot.pause()
                pane.query_one("#run-rv-systems", Input).value = "1, 7"
                await pilot.pause()
                assert pane.query_one("#kind-rvabrep").display is True
                assert pane.query_one("#kind-csv").display is False
                trig = app.state.overrides.trigger
                assert trig is not None and trig.kind == "rvabrep"
                assert list(trig.filters.systems) == ["1", "7"]
                assert app.state.doctor_stale
                summary = str(pane.query_one("#run-summary").renderable)
                assert "pipeline      rvabrep (override)" in summary
                pane.query_one("#run-kind", Select).value = "csv"
                await pilot.pause()
                assert app.state.overrides.trigger is None

        asyncio.run(_run())

    def test_invalid_scan_path_blocks_launch(self, tmp_path: Path) -> None:
        """127 / E3: carpeta inexistente → no se aplica y no se lanza."""

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            fake = _FakeManager()
            app.run_manager = fake  # type: ignore[assignment]
            async with app.run_test() as pilot:
                _ready_state(app)
                await goto(pilot, app, "5")
                pane = app.query_one(RunPane)
                pane.query_one("#run-kind", Select).value = "local_scan"
                await pilot.pause()
                pane.query_one("#run-scan-path", Input).value = str(tmp_path / "nope")
                await pilot.pause()
                assert app.state.overrides.trigger is None
                assert "scan_path" in str(pane.query_one("#run-guard").renderable)
                assert pane.build_spec() is None
                await pilot.press("r")
                await pilot.pause()
                assert fake.launched == []

        asyncio.run(_run())

    def test_single_doc_launch_uses_effective_trigger(self, tmp_path: Path) -> None:
        """127 / E4."""

        async def _run() -> None:
            from cmcourier.cli.console.overrides import apply_overrides

            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            fake = _FakeManager()
            app.run_manager = fake  # type: ignore[assignment]
            async with app.run_test() as pilot:
                _ready_state(app)
                await goto(pilot, app, "5")
                pane = app.query_one(RunPane)
                pane.query_one("#run-kind", Select).value = "single_doc"
                await pilot.pause()
                assert pane.query_one("#kind-single").display is True
                assert pane.query_one("#row-mode").display is False
                pane.query_one("#run-shortname", Input).value = "CLI01"
                pane.query_one("#run-system", Input).value = "1"
                _ready_state(app)  # el cambio de pipeline dejó el doctor stale
                await pilot.press("r")
                assert await _wait_for(pilot, lambda: len(fake.launched) == 1)
                spec = fake.launched[0]
                assert spec.shortname == "CLI01" and spec.system_id == "1"
                eff = apply_overrides(app.config, app.state.overrides)
                assert eff.trigger.kind == "single_doc"

        asyncio.run(_run())

    def test_discard_overrides_keeps_pipeline(self, tmp_path: Path) -> None:
        """127 / E5 + REQ-001: [3] no pisa el pipeline elegido en [5] ni
        lo marca como borrador sin guardar."""

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _FakeManager()  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "5")
                app.query_one(RunPane).query_one("#run-kind", Select).value = "rvabrep"
                await pilot.pause()
                assert app.state.overrides.trigger is not None
                await goto(pilot, app, "3")
                assert not app.draft_dirty()
                app.query_one(ConfigPane).reset_draft()
                await pilot.pause()
                assert app.state.overrides.trigger is not None

        asyncio.run(_run())

    def test_doctor_runs_on_effective_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """127 / REQ-003."""

        async def _run() -> None:
            import cmcourier.cli.console.app as app_module

            seen: list[str] = []

            def fake_doctor(cfg, secrets, selected):  # noqa: ANN001
                seen.append(cfg.trigger.kind)
                return DoctorReport(results=(), elapsed_seconds=0.0)

            monkeypatch.setattr(app_module, "run_doctor", fake_doctor)
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "5")
                app.query_one(RunPane).query_one("#run-kind", Select).value = "rvabrep"
                await pilot.pause()
                await goto(pilot, app, "4")
                await pilot.press("d")
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert await _wait_for(pilot, lambda: seen == ["rvabrep"])

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
