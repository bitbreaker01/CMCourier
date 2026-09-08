"""139 REQ-002..005 — pilot de la pantalla [9] YAML (E4–E9)."""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input, Select, Static

from cmcourier.cli.console.app import ConfirmScreen, ConsoleApp, HelpScreen
from cmcourier.cli.console.config_pane import ConfigPane
from cmcourier.cli.console.creds_pane import CredsPane
from cmcourier.cli.console.schema_form import diff_edits
from cmcourier.cli.console.yaml_pane import YamlPane
from cmcourier.config.loader import load_config
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.yaml_doc import Edit
from tests.unit.cli.console.conftest import goto, wait_for
from tests.unit.cli.console.test_connection_edit import _RVI, _SQL, _config
from tests.unit.cli.console.test_console_app import _fake_report

pytestmark = pytest.mark.unit

_WORKERS_LINE = "  workers: 4  # paralelismo de subida\n"
_META_CSV = "    - kind: csv\n      alias: base\n      csv_path: {csv}\n"


def _yaml(tmp_path: Path, **kw: Any) -> tuple[PipelineConfig, Path]:
    """La config base de la consola + ``cmis.workers`` con comentario inline (E4)."""
    if "metadata_sources" in kw:
        kw["metadata_sources"] = kw["metadata_sources"].format(csv=tmp_path / "triggers.csv")
    _, path = _config(tmp_path, **kw)
    text = path.read_text().replace("  repo_id: repo\n", "  repo_id: repo\n" + _WORKERS_LINE)
    path.write_text(text)
    return load_config(path), path


def _notes(app: Any) -> list[str]:
    return [n.message for n in app._notifications]


def _head(app: Any) -> str:
    return str(app.query_one("#yaml-head", Static).renderable)


def _row(app: Any, path: tuple, widget: type = Input) -> Any:
    pane = app.query_one(YamlPane)
    return app.query_one(f"#{pane.row_id(path)}", widget)


async def _confirm_yes(pilot: Any, app: Any) -> None:
    assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
    app.screen.query_one("#yes", Button).press()
    await pilot.pause()
    await pilot.pause()


