"""Tests pilot de la ConsoleApp (123) — shell, navegación, credenciales, doctor."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, Select, Static, TabbedContent

import cmcourier.cli.console.app as app_module
from cmcourier.cli.console.app import ConfirmScreen, ConsoleApp
from cmcourier.cli.console.doctor_pane import DoctorPane
from cmcourier.cli.doctor import CHECK_NAMES, CheckResult, CheckStatus, DoctorReport
from cmcourier.config.loader import load_config
from tests.unit.cli.console.conftest import goto, wait_for

pytestmark = pytest.mark.unit

_TESTS_ROOT = Path(__file__).parent.parent.parent.parent
_PIPE = _TESTS_ROOT / "fixtures" / "pipeline"
_SVC = _TESTS_ROOT / "fixtures" / "services"
_ASM = _TESTS_ROOT / "fixtures" / "assembly"


def _make_config(tmp_path: Path, *, environment: str = "staging"):
    triggers = tmp_path / "triggers.csv"
    triggers.write_text("ShortName,CIF,SystemID\nTESTCLIENT01,123456,1\n")
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"""\
environment: {environment}
trigger:
  csv_path: {triggers}
indexing:
  source:
    kind: csv
    csv_path: {_PIPE / "rvabrep.csv"}
  columns:
    shortname_column: shortname
    system_id_column: system_id
    delete_code_column: delete_code
    txn_num_column: txn_num
    index2_column: index2
    index3_column: index3
    index4_column: index4
    index5_column: index5
    index6_column: index6
    index7_column: index7
    image_type_column: image_type
    image_path_column: image_path
    file_name_column: file_name
    creation_date_column: creation_date
    last_view_date_column: last_view_date
    total_pages_column: total_pages
mapping:
  csv_path: {_SVC / "modelo_documental.csv"}
metadata:
  field_aliases: {{}}
  field_sources: {{}}
assembly:
  source_root: {_ASM}
  temp_dir: {tmp_path / "stg"}
cmis:
  base_url: http://cm.test/cmis
  repo_id: repo
tracking:
  db_path: {tmp_path / "tracking.db"}
observability:
  log_dir: {tmp_path / "logs"}
