"""138 — editor de conexiones en [2] CREDENCIALES (E1–E8, Textual pilot)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Checkbox, Input, Select, Static

from cmcourier.cli.console.app import ConfirmScreen, ConsoleApp
from cmcourier.cli.console.connection_edit import ConnectionDraft
from cmcourier.cli.console.connection_edit_screen import ConnectionEditScreen
from cmcourier.cli.console.creds_pane import CredsPane
from cmcourier.config.yaml_doc import Edit
from tests.unit.cli.console.conftest import goto, wait_for
from tests.unit.cli.console.test_connection_edit import (
    _META_AS400,
    _RVI,
    _config,
)
from tests.unit.cli.console.test_console_app import _fake_report

pytestmark = pytest.mark.unit


def _notes(app: Any) -> list[str]:
    return [n.message for n in app._notifications]


def _backups(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob("config.yaml.bak-*"))


async def _open_new(pilot: Any, app: ConsoleApp) -> ConnectionEditScreen:
    await goto(pilot, app, "2")
    app.set_focus(None)
    await pilot.press("n")
    assert await wait_for(pilot, lambda: isinstance(app.screen, ConnectionEditScreen))
    screen = app.screen
    assert isinstance(screen, ConnectionEditScreen)
    return screen


async def _press(pilot: Any, app: ConsoleApp, button_id: str) -> None:
    app.query_one(f"#{button_id}", Button).press()
    await pilot.pause()


async def _fill(pilot: Any, screen: ConnectionEditScreen, **values: str) -> None:
    for name, value in values.items():
        wid = "#alias" if name == "alias" else f"#f-{name}"
        screen.query_one(wid, Input).value = value
    await pilot.pause()


async def _save(pilot: Any, app: ConsoleApp) -> None:
    app.screen.query_one("#save", Button).press()
    await pilot.pause()
    await pilot.pause()


class TestNewConnection:
    def test_e1_new_mssql_connection_is_written_and_carded(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI)
            before = path.read_text()
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                screen = await _open_new(pilot, app)
                assert not screen.query_one("#alias", Input).disabled
                screen.query_one("#kind", Select).value = "mssql"
                # `#f-encrypt` sólo existe en mssql: espera el remount real.
                assert await wait_for(pilot, lambda: bool(screen.query("#f-encrypt")))
                assert screen.query_one("#f-port", Input).placeholder == "1433"
                await _fill(pilot, screen, alias="clientes_sql", host="sql01", database="clientes")
                await _save(pilot, app)
                assert await wait_for(
                    pilot, lambda: not isinstance(app.screen, ConnectionEditScreen)
                )
                after = path.read_text()
                block = (
                    "  clientes_sql:\n    kind: mssql\n    host: sql01\n    database: clientes\n"
                )
                assert block in after
                assert after.replace(block, "") == before
                assert _backups(tmp_path)
                assert app.config.connections["clientes_sql"].host == "sql01"
                assert await wait_for(pilot, lambda: bool(app.query("#card-clientes_sql")))
                assert "sin probar" in str(
                    app.query_one("#chiplbl-clientes_sql", Static).renderable
                )
                assert app.state.conn["clientes_sql"].status == "idle"
                assert any("backup en" in m for m in _notes(app))

        asyncio.run(_run())

    def test_e2_validation_errors_keep_the_modal_open(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI)
            before = path.read_text()
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                screen = await _open_new(pilot, app)
                await _fill(pilot, screen, alias="Clientes", host="h")
                await _save(pilot, app)
                assert app.screen is screen
                assert "minúsculas" in str(screen.query_one("#err-alias", Static).renderable)
                await _fill(pilot, screen, alias="cmis")
                await _save(pilot, app)
                assert "reservado" in str(screen.query_one("#err-alias", Static).renderable)
                await _fill(pilot, screen, alias="rvi")
                await _save(pilot, app)
                assert "ya existe" in str(screen.query_one("#err-alias", Static).renderable)
                await _fill(pilot, screen, alias="rvi2", port="70000")
                await _save(pilot, app)
                assert app.screen is screen
                assert str(screen.query_one("#err-alias", Static).renderable) == ""
                assert "1..65535" in str(screen.query_one("#err-port", Static).renderable)
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, ConnectionEditScreen)
            assert path.read_text() == before
            assert not _backups(tmp_path)

        asyncio.run(_run())

    def test_enter_in_an_input_saves(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                screen = await _open_new(pilot, app)
                await _fill(pilot, screen, alias="rvi2", host="as400b")
                screen.query_one("#f-host", Input).focus()
                await pilot.press("enter")
                assert await wait_for(
                    pilot, lambda: not isinstance(app.screen, ConnectionEditScreen)
                )
                assert "  rvi2:\n    kind: as400\n    host: as400b\n" in path.read_text()

        asyncio.run(_run())


class TestEditConnection:
    def test_e3_edit_prefills_from_yaml_and_changes_one_line(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI, metadata_sources=_META_AS400)
            before = path.read_text().splitlines()
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                app.state.record_conn_result("rvi", ok=True, message="ok")
                app.state.set_doctor_report(_fake_report(), group="all")
                await _press(pilot, app, "edit-rvi")
                assert await wait_for(pilot, lambda: isinstance(app.screen, ConnectionEditScreen))
                screen = app.screen
                assert screen.query_one("#alias", Input).disabled
                assert screen.query_one("#alias", Input).value == "rvi"
                assert screen.query_one("#kind", Select).disabled
                assert screen.query_one("#f-host", Input).value == "as400.test"
                assert screen.query_one("#f-port", Input).value == "446"
                driver = screen.query_one("#f-driver", Input)
                assert driver.value == "" and driver.placeholder == "iSeries Access ODBC Driver"
                # el sitio que hoy usa `rvi` viene marcado
                assert screen.query_one("#site-0", Checkbox).value
                screen.query_one("#f-host", Input).value = "as400b"
                await _save(pilot, app)
                assert await wait_for(
                    pilot, lambda: not isinstance(app.screen, ConnectionEditScreen)
                )
                after = path.read_text().splitlines()
                diff = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
                assert diff == [
                    (
                        "  rvi: {kind: as400, host: as400.test, port: 446, database: RVILIB}",
                        "  rvi: {kind: as400, host: as400b, port: 446, database: RVILIB}",
                    )
                ]
                assert app.state.conn["rvi"].status == "idle"
                assert app.config.connections["rvi"].host == "as400b"
                assert "as400b" in str(app.query_one("#title-rvi", Static).renderable)
                assert app.state.doctor_stale_reason == "cambió connections"

        asyncio.run(_run())

    def test_e3_implicit_port_shows_placeholder_only(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(
                tmp_path,
                connections="  rvi: {kind: as400, host: as400.test}\n",
                metadata_sources=_META_AS400,
            )
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                await _press(pilot, app, "edit-rvi")
                assert await wait_for(pilot, lambda: isinstance(app.screen, ConnectionEditScreen))
                port = app.screen.query_one("#f-port", Input)
                assert port.value == "" and port.placeholder == "446"

        asyncio.run(_run())


class TestSites:
    def test_e4_new_alias_takes_over_the_inline_site(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(
                tmp_path, connections=_RVI, inline_indexing=True, metadata_sources=_META_AS400
            )
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                assert app.query("#card-as400")
                screen = await _open_new(pilot, app)
                await _fill(pilot, screen, alias="rvi2", host="as400b")
                indexing = screen.query_one("#site-0", Checkbox)
                assert str(indexing.label) == "indexing.source" and not indexing.value
                indexing.value = True
                await pilot.pause()
                await _save(pilot, app)
                assert await wait_for(
                    pilot, lambda: not isinstance(app.screen, ConnectionEditScreen)
                )
                text = path.read_text()
                assert "    connection: rvi2\n" in text
                assert "      host: as400.test\n" not in text
                assert await wait_for(pilot, lambda: bool(app.query("#card-rvi2")))
                assert not app.query("#card-as400")
                assert "indexing" in str(app.query_one("#sites-rvi2", Static).renderable)

        asyncio.run(_run())

    def test_e4_unchecking_a_site_in_use_is_blocked(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI, metadata_sources=_META_AS400)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                await _press(pilot, app, "edit-rvi")
                assert await wait_for(pilot, lambda: isinstance(app.screen, ConnectionEditScreen))
                box = app.screen.query_one("#site-0", Checkbox)
                assert box.value
                box.value = False
                await pilot.pause()
                await pilot.pause()
                assert box.value
                assert any("soltarlo" in m for m in _notes(app))

        asyncio.run(_run())


class TestDelete:
    def test_e5_delete_in_use_is_refused_and_unused_confirms(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(
                tmp_path,
                connections=_RVI + "  rvi2: {kind: as400, host: b}\n",
                metadata_sources=_META_AS400,
            )
            before = path.read_text()
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                await _press(pilot, app, "del-rvi")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert any(
                    "rvi está en uso por" in m and "metadata.sources[0]" in m for m in _notes(app)
                )
                assert path.read_text() == before

                assert app.query("#card-rvi2")
                await _press(pilot, app, "del-rvi2")
                assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                assert "rvi2" in str(app.screen.query_one(".body", Static).renderable)
                app.screen.query_one("#yes", Button).press()
                assert await wait_for(pilot, lambda: "rvi2" not in path.read_text())
                assert await wait_for(pilot, lambda: not app.query("#card-rvi2"))
                assert "rvi2" not in app.config.connections
                assert _backups(tmp_path)

        asyncio.run(_run())

    def test_i3_session_credentials_of_the_deleted_alias_are_discarded(
        self, tmp_path: Path
    ) -> None:
        """I3: el confirm promete descartarlas; si quedan, `to_secrets` las
        expone y recrear el alias revive la password vieja (prefill setdefault)."""

        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                app.query_one("#user-rvi", Input).value = "usr"
                app.query_one("#pass-rvi", Input).value = "pw-vieja"
                await pilot.pause()
                assert app.state.creds.get("rvi").password == "pw-vieja"

                await _press(pilot, app, "del-rvi")
                assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                app.screen.query_one("#yes", Button).press()
                assert await wait_for(pilot, lambda: "  rvi:" not in path.read_text())
                assert await wait_for(pilot, lambda: "rvi" not in app.state.creds.credentials)
                assert "rvi" not in app.state.creds.to_secrets().credentials
                assert app.state.creds.get("rvi").password == ""

        asyncio.run(_run())

    def test_i3_a_refused_delete_keeps_the_credentials(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI, metadata_sources=_META_AS400)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                app.query_one("#pass-rvi", Input).value = "pw"
                await pilot.pause()
                await _press(pilot, app, "del-rvi")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert app.state.creds.get("rvi").password == "pw"

        asyncio.run(_run())


_ANCHORED = "  rvi: &base\n    kind: as400\n    host: as400.viejo\n    port: 446\n  otro: *base\n"


class TestAnchoredYaml:
    def test_b1_editing_an_alias_notifies_and_leaves_the_yaml_intact(self, tmp_path: Path) -> None:
        """B1: ruamel devuelve el MISMO objeto para `&base` y `*base` — editar
        `rvi` cambiaría también `otro`. Se avisa y no se toca el disco."""

        async def _run() -> None:
            config, path = _config(tmp_path, connections=_ANCHORED)
            before = path.read_text()
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                await _press(pilot, app, "edit-rvi")
                assert await wait_for(pilot, lambda: isinstance(app.screen, ConnectionEditScreen))
                screen = app.screen
                assert isinstance(screen, ConnectionEditScreen)
                await _fill(pilot, screen, host="as400.nuevo")
                await _save(pilot, app)
                assert await wait_for(
                    pilot, lambda: any("No se escribió el YAML" in m for m in _notes(app))
                )
                toast = next(m for m in _notes(app) if "No se escribió el YAML" in m)
                assert "alias YAML" in toast
                assert "Traceback" not in toast and len(toast) < 200
                assert path.read_text() == before
                assert not _backups(tmp_path)
                assert app.config.connections["otro"].host == "as400.viejo"

        asyncio.run(_run())


class TestMoveInline:
    def test_e6_move_inline_to_registry_copies_session_creds(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, inline_indexing=True)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                app.state.creds.set("as400", "usr", "pw")
                assert not app.query("#edit-as400")
                await _press(pilot, app, "move-as400")
                assert await wait_for(pilot, lambda: isinstance(app.screen, ConnectionEditScreen))
                screen = app.screen
                assert screen.query_one("#alias", Input).value == ""
                assert screen.query_one("#kind", Select).value == "as400"
                assert screen.query_one("#f-host", Input).value == "as400.test"
                assert screen.query_one("#f-port", Input).value == "8471"
                assert screen.query_one("#site-0", Checkbox).value
                await _fill(pilot, screen, alias="rvi_main")
                await _save(pilot, app)
                assert await wait_for(
                    pilot, lambda: not isinstance(app.screen, ConnectionEditScreen)
                )
                text = path.read_text()
                assert (
                    "connections:\n  rvi_main:\n    kind: as400\n    host: as400.test\n"
                    "    port: 8471\n    database: RVILIB\n    driver: iSeries Access ODBC Driver\n"
                ) in text
                assert "    connection: rvi_main\n" in text
                assert app.config.indexing.source.connection == "rvi_main"  # type: ignore[union-attr]
                assert app.state.creds.get("rvi_main").username == "usr"
                assert await wait_for(pilot, lambda: bool(app.query("#card-rvi_main")))
                assert app.query_one("#user-rvi_main", Input).value == "usr"

        asyncio.run(_run())


class _ActiveManager:
    active = True
    paused = False
    reauth_pending = False


class TestGuards:
    def test_e7_run_active_disables_buttons_and_n(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI, metadata_sources=_META_AS400)
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _ActiveManager()  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                assert app.query_one("#new-conn", Button).disabled
                assert app.query_one("#edit-rvi", Button).disabled
                assert app.query_one("#del-rvi", Button).disabled
                app.set_focus(None)
                await pilot.press("n")
                await pilot.pause()
                assert not isinstance(app.screen, ConnectionEditScreen)
                assert any("corrida activa" in m for m in _notes(app))

        asyncio.run(_run())

    def test_n_outside_credentials_tab_is_ignored(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "1")
                await pilot.press("n")
                await pilot.pause()
                assert not isinstance(app.screen, ConnectionEditScreen)

        asyncio.run(_run())

    def test_e8_verify_failure_notifies_and_leaves_yaml_intact(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, connections=_RVI, metadata_sources=_META_AS400)
            before = path.read_text()
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                pane = app.query_one(CredsPane)
                # un draft mssql marcado en un sitio as400: plan_write corta antes del disco
                draft = ConnectionDraft("sql", "mssql", {"host": "h", "database": "d"})
                await pane.commit_draft(
                    draft, use_at={("metadata", "sources", 0, "as400_connection")}, editing=None
                )
                await pilot.pause()
                assert any("necesita una conexión as400" in m for m in _notes(app))
                # y una edición que pydantic rechaza: el verify falla, sin backup
                ok = await pane.apply_connection_edits(
                    [Edit(("metadata", "sources", 0, "as400_connection"), "nope")], "x"
                )
                assert not ok
                await pilot.pause()
                # el toast dice qué rechazó pydantic, no vuelca la config entera
                assert any(
                    "unknown connection alias" in m and "'input':" not in m for m in _notes(app)
                )
            assert path.read_text() == before
            assert not _backups(tmp_path)

        asyncio.run(_run())
