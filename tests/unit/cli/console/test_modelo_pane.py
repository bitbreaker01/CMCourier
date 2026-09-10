"""145 REQ-006 — pilot de la pestaña ``M·MODELO``.

El manifest de tipos CM desde la consola: ver las clases y sus
propiedades, decidir ``usar``/``omitir``, fijar carpeta, firmar el tipo, y
—con credenciales— descubrir / comparar / actualizar contra el servidor.
Todo lo que se toca se guarda al JSON en el acto.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, DataTable, Input

import cmcourier.cli.console.modelo_pane as modelo_module
from cmcourier.adapters.manifest.json_store import JsonTypeManifestStore
from cmcourier.cli.console.app import ConsoleApp
from cmcourier.cli.console.modelo_pane import ModeloPane
from cmcourier.config.loader import load_config
from cmcourier.config.schema import PipelineConfig
from cmcourier.domain.cm_types import DECISION_OMIT, DECISION_USE
from cmcourier.services.type_manifest import build_manifest, mark_reviewed
from tests.unit.cli.console.conftest import goto, wait_for
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = pytest.mark.unit

_FIELD_SOURCES = """  field_sources:
    BAC_CIF:
      sources:
        - source_type: trigger
          lookup_value_column: cif
"""


# ---------------------------------------------------------------------------
# Dobles y fixtures
# ---------------------------------------------------------------------------


def _prop(
    prop_id: str,
    *,
    required: bool = False,
    default: str | None = None,
    max_length: int | None = None,
) -> dict[str, Any]:
    return {
        "id": prop_id,
        "displayName": prop_id.split(".")[-1],
        "propertyType": "string",
        "cardinality": "single",
        "updatability": "readwrite",
        "required": required,
        "maxLength": max_length,
        "defaultValue": default,
        "inherited": False,
    }


def _type_node(id_corto: str, props: list[dict[str, Any]]) -> dict[str, Any]:
    defs = {p["id"]: p for p in props}
    defs["clbNonGroup.BAC_ID_Corto"] = _prop(
        "clbNonGroup.BAC_ID_Corto", required=True, default=id_corto
    )
    return {
        "type": {
            "id": f"clbNonGroup.{id_corto}",
            "localName": id_corto,
            "displayName": f"{id_corto} - Clase de prueba",
            "creatable": True,
            "baseId": "cmis:document",
            "propertyDefinitions": defs,
        },
        "children": [],
    }


def _nodes(extra: bool = False) -> list[dict[str, Any]]:
    dc01 = [_prop("clbNonGroup.BAC_CIF", required=True, max_length=9)]
    if extra:
        dc01.append(_prop("clbNonGroup.BAC_Nueva", required=True))
    return [_type_node("DC01", dc01), _type_node("DC02", [_prop("clbNonGroup.BAC_Fecha")])]


class _FakeUploader:
    """Uploader mínimo: lo que ``TypeDiscoveryService`` le pide, sin red."""

    def __init__(
        self, nodes: list[dict[str, Any]] | None = None, *, gate: threading.Event | None = None
    ) -> None:
        self._nodes = nodes or _nodes()
        self._gate = gate
        self.verified: list[str] = []

    def get_type_descendants(
        self, include_property_definitions: bool = True
    ) -> list[dict[str, Any]]:
        # El gate congela el worker en la fase "descargando tipos" para que
        # el test pueda leer la línea VIVA de progreso sin carrera.
        if self._gate is not None:
            self._gate.wait(5)
        return self._nodes

    def verify_folder_exists(self, folder_path: str) -> bool:
        self.verified.append(folder_path)
        return True


def _seed(path: Path, nodes: list[dict[str, Any]] | None = None) -> JsonTypeManifestStore:
    store = JsonTypeManifestStore(path)
    store.save(
        build_manifest(
            nodes or _nodes(),
            service_url="http://cm.test/cmis",
            repository_id="repo",
            discovered_at="2026-01-01T00:00:00+00:00",
        )
    )
    return store


def _config(tmp_path: Path, *, manifest: Path | None) -> tuple[PipelineConfig, Path]:
    """La config de la consola en modo manifest (145 REQ-001)."""
    _, path = _make_config(tmp_path)
    if manifest is None:
        return load_config(path), path
    rvi_cm = tmp_path / "MapeoRVI_CM.csv"
    rvi_cm.write_text("IDSistema,IDRVI,IDCM\n,FB01,DC01\n", encoding="utf-8")
    text, replaced = re.subn(
        r"mapping:\n  csv_path: .*\n",
        f"mapping:\n  rvi_cm_csv_path: {rvi_cm}\n  type_manifest_path: {manifest}\n",
        path.read_text(),
    )
    assert replaced == 1
    text = text.replace("  field_sources: {}\n", _FIELD_SOURCES)
    path.write_text(text)
    return load_config(path), path


def _pane(app: ConsoleApp) -> ModeloPane:
    return app.query_one(ModeloPane)


def _with_creds(app: ConsoleApp) -> None:
    app.state.creds.set("cmis", "admin", "admin")
    app.state.record_conn_result("cmis", ok=True, message="ok")


def _stored(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["types"]


# ---------------------------------------------------------------------------
# Cableado del shell
# ---------------------------------------------------------------------------


class TestShellWiring:
    def test_tab_is_last_and_has_keys(self) -> None:
        from cmcourier.cli.console.app import _TABS

        assert _TABS[-1] == "modelo"
        keys = {binding.key for binding in ConsoleApp.BINDINGS}  # type: ignore[attr-defined]
        assert "m" in keys
        assert "f11" in keys

    def test_help_mentions_the_new_screen(self) -> None:
        from cmcourier.cli.console.app import HelpScreen

        assert "[m]" in HelpScreen.HELP.lower()

    def test_key_m_switches_to_the_tab(self, tmp_path: Path) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest)
            config, path = _config(tmp_path, manifest=manifest)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "m")
                await goto(pilot, app, "1")
                await goto(pilot, app, "f11")

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Lectura del manifest
# ---------------------------------------------------------------------------


class TestTables:
    def test_tables_populate_from_the_manifest(self, tmp_path: Path) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest)
            config, path = _config(tmp_path, manifest=manifest)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "m")
                pane = _pane(app)
                types = pane.query_one("#md-types", DataTable)
                assert types.row_count == 2
                assert pane.selected_code() == "DC01"
                props = pane.query_one("#md-props", DataTable)
                # DC01 tiene BAC_CIF + BAC_ID_Corto (las dos escribibles)
                assert props.row_count == 2
                assert pane.query_one("#md-folder", Input).value == "/$type/DC01"

        asyncio.run(_run())

    def test_without_manifest_path_hints_and_disables_the_buttons(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, manifest=None)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "m")
                pane = _pane(app)
                assert "mapping.type_manifest_path" in pane.log_text()
                for wid in ("#md-discover", "#md-diff", "#md-update", "#md-check"):
                    assert pane.query_one(wid, Button).disabled is True

        asyncio.run(_run())

    def test_missing_file_leaves_only_discover_enabled(self, tmp_path: Path) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest)
            config, path = _config(tmp_path, manifest=manifest)
            manifest.unlink()
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "m")
                pane = _pane(app)
                assert pane.query_one("#md-types", DataTable).row_count == 0
                assert pane.query_one("#md-discover", Button).disabled is False
                assert pane.query_one("#md-check", Button).disabled is True

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Edición (offline, se guarda en cada tecla)
# ---------------------------------------------------------------------------


class TestEditing:
    def test_space_toggles_the_decision_and_persists(self, tmp_path: Path) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest)
            config, path = _config(tmp_path, manifest=manifest)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "m")
                pane = _pane(app)
                props = pane.query_one("#md-props", DataTable)
                props.focus()
                await pilot.pause()
                prop_id = pane.selected_prop_id()
                assert prop_id is not None
                before = _stored(manifest)["DC01"]["decisions"][prop_id]
                await pilot.press("space")
                assert await wait_for(
                    pilot, lambda: _stored(manifest)["DC01"]["decisions"][prop_id] != before
                )
                after = _stored(manifest)["DC01"]["decisions"][prop_id]
                assert {before, after} == {DECISION_USE, DECISION_OMIT}

        asyncio.run(_run())

    def test_enter_marks_the_type_reviewed_and_persists(self, tmp_path: Path) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest)
            config, path = _config(tmp_path, manifest=manifest)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "m")
                pane = _pane(app)
                assert _stored(manifest)["DC01"]["reviewed"] is False
                types = pane.query_one("#md-types", DataTable)
                types.focus()
                await pilot.pause()
                await pilot.press("enter")
                assert await wait_for(pilot, lambda: _stored(manifest)["DC01"]["reviewed"] is True)

        asyncio.run(_run())

    def test_folder_edit_persists_as_manual(self, tmp_path: Path) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest)
            config, path = _config(tmp_path, manifest=manifest)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "m")
                pane = _pane(app)
                folder = pane.query_one("#md-folder", Input)
                folder.focus()
                folder.value = "/Bancos/DC01"
                await pilot.press("enter")
                assert await wait_for(
                    pilot, lambda: _stored(manifest)["DC01"]["folder"] == "/Bancos/DC01"
                )
                assert _stored(manifest)["DC01"]["folder_source"] == "manual"

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Operaciones contra el servidor
# ---------------------------------------------------------------------------


class TestServerOps:
    def test_discover_writes_the_manifest_and_shows_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest)
            config, path = _config(tmp_path, manifest=manifest)
            manifest.unlink()
            gate = threading.Event()
            uploader = _FakeUploader(gate=gate)
            monkeypatch.setattr(modelo_module, "build_uploader", lambda c, s: uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "m")
                pane = _pane(app)
                assert pane.progress_text() == ""
                pane.query_one("#md-discover", Button).press()
                # mientras corre: botonera muerta y línea viva de progreso
                assert await wait_for(pilot, lambda: "descargando tipos" in pane.progress_text())
                assert pane.query_one("#md-discover", Button).disabled is True
                gate.set()
                assert await wait_for(pilot, lambda: manifest.is_file())
                assert await wait_for(pilot, lambda: "2 tipo(s)" in pane.log_text())
                assert await wait_for(
                    pilot, lambda: pane.query_one("#md-types", DataTable).row_count == 2
                )
                assert await wait_for(pilot, lambda: pane.progress_text() == "")
                assert uploader.verified

        asyncio.run(_run())

    def test_discover_refuses_when_there_are_reviewed_types(self, tmp_path: Path) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            store = _seed(manifest)
            store.save(mark_reviewed(store.load(), "DC01"))
            before = manifest.read_text(encoding="utf-8")
            config, path = _config(tmp_path, manifest=manifest)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "m")
                pane = _pane(app)
                pane.query_one("#md-discover", Button).press()
                assert await wait_for(pilot, lambda: "Actualizar" in pane.log_text())
                assert manifest.read_text(encoding="utf-8") == before

        asyncio.run(_run())

    def test_without_credentials_the_network_buttons_only_warn(self, tmp_path: Path) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest)
            config, path = _config(tmp_path, manifest=manifest)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "m")
                pane = _pane(app)
                pane.query_one("#md-diff", Button).press()
                assert await wait_for(pilot, lambda: "credenciales" in pane.log_text())

        asyncio.run(_run())

    def test_compare_logs_the_diff_and_update_applies_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest)
            config, path = _config(tmp_path, manifest=manifest)
            monkeypatch.setattr(
                modelo_module, "build_uploader", lambda c, s: _FakeUploader(_nodes(extra=True))
            )
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "m")
                pane = _pane(app)
                pane.query_one("#md-diff", Button).press()
                assert await wait_for(pilot, lambda: "BAC_Nueva" in pane.log_text())
                pane.query_one("#md-update", Button).press()
                assert await wait_for(
                    pilot,
                    lambda: "clbNonGroup.BAC_Nueva" in _stored(manifest)["DC01"]["decisions"],
                )

        asyncio.run(_run())

    def test_verify_logs_critical_for_a_used_property_without_field_source(
        self, tmp_path: Path
    ) -> None:
        async def _run() -> None:
            manifest = tmp_path / "types.json"
            _seed(manifest, [_type_node("DC01", [_prop("clbNonGroup.BAC_Falta", required=True)])])
            config, path = _config(tmp_path, manifest=manifest)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "m")
                pane = _pane(app)
                pane.query_one("#md-check", Button).press()
                assert await wait_for(pilot, lambda: "CRITICAL" in pane.log_text())
                assert "field_sources.BAC_Falta" in pane.log_text()

        asyncio.run(_run())