""")
    return load_config(yaml_path), yaml_path


def _fake_report() -> DoctorReport:
    return DoctorReport(
        results=(
            CheckResult(name="a", status=CheckStatus.PASS, message="bien"),
            CheckResult(name="b", status=CheckStatus.WARN, message="ojo", details={"k": "v"}),
        ),
        elapsed_seconds=0.2,
    )


class TestShell:
    def test_number_keys_switch_tabs(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                assert app.query_one(TabbedContent).active == "inicio"
                await goto(pilot, app, "2")
                assert app.query_one(TabbedContent).active == "credenciales"
                await goto(pilot, app, "4")
                assert app.query_one(TabbedContent).active == "doctor"

        asyncio.run(_run())

    def test_fkeys_work_even_with_input_focus(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                app.query_one("#user-cmis", Input).focus()
                await pilot.press("4")  # el Input se la traga — a propósito, SIN goto
                await pilot.pause()
                assert app.query_one(TabbedContent).active == "credenciales"
                await goto(pilot, app, "f4")  # priority=True la rescata

        asyncio.run(_run())

    def test_prd_environment_shows_red_badge(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path, environment="prd")
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test():
                badge = app.query_one("#env-badge", Static)
                assert "PRODUCCIÓN" in str(badge.renderable)
                assert badge.has_class("prd")

        asyncio.run(_run())

    def test_quit_asks_confirmation(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await pilot.press("q")
                assert isinstance(app.screen, ConfirmScreen)
                await pilot.press("escape")  # opción segura
                assert not isinstance(app.screen, ConfirmScreen)

        asyncio.run(_run())


class TestCredsFlow:
    def test_edit_password_invalidates_tested_connection(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                app.state.record_conn_result("cmis", ok=True, message="ok")
                await goto(pilot, app, "2")
                inp = app.query_one("#pass-cmis", Input)
                inp.focus()
                await pilot.press("x")
                assert app.state.conn["cmis"].status == "idle"

        asyncio.run(_run())

    def test_test_button_runs_check_and_marks_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            monkeypatch.setattr(
                app_module,
                "run_single_check",
                lambda which, console: (
                    CheckResult(name=which, status=CheckStatus.PASS, message="conectado"),
                    42.0,
                ),
            )
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                app.query_one("#user-cmis", Input).value = "admin"
                app.query_one("#pass-cmis", Input).value = "admin"
                app.query_one("#test-cmis").focus()
                await pilot.press("enter")
                await pilot.pause()  # deja que el handler registre el worker
                await app.workers.wait_for_complete()
                assert await wait_for(pilot, lambda: app.state.conn["cmis"].status == "ok")

        asyncio.run(_run())


class TestDoctorFlow:
    def test_d_runs_doctor_and_summarizes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            monkeypatch.setattr(
                app_module, "run_doctor", lambda cfg, secrets, selected: _fake_report()
            )
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "4")
                await pilot.press("d")
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert await wait_for(pilot, lambda: app.state.doctor_verdict() == "aprobado")
                summary = str(app.query_one("#doc-summary", Static).renderable)
                assert "1 ok" in summary and "1 warn" in summary

        asyncio.run(_run())

    def test_credential_change_stales_doctor_banner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            monkeypatch.setattr(
                app_module, "run_doctor", lambda cfg, secrets, selected: _fake_report()
            )
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "4")
                await pilot.press("d")
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert await wait_for(pilot, lambda: app.state.doctor_report is not None)
                app.state.creds.cmis_password = "old"
                await goto(pilot, app, "2")
                inp = app.query_one("#pass-cmis", Input)
                inp.focus()
                await pilot.press("y")
                assert await wait_for(pilot, lambda: app.state.doctor_stale)
                await goto(pilot, app, "f4")
                app.query_one(DoctorPane).render_results()
                await pilot.pause()
                stale = app.query_one("#doc-stale", Static)
                assert stale.display

        asyncio.run(_run())

    def test_selector_visible_and_lists_individual_checks(self, tmp_path: Path) -> None:
        """126 / E3: el Select tiene ancho real (antes 1fr dentro de un
        Horizontal → ancho 1, invisible), no repite `all` y ofrece los
        checks individuales además de los grupos."""

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "4")
                await pilot.pause()
                sel = app.query_one("#doc-group", Select)
                assert sel.region.width > 10
                values = [str(v) for _, v in sel._options]  # noqa: SLF001
                assert len(values) == len(set(values))
                assert "all" in values
                assert "connections" in values
                assert "log_dir_writable" in values
                assert set(CHECK_NAMES) <= set(values)

        asyncio.run(_run())

    def test_help_lists_doctor_checks(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await pilot.press("question_mark")
                await pilot.pause()
                text = str(app.screen.query_one(Static).renderable)
                for name in CHECK_NAMES:
                    assert name in text

        asyncio.run(_run())


class TestMarkupSafety:
    """Regresión: los mensajes de error de CMIS/AS400 traen JSON con
    corchetes que Textual leería como markup y rompería el render
    (cazado por el smoke-test real contra Alfresco)."""

    def test_error_body_with_brackets_does_not_crash_render(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                nasty = '{"status": {"code": 500}, "message": "rawPassword [mandatory]"}'
                app.state.record_conn_result("cmis", ok=False, message=nasty)
                from cmcourier.cli.console.creds_pane import CredsPane

                app.query_one(CredsPane)._render_conn("cmis")  # noqa: SLF001
                await pilot.pause()  # el render no debe explotar
                from textual.widgets import Static

                assert nasty in str(app.query_one("#msg-cmis", Static).renderable)

        asyncio.run(_run())
