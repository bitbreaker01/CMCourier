"""141 REQ-005 (E5–E9) — pilot de la pestaña ``[0] PRUEBA``.

Validar un código CM contra el mapping, cargar los metadatos requeridos
a mano, generar un documento sintético, subirlo con un uploader falso y
leer la respuesta CRUDA; después borrarlo.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input, Select, Static, TabbedContent

import cmcourier.cli.console.practice_pane as practice_module
from cmcourier.cli.console.app import ConfirmScreen, ConsoleApp, HelpScreen
from cmcourier.cli.console.practice_pane import PracticePane
from cmcourier.config.loader import load_config
from cmcourier.config.schema import PipelineConfig
from cmcourier.domain.models import StagedFile
from cmcourier.services.practice_upload import RawResponse
from tests.unit.cli.console.conftest import goto, wait_for
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = pytest.mark.unit

_SAMPLE = Path(__file__).parents[4] / "sample"
_CREATED = '{"succinctProperties": {"cmis:objectId": "workspace://obj-1"}}'


def _config(
    tmp_path: Path, *, environment: str = "staging", rvi_csv: Path | None = None
) -> tuple[PipelineConfig, Path]:
    """La config base de la consola, con el mapping split de ``sample/``
    (o un ``rvi_csv`` a medida, para los tests de varias filas por IDCM)."""
    _, path = _make_config(tmp_path, environment=environment)
    text, replaced = re.subn(
        r"mapping:\n  csv_path: .*\n",
        "mapping:\n"
        f"  rvi_cm_csv_path: {rvi_csv or (_SAMPLE / 'MapeoRVI_CM.csv')}\n"
        f"  metadatos_csv_path: {_SAMPLE / 'MetadatosCM.csv'}\n",
        path.read_text(),
    )
    assert replaced == 1
    path.write_text(text)
    return load_config(path), path


def _multi_row_csv(tmp_path: Path) -> Path:
    """Dos filas ``IDRVI`` distintas apuntando al mismo ``IDCM`` (``CN01``)."""
    csv = tmp_path / "multi.csv"
    csv.write_text(
        "IDSistema,IDRVI,IDCM,IDClaseDocumental,CMISType,CMISFolder\n"
        ",FB01,CN01,01.01.01.01.01,D:cmcourier:bacDoc,/carpeta/UNO\n"
        ",FB99,CN01,01.01.01.01.02,D:cmcourier:otroTipo,/carpeta/DOS\n"
    )
    return csv


class _FakeUploader:
    """Doble del ``CmisUploader``: sin red, con lo que hace falta para [0]."""

    def __init__(
        self,
        *,
        status: int = 201,
        body: str = _CREATED,
        type_ok: bool = True,
        folder_ok: bool = True,
    ) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self._status, self._body = status, body
        self._type_ok, self._folder_ok = type_ok, folder_ok

    def get_type_definition(self, object_type_id: str) -> Mapping[str, Any]:
        if not self._type_ok:
            raise RuntimeError("tipo desconocido")
        return {"id": object_type_id}

    def verify_folder_exists(self, folder_path: str) -> bool:
        return self._folder_ok

    def upload_raw(
        self,
        file: StagedFile,
        folder_path: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
    ) -> RawResponse:
        self.uploads.append(
            {
                "size_bytes": file.size_bytes,
                "existed": file.path.exists(),
                "folder": folder_path,
                "object_type": object_type_id,
                "name": document_name,
                "mime_type": mime_type,
                "properties": dict(properties),
            }
        )
        return RawResponse(
            self._status, "Created", {"content-type": "application/json"}, self._body, 7, "curl …"
        )

    def delete_object(self, object_id: str) -> RawResponse:
        self.deleted.append(object_id)
        return RawResponse(200, "OK", {}, '{"borrado": true}', 3, "curl delete")


class _CountingUploader:
    """Doble que sólo cuenta cuántas veces se consulta al servidor —
    para los tests de bucle de render (B1)."""

    def __init__(self) -> None:
        self.type_calls = 0
        self.folder_calls = 0

    def get_type_definition(self, object_type_id: str) -> Mapping[str, Any]:
        self.type_calls += 1
        return {"id": object_type_id}

    def verify_folder_exists(self, folder_path: str) -> bool:
        self.folder_calls += 1
        return True


def _install(monkeypatch: pytest.MonkeyPatch, uploader: object) -> None:
    monkeypatch.setattr(practice_module, "build_uploader", lambda config, secrets: uploader)


def _with_creds(app: ConsoleApp) -> None:
    app.state.creds.set("cmis", "admin", "admin")
    app.state.record_conn_result("cmis", ok=True, message="ok")


def _pane(app: ConsoleApp) -> PracticePane:
    return app.query_one(PracticePane)


def _text(app: ConsoleApp, selector: str) -> str:
    return str(app.query_one(selector, Static).renderable)


def _notes(app: Any) -> list[str]:
    return [n.message for n in app._notifications]


async def _validate(pilot: Any, app: ConsoleApp, code: str) -> None:
    field = app.query_one("#code", Input)
    field.value = code
    field.focus()
    await pilot.press("enter")
    await pilot.pause()
    await pilot.pause()


async def _confirm_yes(pilot: Any, app: ConsoleApp, *, typed: str | None = None) -> None:
    assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
    if typed is not None:
        app.screen.query_one("#ctext", Input).value = typed
    app.screen.query_one("#yes", Button).press()
    await pilot.pause()
    await pilot.pause()


async def _fill_required(app: ConsoleApp, pilot: Any, *, skip: int | None = None) -> None:
    pane = _pane(app)
    for index in range(len(pane.required_fields())):
        if index == skip:
            continue
        app.query_one(f"#m-{index}", Input).value = f"valor-{index}"
    await pilot.pause()


# ---------------------------------------------------------------------------
# E9 — cableado del shell
# ---------------------------------------------------------------------------


class TestE9Shell:
    def test_tab_is_last_and_has_keys(self) -> None:
        from cmcourier.cli.console.app import _TABS

        # 145 agregó `modelo` después: PRUEBA sigue siendo la última numerada.
        assert _TABS[-2] == "prueba"
        keys = {binding.key for binding in ConsoleApp.BINDINGS}  # type: ignore[attr-defined]
        assert "0" in keys
        assert "f10" in keys

    def test_help_mentions_the_new_screen(self) -> None:
        help_text = HelpScreen.HELP
        assert "[0]" in help_text
        assert "validar código" in help_text
        assert "generar y subir" in help_text
        assert "borrar el último subido" in help_text
        # 145 sumó la pestaña `m` (F11): la leyenda global cubre hasta F11.
        assert "F1–F11" in help_text
        assert "1-9,0,m / F1-F11" in help_text

    def test_key_zero_and_f10_switch_to_the_tab(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                await goto(pilot, app, "1")
                await goto(pilot, app, "f10")
                assert app.query_one(TabbedContent).active == "prueba"

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# E5 — validar el código contra el mapping
# ---------------------------------------------------------------------------


class TestE5Validate:
    def test_known_code_shows_target_and_one_input_per_required(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")

                assert await wait_for(pilot, lambda: app.query_one("#target").display)
                info = _text(app, "#target-info")
                assert "FB01" in info
                assert "/cmcourier-staging/CN01" in info
                assert "D:cmcourier:bacDoc" in info
                pane = _pane(app)
                assert pane.required_fields() == (
                    "CIF",
                    "Nombre_Cliente",
                    "Short_Name",
                    "Fvenc_Inicio",
                    "Fvenc_Fin",
                )
                for index in range(5):
                    assert app.query_one(f"#m-{index}", Input) is not None
                assert _text(app, "#code-err") == ""

        asyncio.run(_run())

    def test_unknown_code_shows_error_and_suggestions(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                await _validate(pilot, app, "ZZ99")

                assert await wait_for(
                    pilot, lambda: "no está en el mapping" in _text(app, "#code-err")
                )
                error = _text(app, "#code-err")
                codes = _pane(app).cm_codes()
                assert any(code in error for code in codes)
                assert not app.query_one("#target").display

        asyncio.run(_run())

    def test_without_credentials_says_only_mapping(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                assert "sin credenciales CMIS" in _text(app, "#server-check")

        asyncio.run(_run())

    def test_with_credentials_checks_type_and_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            uploader = _FakeUploader(type_ok=True, folder_ok=False)
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")

                assert await wait_for(pilot, lambda: "carpeta" in _text(app, "#server-check"))
                check = _text(app, "#server-check")
                assert "tipo ✓" in check
                assert "carpeta ✗" in check

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# E6 — subir
# ---------------------------------------------------------------------------


class TestE6Upload:
    def test_without_credentials_jumps_to_creds(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                app.set_focus(None)
                await pilot.press("s")
                await pilot.pause()

                assert await wait_for(
                    pilot, lambda: app.query_one(TabbedContent).active == "credenciales"
                )
                assert any("[2]" in note for note in _notes(app))

        asyncio.run(_run())

    def test_missing_required_marks_the_field_and_never_uploads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            uploader = _FakeUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                await _fill_required(app, pilot, skip=2)
                app.set_focus(None)
                await pilot.press("s")
                await pilot.pause()

                assert _text(app, "#e-2") == "requerido"
                assert uploader.uploads == []
                assert not isinstance(app.screen, ConfirmScreen)

        asyncio.run(_run())

    def test_full_upload_sends_cmis_property_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            uploader = _FakeUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                await _fill_required(app, pilot)
                app.query_one("#fmt", Select).value = "png"
                app.query_one("#size", Input).value = "20kb"
                app.set_focus(None)
                await pilot.press("s")
                await _confirm_yes(pilot, app)

                assert await wait_for(pilot, lambda: bool(uploader.uploads))
                sent = uploader.uploads[0]
                assert sent["properties"]["cmcourier:BAC_CIF"] == "valor-0"
                assert sent["properties"]["cmcourier:Fvenc_Fin"] == "valor-4"
                assert sent["mime_type"] == "image/png"
                assert sent["folder"] == "/cmcourier-staging/CN01"
                assert sent["object_type"] == "D:cmcourier:bacDoc"
                assert sent["existed"] is True

                assert await wait_for(
                    pilot, lambda: _pane(app).result_text().startswith("HTTP 201")
                )
                result = _pane(app).result_text()
                assert "## headers" in result
                assert "## body" in result
                assert "## curl" in result
                assert "workspace://obj-1" in result

        asyncio.run(_run())

    def test_history_row_and_delete_button(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            uploader = _FakeUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                await _fill_required(app, pilot)
                app.set_focus(None)
                await pilot.press("s")
                await _confirm_yes(pilot, app)

                assert await wait_for(pilot, lambda: bool(_pane(app).history))
                assert await wait_for(pilot, lambda: bool(app.query("#del-0")))
                line = _text(app, "#h-0")
                assert "CN01" in line
                assert "201" in line
                assert "workspace://obj-1" in line

        asyncio.run(_run())

    def test_prd_requires_typed_confirmation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path, environment="prd")
            uploader = _FakeUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                await _fill_required(app, pilot)
                app.set_focus(None)
                await pilot.press("s")

                assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                assert app.screen.query("#ctext")
                await _confirm_yes(pilot, app, typed="PRD")
                assert await wait_for(pilot, lambda: bool(uploader.uploads))

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# E7 — borrar
# ---------------------------------------------------------------------------


class TestE7Delete:
    def test_d_deletes_the_last_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            uploader = _FakeUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                await _fill_required(app, pilot)
                app.set_focus(None)
                await pilot.press("s")
                await _confirm_yes(pilot, app)
                assert await wait_for(pilot, lambda: bool(_pane(app).history))

                app.set_focus(None)
                await pilot.press("d")
                await _confirm_yes(pilot, app)

                assert await wait_for(pilot, lambda: uploader.deleted == ["workspace://obj-1"])
                assert await wait_for(pilot, lambda: "(borrado)" in _text(app, "#h-0"))
                assert "borrado" in _pane(app).result_text()
                assert not app.query("#del-0")

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# E8 — guardas
# ---------------------------------------------------------------------------


class TestE8Guards:
    def test_active_run_blocks_the_upload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            uploader = _FakeUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                await _fill_required(app, pilot)
                monkeypatch.setattr(type(app), "run_active", property(lambda self: True))
                app.set_focus(None)
                await pilot.press("s")
                await pilot.pause()

                assert any("corrida activa" in note for note in _notes(app))
                assert uploader.uploads == []

        asyncio.run(_run())

    def test_config_without_mapping_shows_only_the_notice(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config.model_copy(update={"mapping": None}), config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                await pilot.pause()

                assert "mapping" in _text(app, "#no-mapping")
                assert not app.query_one("#body").display

        asyncio.run(_run())

    def test_the_screen_says_it_does_not_touch_tracking(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "0")
                hint = _text(app, "#practice-hint")
                assert "tracking" in hint
                assert "[7]" in hint

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# B1 — antagonista: bucle infinito Select.Changed -> render_target
# ---------------------------------------------------------------------------


class TestB1RenderLoop:
    def test_row_change_renders_at_most_twice_and_checks_server_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        csv = _multi_row_csv(tmp_path)
        counter = _CountingUploader()
        _install(monkeypatch, counter)
        calls = {"n": 0}
        original = PracticePane.render_target

        async def counted(self: PracticePane) -> None:
            calls["n"] += 1
            await original(self)

        monkeypatch.setattr(PracticePane, "render_target", counted)

        async def _run() -> None:
            config, path = _config(tmp_path, rvi_csv=csv)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                assert await wait_for(pilot, lambda: app.query_one("#target").display)
                # deja que la validación inicial (que sí renderiza y chequea
                # una vez) se asiente antes de fijar la base.
                for _ in range(10):
                    await pilot.pause()
                base_renders = calls["n"]
                base_type, base_folder = counter.type_calls, counter.folder_calls

                sel = app.query_one("#row", Select)
                sel.value = "1"
                # ronda acotada — si hay bucle, esto NO drena solo, pero
                # tampoco cuelga el test: simplemente deja pasar el tiempo
                # y mide cuánto se disparó en esa ventana.
                for _ in range(40):
                    await asyncio.sleep(0.01)
                    await pilot.pause()

                assert calls["n"] - base_renders <= 2, (
                    f"render_target se llamó {calls['n'] - base_renders} veces por UN "
                    "cambio de fila"
                )
                assert counter.type_calls - base_type == 1
                assert counter.folder_calls - base_folder == 1

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# B2 — antagonista: excepción en worker mata la app
# ---------------------------------------------------------------------------


class TestB2WorkerCrash:
    def test_check_target_exception_does_not_brick_the_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(config: object, secrets: object) -> None:
            raise RuntimeError("base_url inválida / TLS roto")

        monkeypatch.setattr(practice_module, "build_uploader", _boom)

        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")

                assert await wait_for(pilot, lambda: not _pane(app)._busy)
                assert app.is_running
                assert "RuntimeError" in _text(app, "#server-check")
                assert app.query_one("#validate", Button).disabled is False

        asyncio.run(_run())

    def test_upload_exception_does_not_brick_the_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(config: object, secrets: object) -> None:
            raise RuntimeError("se cayó la red a mitad de camino")

        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                monkeypatch.setattr(practice_module, "build_uploader", _boom)
                await _fill_required(app, pilot)
                app.set_focus(None)
                await pilot.press("s")
                await _confirm_yes(pilot, app)

                assert await wait_for(pilot, lambda: not _pane(app)._busy)
                assert app.is_running
                assert any("RuntimeError" in note for note in _notes(app))
                assert app.query_one("#upload", Button).disabled is False
                # la app sigue respondiendo — el ciclo de guardas normal
                # sigue funcionando tras el crash.
                await pilot.press("s")
                await pilot.pause()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# I1 — antagonista: headers con credenciales en pantalla
# ---------------------------------------------------------------------------


class TestI1MaskHeaders:
    def test_set_cookie_header_is_masked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uploader = _FakeUploader()
        uploader_upload_raw = uploader.upload_raw

        def _leaky_upload_raw(*a: Any, **k: Any) -> RawResponse:
            resp = uploader_upload_raw(*a, **k)
            return RawResponse(
                resp.status_code,
                resp.reason,
                {**resp.headers, "Set-Cookie": "JSESSIONID=SECRETO"},
                resp.body,
                resp.elapsed_ms,
                resp.curl,
            )

        monkeypatch.setattr(uploader, "upload_raw", _leaky_upload_raw)
        _install(monkeypatch, uploader)

        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                await _fill_required(app, pilot)
                app.set_focus(None)
                await pilot.press("s")
                await _confirm_yes(pilot, app)

                assert await wait_for(pilot, lambda: _pane(app).result_text().startswith("HTTP"))
                text = _pane(app).result_text()
                assert "SECRETO" not in text
                assert "set-cookie: ***" in text.lower()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# I2 — antagonista: request_delete(index) reindexado por un insert previo
# ---------------------------------------------------------------------------


class TestI2DeleteByIdentity:
    def test_delete_targets_the_attempt_object_not_a_stale_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uploader = _FakeUploader()
        _install(monkeypatch, uploader)

        async def _run() -> None:
            config, path = _config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)  # 141 I6: borrar exige credenciales CMIS frescas
                await goto(pilot, app, "0")
                pane = _pane(app)
                first = practice_module.Attempt(
                    stamp="10:00:00",
                    cm_code="CN01",
                    name="uno.pdf",
                    status=201,
                    object_id="obj-uno",
                )
                pane.history.append(first)
                await pane.render_history()
                await pilot.pause()

                # el operador pide borrar el intento en el índice 0 (``first``)…
                pane.request_delete(0)
                # …pero ANTES de confirmar, llega un intento nuevo y se
                # inserta AL FRENTE — reindexando ``first`` a la posición 1.
                second = practice_module.Attempt(
                    stamp="10:00:05",
                    cm_code="CN01",
                    name="dos.pdf",
                    status=201,
                    object_id="obj-dos",
                )
                pane.history.insert(0, second)
                await pane.render_history()
                await pilot.pause()

                assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                app.screen.query_one("#yes", Button).press()

                assert await wait_for(pilot, lambda: bool(uploader.deleted))
                # el objectId borrado tiene que ser el de ``first``
                # (obj-uno), NO el que ahora vive en el índice 0 (obj-dos).
                assert uploader.deleted == ["obj-uno"]
                assert first.deleted is True
                assert second.deleted is False

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# I6 — antagonista: `d` sin las guardas de `run_active` / credenciales
# ---------------------------------------------------------------------------


class TestI6DeleteGuards:
    def test_d_respects_active_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            uploader = _FakeUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                await _fill_required(app, pilot)
                app.set_focus(None)
                await pilot.press("s")
                await _confirm_yes(pilot, app)
                assert await wait_for(pilot, lambda: bool(_pane(app).history))

                monkeypatch.setattr(type(app), "run_active", property(lambda self: True))
                app.set_focus(None)
                await pilot.press("d")
                await pilot.pause()

                assert not isinstance(app.screen, ConfirmScreen)
                assert uploader.deleted == []
                assert any("corrida activa" in note for note in _notes(app))

        asyncio.run(_run())

    def test_d_requires_fresh_cmis_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _run() -> None:
            config, path = _config(tmp_path)
            uploader = _FakeUploader()
            _install(monkeypatch, uploader)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                _with_creds(app)
                await goto(pilot, app, "0")
                await _validate(pilot, app, "CN01")
                await _fill_required(app, pilot)
                app.set_focus(None)
                await pilot.press("s")
                await _confirm_yes(pilot, app)
                assert await wait_for(pilot, lambda: bool(_pane(app).history))

                app.state.conn.pop("cmis", None)
                app.set_focus(None)
                await pilot.press("d")
                await pilot.pause()

                assert not isinstance(app.screen, ConfirmScreen)
                assert uploader.deleted == []
                assert await wait_for(
                    pilot, lambda: app.query_one(TabbedContent).active == "credenciales"
                )

        asyncio.run(_run())
