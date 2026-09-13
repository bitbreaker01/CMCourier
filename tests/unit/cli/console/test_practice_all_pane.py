"""158 — pilot de la sección "probar todos los tipos" de ``[0] PRUEBA``.

Cargar los tipos configurados, pedir los metadatos UNA vez por campo
distinto, subir un documento por tipo (mismo PDF), leer el resultado en
vivo tipo por tipo con la razón CRUDA de CM, y borrar en lote los de
prueba.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static

from cmcourier.cli.console.app import ConfirmScreen, ConsoleApp
from cmcourier.cli.console.practice_pane import PracticePane
from cmcourier.domain.models import StagedFile
from cmcourier.services.practice_upload import RawResponse
from tests.unit.cli.console.conftest import goto, wait_for
from tests.unit.cli.console.test_practice_pane import _config, _install, _with_creds

pytestmark = pytest.mark.unit


def _two_type_csv(tmp_path: Path) -> Path:
    """Dos IDCM con metadatos en el sample: CN01 (5 campos) y AF03 (3)."""
    csv = tmp_path / "two.csv"
    csv.write_text(
        "IDSistema,IDRVI,IDCM,IDClaseDocumental,CMISType,CMISFolder\n"
        ",R1,CN01,01.01.01.01.01,D:cmcourier:bacDoc,/carpeta/CN01\n"
        ",R2,AF03,01.04.02.02,D:cmcourier:bacDoc,/carpeta/AF03\n"
    )
    return csv


class _AllUploader:
    """Doble del uploader: objectId por código, con fallas configurables."""

    def __init__(self, *, fail: tuple[str, ...] = (), delete_fail: tuple[str, ...] = ()) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self._fail, self._delete_fail = set(fail), set(delete_fail)

    def upload_raw(
        self,
        file: StagedFile,
        folder_path: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
    ) -> RawResponse:
        code = document_name.split("-")[1]
        self.uploads.append(
            {"code": code, "bytes": file.path.read_bytes(), "props": dict(properties)}
        )
        if code in self._fail:
            return RawResponse(400, "Bad Request", {}, '{"message": "BAC_X no existe"}', 5, "c")
        body = f'{{"succinctProperties": {{"cmis:objectId": "obj-{code}"}}}}'
        return RawResponse(201, "Created", {"content-type": "application/json"}, body, 5, "c")

    def delete_object(self, object_id: str) -> RawResponse:
        self.deleted.append(object_id)
        if object_id in self._delete_fail:
            return RawResponse(500, "Server Error", {}, '{"message": "no se pudo"}', 5, "c")
        return RawResponse(200, "OK", {}, '{"ok": true}', 3, "c")


def _pane(app: ConsoleApp) -> PracticePane:
    return app.query_one(PracticePane)


def _text(app: ConsoleApp, selector: str) -> str:
    return str(app.query_one(selector, Static).renderable)


async def _load(pilot: Any, app: ConsoleApp) -> None:
    app.query_one("#all-load", Button).press()
    assert await wait_for(pilot, lambda: bool(app.query("#a-0")))


async def _fill_all(app: ConsoleApp) -> None:
    for index in range(len(_pane(app).distinct_field_names())):
        app.query_one(f"#a-{index}", Input).value = f"v{index}"


async def _confirm_yes(pilot: Any, app: ConsoleApp) -> None:
    assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
    app.screen.query_one("#yes", Button).press()
    await pilot.pause()


class TestLoadTypes:
    def test_lists_distinct_fields_with_type_counts(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, rvi_csv=_two_type_csv(tmp_path))
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                await _load(pilot, app)

                pane = _pane(app)
                assert pane.all_type_count() == 2
                assert pane.distinct_field_names() == (
                    "CIF",
                    "Nombre_Cliente",
                    "Short_Name",
                    "Fvenc_Inicio",
                    "Fvenc_Fin",
                )
                assert "2 tipos" in _text(app, "#all-info")
                assert "5 campos" in _text(app, "#all-info")
                # el campo compartido dice en cuántos tipos se usa.
                labels = [str(w.renderable) for w in app.query("#all-meta Label")]
                assert any(label == "CIF (usado en 2 tipos)" for label in labels)

        asyncio.run(_run())

    def test_scope_all_without_manifest_notes_it(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, rvi_csv=_two_type_csv(tmp_path))
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                app.query_one("#all-scope", Select).value = "all"
                await pilot.pause()
                await _load(pilot, app)
                assert "no usa manifest" in _text(app, "#all-info")
                assert _pane(app).all_type_count() == 2

        asyncio.run(_run())


class TestRunAll:
    def test_uploads_one_per_type_reusing_a_single_marked_pdf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, rvi_csv=_two_type_csv(tmp_path))
            uploader = _AllUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _load(pilot, app)
                await _fill_all(app)
                app.query_one("#all-run", Button).press()
                await _confirm_yes(pilot, app)

                assert await wait_for(pilot, lambda: len(uploader.uploads) == 2)
                bodies = {u["bytes"] for u in uploader.uploads}
                assert len(bodies) == 1  # REQ-003: un solo PDF reusado
                assert b"PRUEBA DE CARGA" in next(iter(bodies))
                # REQ-002: cada tipo recibe el valor del campo que usa.
                by_code = {u["code"]: u["props"] for u in uploader.uploads}
                assert by_code["AF03"]["cmcourier:BAC_CIF"] == "v0"
                assert await wait_for(pilot, lambda: "2 OK · 0 FALLA" in _text(app, "#all-summary"))
                assert app.query_one("#all-results", DataTable).row_count == 2

        asyncio.run(_run())

    def test_failure_shows_raw_reason_and_summary_is_not_green(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, rvi_csv=_two_type_csv(tmp_path))
            uploader = _AllUploader(fail=("AF03",))
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _load(pilot, app)
                await _fill_all(app)
                app.query_one("#all-run", Button).press()
                await _confirm_yes(pilot, app)

                assert await wait_for(pilot, lambda: len(_pane(app).all_outcomes) == 2)
                by_code = {o.id_corto: o for o in _pane(app).all_outcomes}
                assert by_code["CN01"].ok
                assert not by_code["AF03"].ok
                assert "400" in by_code["AF03"].reason
                assert "BAC_X no existe" in by_code["AF03"].reason
                assert "1 OK · 1 FALLA" in _text(app, "#all-summary")
                notes = [n.message for n in app._notifications]
                sev = [n.severity for n in app._notifications if "probar todos" in n.message]
                assert sev and sev[-1] == "warning"
                assert any("1 FALLA" in note for note in notes)

        asyncio.run(_run())

    def test_without_credentials_jumps_to_creds(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, rvi_csv=_two_type_csv(tmp_path))
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                await _load(pilot, app)
                app.query_one("#all-run", Button).press()
                await pilot.pause()
                from textual.widgets import TabbedContent

                assert await wait_for(
                    pilot, lambda: app.query_one(TabbedContent).active == "credenciales"
                )

        asyncio.run(_run())


class TestDeleteAll:
    def test_deletes_collected_ids_and_lists_survivors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, rvi_csv=_two_type_csv(tmp_path))
            uploader = _AllUploader(delete_fail=("obj-AF03",))
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _load(pilot, app)
                await _fill_all(app)
                app.query_one("#all-run", Button).press()
                await _confirm_yes(pilot, app)
                assert await wait_for(pilot, lambda: len(_pane(app).all_outcomes) == 2)

                app.query_one("#all-delete", Button).press()
                await _confirm_yes(pilot, app)

                assert await wait_for(pilot, lambda: len(uploader.deleted) == 2)
                assert set(uploader.deleted) == {"obj-CN01", "obj-AF03"}
                assert await wait_for(pilot, lambda: "obj-AF03" in _text(app, "#all-survivors"))
                assert "borrados 1 · quedaron 1" in _text(app, "#all-summary")

        asyncio.run(_run())


class TestNoTracking:
    def test_all_upload_never_writes_to_tracking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, rvi_csv=_two_type_csv(tmp_path))
            uploader = _AllUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _load(pilot, app)
                await _fill_all(app)
                app.query_one("#all-run", Button).press()
                await _confirm_yes(pilot, app)
                assert await wait_for(pilot, lambda: len(uploader.uploads) == 2)
                # el uploader de prueba no expone API de tracking: imposible
                # escribir en migration_log desde este camino.
                assert not hasattr(uploader, "mark_stage_done")

        asyncio.run(_run())