class TestE4Write:
    def test_edit_validate_write(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                app.state.set_doctor_report(_fake_report(), group="all")
                app.state.doctor_stale_reason = ""
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                assert "sin cambios" in _head(app)
                assert pane.working == pane.original

                _row(app, ("cmis", "workers")).value = "8"
                await pilot.pause()
                assert pane.working["cmis"]["workers"] == 8
                assert "1 cambios" in _head(app)
                app.set_focus(None)
                await pilot.press("v")
                await pilot.pause()
                assert "válido ✓" in _head(app)

                await pilot.press("w")
                await _confirm_yes(pilot, app)
                assert await wait_for(pilot, lambda: "workers: 8" in path.read_text())
                assert "  workers: 8  # paralelismo de subida\n" in path.read_text()
                assert app.config.cmis.workers == 8
                assert list(tmp_path.glob("config.yaml.bak-*"))
                assert app.state.doctor_stale
                assert pane.working == pane.original
                assert "sin cambios" in _head(app)
                assert any("config.yaml.bak-" in m for m in _notes(app))
                await goto(pilot, app, "3")
                assert app.query_one("#ov-workers", Input).placeholder == "(yaml) 8"
                assert app.query_one(ConfigPane)  # [3] sigue viva

        asyncio.run(_run())

    def test_w_without_changes_notifies(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                await pilot.press("w")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert any("sin cambios" in m for m in _notes(app))

        asyncio.run(_run())

    def test_empty_input_drops_the_key_and_reload_after_3_persist(self, tmp_path: Path) -> None:
        """Vaciar un Input borra la clave (aplica el default); tras escribir
        desde [3] con `w`, [9] limpio se recarga del disco (REQ-004)."""

        async def _run() -> None:
            config, path = _yaml(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                _row(app, ("cmis", "workers")).value = ""
                await pilot.pause()
                assert "workers" not in pane.working["cmis"]
                assert "1 cambios" in _head(app)
                _row(app, ("cmis", "workers")).value = "4"
                await pilot.pause()
                assert pane.working == pane.original

                await goto(pilot, app, "3")
                app.query_one("#ov-workers", Input).value = "6"
                app.query_one(ConfigPane).apply_draft()
                await pilot.pause()
                app.set_focus(None)
                await pilot.press("w")
                await _confirm_yes(pilot, app)
                assert app.config.cmis.workers == 6
                await goto(pilot, app, "9")
                assert await wait_for(pilot, lambda: pane.original["cmis"]["workers"] == 6)
                assert _row(app, ("cmis", "workers")).value == "6"

        asyncio.run(_run())


class TestE5Errors:
    def test_invalid_value_marks_row_and_blocks_write(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path)
            before = path.read_text()
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                pane.box(("cmis",)).collapsed = True
                _row(app, ("cmis", "workers")).value = "abc"
                await pilot.pause()
                assert pane.working["cmis"]["workers"] == "abc"
                app.set_focus(None)
                await pilot.press("v")
                await pilot.pause()
                assert "1 errores" in _head(app)
                err = app.query_one(f"#{pane.err_id(('cmis', 'workers'))}", Static)
                assert "integer" in str(err.renderable)
                assert err.display
                assert pane.box(("cmis",)).collapsed is False

                await pilot.press("w")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert any("1 errores" in m for m in _notes(app))
                assert path.read_text() == before
                assert not list(tmp_path.glob("config.yaml.bak-*"))

                _row(app, ("cmis", "workers")).value = "5"
                await pilot.pause()
                app.set_focus(None)
                await pilot.press("v")
                await pilot.pause()
                assert "válido ✓" in _head(app)
                assert not err.display

        asyncio.run(_run())


class TestE6Lists:
    def test_add_mssql_source_write_and_remove(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path, connections=_SQL, metadata_sources=_META_CSV)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                sources = ("metadata", "sources")
                box = pane.box(sources)
                box.query_one("Select.addkind", Select).value = "mssql"
                box.query_one("Button.addbtn", Button).press()
                await pilot.pause()
                await pilot.pause()
                assert pane.working["metadata"]["sources"][1] == {"kind": "mssql"}
                assert pane.box((*sources, 1)).collapsed is False

                _row(app, (*sources, 1, "alias")).value = "clientes"
                conn = _row(app, (*sources, 1, "connection"), Select)
                conn.value = "clientes_sql"  # explota si el alias no está en las opciones
                _row(app, (*sources, 1, "table")).value = "dbo.clientes"
                await pilot.pause()
                assert pane.working["metadata"]["sources"][1] == {
                    "kind": "mssql",
                    "alias": "clientes",
                    "connection": "clientes_sql",
                    "table": "dbo.clientes",
                }
                app.set_focus(None)
                await pilot.press("v")
                await pilot.pause()
                assert "válido ✓" in _head(app)
                await pilot.press("w")
                await _confirm_yes(pilot, app)
                assert await wait_for(pilot, lambda: "kind: mssql" in path.read_text())
                loaded = load_config(path)
                assert loaded.metadata.sources[1].alias == "clientes"
                assert app.config.metadata.sources[1].alias == "clientes"

                pane.box((*sources, 1)).query_one("Button.delbtn", Button).press()
                await pilot.pause()
                await pilot.pause()
                assert len(pane.working["metadata"]["sources"]) == 1
                assert not any(p[:3] == (*sources, 1) for p, _ in pane.rows.values())
                assert "1 cambios" in _head(app)
                await pilot.press("w")
                await _confirm_yes(pilot, app)
                # ojo: `connections.clientes_sql` también dice `kind: mssql`
                assert await wait_for(pilot, lambda: "alias: clientes" not in path.read_text())
                assert len(load_config(path).metadata.sources) == 1

        asyncio.run(_run())

    def test_dict_section_add_and_remove_key(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                fs = ("metadata", "field_sources")
                box = pane.box(fs)
                box.query_one("Input.addkey", Input).value = "cif"
                box.query_one("Button.addbtn", Button).press()
                await pilot.pause()
                await pilot.pause()
                assert pane.working["metadata"]["field_sources"] == {"cif": {}}
                assert (*fs, "cif", "default_value") in {p for p, _ in pane.rows.values()}
                _row(app, (*fs, "cif", "default_value")).value = "x"
                await pilot.pause()
                assert pane.working["metadata"]["field_sources"]["cif"] == {"default_value": "x"}
                pane.box((*fs, "cif")).query_one("Button.delbtn", Button).press()
                await pilot.pause()
                await pilot.pause()
                assert pane.working["metadata"]["field_sources"] == {}

        asyncio.run(_run())


class TestE7Discriminator:
    def test_changing_trigger_kind_rerenders_the_section(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                paths = {p for p, _ in pane.rows.values()}
                assert ("trigger", "csv_path") in paths
                _row(app, ("trigger", "kind"), Select).value = "local_scan"
                await pilot.pause()
                await pilot.pause()
                assert pane.working["trigger"] == {"kind": "local_scan"}
                paths = {p for p, _ in pane.rows.values()}
                assert ("trigger", "scan_path") in paths
                assert ("trigger", "csv_path") not in paths
                app.set_focus(None)
                await pilot.press("v")
                await pilot.pause()
                assert "1 errores" in _head(app)
                err = app.query_one(f"#{pane.err_id(('trigger', 'scan_path'))}", Static)
                assert "required" in str(err.renderable).lower()

        asyncio.run(_run())


class TestI4QuotedScalars:
    def test_typing_back_the_quoted_original_leaves_no_changes(self, tmp_path: Path) -> None:
        """I4: `repo_id: "repo"` llega como `DoubleQuotedScalarString`; `dirty`
        (==) lo veía igual y `diff_edits` (por tipo) distinto — la cabecera
        marcaba "1 cambios" y `w` contestaba "sin cambios"."""

        async def _run() -> None:
            _, path = _yaml(tmp_path)
            path.write_text(path.read_text().replace("  repo_id: repo\n", '  repo_id: "repo"\n'))
            config = load_config(path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                assert type(pane.original["cmis"]["repo_id"]) is str
                row = _row(app, ("cmis", "repo_id"))
                row.value = "otro"
                await pilot.pause()
                assert "1 cambios" in _head(app)
                row.value = "repo"
                await pilot.pause()
                assert pane.dirty is False
                assert diff_edits(pane.original, pane.working) == []
                assert "sin cambios" in _head(app)

        asyncio.run(_run())


class TestI5Discard:
    def test_u_restores_the_form_after_changing_a_discriminator(self, tmp_path: Path) -> None:
        """I5: cambiar el kind destruye el bloque y no había forma de volver."""

        async def _run() -> None:
            config, path = _yaml(tmp_path)
            before = path.read_text()
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                original = deepcopy(pane.original)
                _row(app, ("trigger", "kind"), Select).value = "local_scan"
                await pilot.pause()
                await pilot.pause()
                assert pane.working["trigger"] == {"kind": "local_scan"}
                assert pane.dirty

                app.set_focus(None)
                await pilot.press("u")
                assert await wait_for(pilot, lambda: not pane.dirty)
                assert pane.working == original
                assert _row(app, ("trigger", "csv_path")).value
                assert "sin cambios" in _head(app)
                assert any("descartados" in m.lower() for m in _notes(app))
                assert path.read_text() == before

        asyncio.run(_run())

    def test_u_outside_the_yaml_tab_is_ignored(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                _row(app, ("cmis", "workers")).value = "8"
                await pilot.pause()
                await goto(pilot, app, "1")
                await pilot.press("u")
                await pilot.pause()
                assert pane.dirty

        asyncio.run(_run())

    def test_u_is_listed_in_the_help(self, tmp_path: Path) -> None:
        line = next(ln for ln in HelpScreen.HELP.splitlines() if ln.strip().startswith("[9]"))
        assert "u descartar" in line


class TestE8RunActive:
    def test_w_with_run_active_does_not_write(self, tmp_path: Path) -> None:
        from tests.unit.cli.console.test_connection_edit_screen import _ActiveManager

        async def _run() -> None:
            config, path = _yaml(tmp_path)
            before = path.read_text()
            app = ConsoleApp(config=config, config_path=path)
            app.run_manager = _ActiveManager()  # type: ignore[assignment]
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                _row(app, ("cmis", "workers")).value = "8"
                await pilot.pause()
                app.set_focus(None)
                await pilot.press("w")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert any("corrida activa" in m for m in _notes(app))
                assert path.read_text() == before

        asyncio.run(_run())


class TestE9Reload:
    def test_new_connection_from_2_shows_up_in_selects(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path, connections=_RVI, inline_indexing=True)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                conn = _row(app, ("indexing", "source", "connection"), Select)
                assert conn.value == pane.INLINE
                with pytest.raises(Exception, match="rvi2"):
                    conn.value = "rvi2"
                await goto(pilot, app, "2")
                creds = app.query_one(CredsPane)
                edit = Edit(("connections", "rvi2"), {"kind": "as400", "host": "as400.two"})
                assert await creds.apply_connection_edits([edit], "ok")
                await goto(pilot, app, "9")
                assert await wait_for(pilot, lambda: "rvi2" in pane.original["connections"])
                conn = _row(app, ("indexing", "source", "connection"), Select)
                conn.value = "rvi2"
                await pilot.pause()
                assert pane.working["indexing"]["source"]["connection"] == "rvi2"

        asyncio.run(_run())

    def test_pending_edits_survive_and_header_warns_about_disk(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "9")
                pane = app.query_one(YamlPane)
                _row(app, ("cmis", "workers")).value = "8"
                await pilot.pause()
                path.write_text(path.read_text() + "# tocado afuera\n")
                stamp = path.stat().st_mtime + 10
                os.utime(path, (stamp, stamp))
                await goto(pilot, app, "2")
                await goto(pilot, app, "9")
                await pilot.pause()
                assert pane.working["cmis"]["workers"] == 8
                assert "# tocado afuera" not in str(pane.original)
                assert "el archivo cambió en disco" in _head(app)

        asyncio.run(_run())


class TestShell:
    def test_help_and_fkey(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _yaml(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "f9")
                assert app.query_one(YamlPane).rows
                await pilot.press("question_mark")
                assert await wait_for(pilot, lambda: isinstance(app.screen, HelpScreen))
                # 141: la cabecera pasó a F1–F10 al sumarse la pestaña [0].
                assert "[9]" in HelpScreen.HELP and "F1–F10" in HelpScreen.HELP

        asyncio.run(_run())
