"""141 REQ-005 — panel ``[0] PRUEBA`` de la consola.

UN tiro de prueba contra un código de Content Manager antes de disparar
una corrida de miles: se valida el código contra el mapping, se tipean a
mano los metadatos REQUERIDOS, se genera un documento sintético del
formato y tamaño pedidos, se sube con UN solo POST y se muestra la
respuesta CRUDA del servidor — headers, body completo y el curl
equivalente.

El documento NO pasa por tracking ni por idempotencia: no aparece en
``[7]`` ni en el ``migration_log``. Por eso la pantalla ofrece borrarlo:
lo que sube acá lo limpia el operador, no el `pipeline`.

La lógica pura vive en :mod:`cmcourier.services.practice_upload`; acá
sólo hay widget, workers y confirmaciones.
"""

from __future__ import annotations

__all__ = ["PracticePane"]

import contextlib
import difflib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from cmcourier.cli.doctor import build_uploader
from cmcourier.config.wiring import build_mapping_service
from cmcourier.domain.models import CMMapping
from cmcourier.services.mapping import MappingService
from cmcourier.services.mock.sizing import parse_size
from cmcourier.services.mock.synthetic_file import (
    MAX_SIZE_BYTES,
    MIN_SIZE_BYTES,
    SyntheticFormat,
)
from cmcourier.services.practice_upload import (
    PracticeDraft,
    PracticeResult,
    RawResponse,
    document_name,
    run_practice_upload,
    validate_values,
)

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

_FORMATS: list[tuple[str, str]] = [
    ("pdf — una página, tamaño exacto", "pdf"),
    ("tiff — imagen LZW", "tiff"),
    ("jpeg — imagen comprimida", "jpeg"),
    ("png — imagen sin pérdida", "png"),
]
_MAX_HISTORY = 20
_MAX_SUGGESTIONS = 5
_HINT = (
    "Un tiro de prueba: UN documento sintético a un código CM, sin reintentos, "
    "con la respuesta cruda del servidor. NO pasa por tracking ni por "
    "idempotencia — no aparece en [7] BATCHES ni en el migration_log, así que "
    "borralo vos cuando termines."
)


@dataclass
class Attempt:
    """Una línea del historial de la sesión."""

    stamp: str
    cm_code: str
    name: str
    status: int
    object_id: str | None
    deleted: bool = False

    def line(self) -> str:
        tail = "(borrado)" if self.deleted else (f"[{self.object_id}]" if self.object_id else "")
        return f"{self.stamp}  {self.cm_code}  {self.name} → {self.status} {tail}".rstrip()


class PracticePane(VerticalScroll):
    """``VerticalScroll``: formulario + resultado + historial no entran en 24 filas."""

    DEFAULT_CSS = """
    PracticePane { padding: 1 2; }
    PracticePane .hint { color: $text-muted; margin-bottom: 1; }
    PracticePane .warn { color: $warning; }
    PracticePane .err { color: $error; }
    PracticePane .section { text-style: bold; margin-top: 1; }
    PracticePane .frow { height: auto; margin-bottom: 1; }
    PracticePane .frow Label { width: 20; color: $text-muted; padding-top: 1; }
    PracticePane .frow Input { width: 40; }
    PracticePane .frow Select { width: 40; }
    PracticePane .frow Button { margin-left: 1; }
    PracticePane #target { height: auto; border: solid $surface-lighten-2; padding: 0 1;
                           margin-bottom: 1; }
    PracticePane #result { height: 16; }
    PracticePane #history { height: auto; }
    PracticePane .hrow { height: auto; }
    """

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console
        self._service: MappingService | None = None
        self._rows: tuple[CMMapping, ...] = ()
        self._row_index = 0
        self._busy = False
        self._rendering = False
        self.history: list[Attempt] = []

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static(_HINT, classes="hint", id="practice-hint")
        yield Static("", classes="warn", id="no-mapping", markup=False)
        with Vertical(id="body"):
            with Horizontal(classes="frow"):
                yield Label("código CM")
                yield Input(placeholder="código CM, p. ej. CN01", id="code")
                yield Button("validar (↵)", variant="primary", id="validate")
            yield Static("", classes="err", id="code-err", markup=False)
            with Vertical(id="target"):
                yield Select([("—", "0")], value="0", id="row", allow_blank=False)
                yield Static("", id="target-info", markup=False)
                yield Static("", id="server-check", markup=False)
                yield Static("metadatos requeridos", classes="section")
                yield Vertical(id="meta")
            with Horizontal(classes="frow"):
                yield Label("formato")
                yield Select(_FORMATS, value="pdf", id="fmt", allow_blank=False)
            with Horizontal(classes="frow"):
                yield Label("tamaño")
                yield Input(value="200kb", placeholder="ej. 200kb, 2mb", id="size")
                yield Button("generar y subir (s)", variant="primary", id="upload")
            yield Static("", classes="warn", id="size-note", markup=False)
            yield Static("respuesta del servidor", classes="section")
            yield TextArea("", id="result", read_only=True)
            yield Static("intentos de esta sesión (máx. 20)", classes="section")
            yield Vertical(id="history")

    def on_mount(self) -> None:
        self._load_mapping()
        self.query_one("#target").display = False

    # ------------------------------------------------------------ mapping

    def _load_mapping(self) -> None:
        """Carga el Modelo Documental de la config EFECTIVA.

        Sin bloque ``mapping:`` (o con un CSV que no se puede leer) la
        pantalla se apaga entera: no hay código CM que validar.
        """
        model = getattr(self.console.effective_config(), "mapping", None)
        if model is None:
            self._fail_mapping("esta config no tiene `mapping:` — no hay códigos CM que validar")
            return
        try:
            self._service = build_mapping_service(model)
        except Exception as exc:  # noqa: BLE001 — cualquier CSV roto apaga la pantalla
            self._fail_mapping(f"no se pudo leer el `mapping:` de esta config — {exc}")
            return
        self.query_one("#no-mapping", Static).display = False

    def _fail_mapping(self, message: str) -> None:
        self._service = None
        self.query_one("#no-mapping", Static).update(message)
        self.query_one("#body").display = False

    def cm_codes(self) -> tuple[str, ...]:
        """Los códigos CM conocidos (para las sugerencias y para los tests)."""
        return self._service.cm_codes() if self._service is not None else ()

    def current_mapping(self) -> CMMapping | None:
        """La fila elegida en ``#row`` (la única, cuando hay una sola)."""
        if not self._rows:
            return None
        return self._rows[min(self._row_index, len(self._rows) - 1)]

    def required_fields(self) -> tuple[str, ...]:
        mapping = self.current_mapping()
        return mapping.required_metadata_fields if mapping is not None else ()

    # ------------------------------------------------------------ validar

    def validate_code(self) -> None:
        """141 REQ-005: busca el código en el mapping y arma el formulario."""
        if self._service is None or self._guard_busy():
            return
        code = self.query_one("#code", Input).value.strip()
        if not code:
            self._set_error("escribí un código CM (el IDCM del Modelo Documental)")
            return
        rows = self._service.get_by_cm_code(code)
        if not rows:
            self._set_error(self._not_found_text(code))
            return
        self._set_error("")
        self._rows, self._row_index = rows, 0
        self.call_later(self.render_target)

    def _not_found_text(self, code: str) -> str:
        """Hasta 5 sugerencias por similitud.

        Con un código muy lejano (``ZZ99``) el corte de 0.4 no devuelve
        nada y el operador se queda sin pista; en ese caso se relaja el
        corte y se ofrecen los más parecidos que haya, diciendo que son
        eso y no un match.
        """
        codes = self.cm_codes()
        text = f"{code} no está en el mapping de esta config"
        close = difflib.get_close_matches(code, codes, n=_MAX_SUGGESTIONS, cutoff=0.4)
        if close:
            return f"{text} — ¿quisiste decir {', '.join(close)}?"
        loose = difflib.get_close_matches(code, codes, n=_MAX_SUGGESTIONS, cutoff=0.0)
        if loose:
            return f"{text} — los más parecidos: {', '.join(loose)}"
        return text

    def _set_error(self, text: str) -> None:
        self.query_one("#code-err", Static).update(text)
        if text:
            self._rows = ()
            self.query_one("#target").display = False

    async def render_target(self) -> None:
        """Panel del destino + un ``Input`` por metadato requerido."""
        mapping = self.current_mapping()
        if mapping is None:
            return
        self.query_one("#target").display = True
        self._render_row_select()
        folder = mapping.cmis_folder or mapping.cm_folder
        object_type = mapping.cmis_type or mapping.cm_object_type
        self.query_one("#target-info", Static).update(
            f"IDRVI {mapping.id_rvi} · clase {mapping.clase_id}\n"
            f"CMISType   {object_type}\n"
            f"CMISFolder {folder}"
        )
        meta = self.query_one("#meta", Vertical)
        await meta.remove_children()
        rows: list[Any] = []
        for index, field in enumerate(mapping.required_metadata_fields):
            rows.append(Horizontal(Label(field), Input(id=f"m-{index}"), classes="frow"))
            rows.append(Static("", classes="err", id=f"e-{index}", markup=False))
        if not rows:
            rows.append(Static("este código no declara metadatos requeridos"))
        await meta.mount_all(rows)
        self._check_target(mapping)

    def _render_row_select(self) -> None:
        select = self.query_one("#row", Select)
        select.display = len(self._rows) > 1
        if len(self._rows) <= 1:
            return
        options = [
            (
                f"{m.id_rvi} · {m.cmis_type or m.cm_object_type} · {m.cmis_folder or m.cm_folder}",
                str(i),
            )
            for i, m in enumerate(self._rows)
        ]
        self._rendering = True
        try:
            select.set_options(options)
            select.value = str(self._row_index)
        finally:
            self._rendering = False

    # -------------------------------------------- chequeo contra el server

    def _check_target(self, mapping: CMMapping) -> None:
        """Con credenciales CMIS, confirma tipo y carpeta contra el servidor.

        Un ✗ NO bloquea el upload: el punto de la pantalla es ver qué
        contesta el servidor, no adivinarlo desde el CSV.
        """
        check = self.query_one("#server-check", Static)
        if not self.console.state.creds_ready(required=("cmis",)):
            check.update("sin credenciales CMIS — validado sólo contra el mapping ([2])")
            return
        check.update("consultando el servidor…")
        config = self.console.effective_config()
        secrets = self.console.state.creds.to_secrets()
        object_type = mapping.cmis_type or mapping.cm_object_type
        folder = mapping.cmis_folder or mapping.cm_folder

        def work() -> None:
            uploader = build_uploader(config, secrets)
            text = f"tipo {_probe(lambda: uploader.get_type_definition(object_type))}"
            text += f" · carpeta {_probe(lambda: uploader.verify_folder_exists(folder))}"
            self.console.call_from_thread(self._apply_check, text)

        self._set_busy(True)
        self.console.run_worker(work, thread=True, exclusive=False)

    def _apply_check(self, text: str) -> None:
        self._set_busy(False)
        with contextlib.suppress(NoMatches):
            self.query_one("#server-check", Static).update(text)

    # ------------------------------------------------------------ subir

    def request_upload(self) -> None:
        """141 REQ-005: la cadena de guardas del tiro de prueba."""
        if self._service is None or self._guard_busy():
            return
        if self.console.run_active:
            self.console.notify("hay una corrida activa — probá cuando termine", severity="warning")
            return
        mapping = self.current_mapping()
        if mapping is None:
            self.console.notify("Validá primero un código CM (↵)", severity="warning")
            return
        if not self.console.state.creds_ready(required=("cmis",)):
            self.console.notify(
                "Sin credenciales frescas de sesión — cargalas en [2]", severity="error"
            )
            self.console.action_switch_tab("credenciales")
            return
        values = self._values(mapping)
        errors = validate_values(mapping, values)
        self._render_field_errors(mapping, errors)
        if errors:
            self.console.notify("Faltan metadatos requeridos", severity="error")
            return
        size_bytes = self._size_bytes()
        if size_bytes is None:
            return
        self._confirm_upload(
            PracticeDraft(
                cm_code=mapping.id_corto,
                mapping=mapping,
                values=values,
                fmt=self._format(),
                size_bytes=size_bytes,
            )
        )

    def _values(self, mapping: CMMapping) -> dict[str, str]:
        values: dict[str, str] = {}
        for index, field in enumerate(mapping.required_metadata_fields):
            with contextlib.suppress(NoMatches):
                values[field] = self.query_one(f"#m-{index}", Input).value
        return values

    def _render_field_errors(self, mapping: CMMapping, errors: dict[str, str]) -> None:
        for index, field in enumerate(mapping.required_metadata_fields):
            with contextlib.suppress(NoMatches):
                self.query_one(f"#e-{index}", Static).update(errors.get(field, ""))

    def _format(self) -> SyntheticFormat:
        value = str(self.query_one("#fmt", Select).value)
        return value if value in ("pdf", "tiff", "jpeg", "png") else "pdf"  # type: ignore[return-value]

    def _size_bytes(self) -> int | None:
        raw = self.query_one("#size", Input).value.strip()
        try:
            size = parse_size(raw)
        except ValueError as exc:
            self.console.notify(f"tamaño: {exc}", severity="error")
            return None
        if not MIN_SIZE_BYTES <= size <= MAX_SIZE_BYTES:
            self.console.notify(f"tamaño: {raw} está fuera del rango 1kb–50mb", severity="error")
            return None
        return size

    def _confirm_upload(self, draft: PracticeDraft) -> None:
        now = datetime.now()
        name = document_name(draft.cm_code, draft.fmt, now)
        prd = self.console.config.environment == "prd"
        self.console.confirm(
            title="Subir documento de prueba",
            body=f"{name} → {draft.folder} ({draft.object_type})",
            yes="subir",
            no="cancelar",
            danger=prd,
            confirm_text="PRD" if prd else None,
            cb=lambda ok: self._start_upload(draft, now) if ok else None,
        )

    def _start_upload(self, draft: PracticeDraft, now: datetime) -> None:
        self._set_busy(True)
        self.query_one("#result", TextArea).text = "subiendo…"
        self.query_one("#size-note", Static).update("")
        config = self.console.effective_config()
        secrets = self.console.state.creds.to_secrets()
        workdir = config.assembly.temp_dir

        def work() -> None:
            try:
                result: PracticeResult | None = run_practice_upload(
                    draft, build_uploader(config, secrets), workdir=workdir, now=now
                )
                error: str | None = None
            except Exception as exc:  # noqa: BLE001 — el worker nunca revienta la UI
                result, error = None, f"{type(exc).__name__}: {exc}"
            self.console.call_from_thread(self._apply_upload, draft, result, error)

        self.console.run_worker(work, thread=True, exclusive=False)

    def _apply_upload(
        self, draft: PracticeDraft, result: PracticeResult | None, error: str | None
    ) -> None:
        self._set_busy(False)
        with contextlib.suppress(NoMatches):
            if result is None:
                self.query_one("#result", TextArea).text = f"✘ no hubo respuesta: {error}"
                self.console.notify(str(error), severity="error", timeout=10)
                return
            self._show_response(result.response, header=self._headline(result))
            self.query_one("#size-note", Static).update(result.size_note)
            self._push_attempt(
                Attempt(
                    stamp=time.strftime("%H:%M:%S"),
                    cm_code=draft.cm_code,
                    name=result.name,
                    status=result.response.status_code,
                    object_id=result.response.object_id,
                )
            )
            self.console.notify(
                f"HTTP {result.response.status_code} · {result.name}",
                severity="information" if result.response.ok else "error",
            )

    @staticmethod
    def _headline(result: PracticeResult) -> str:
        response = result.response
        return (
            f"HTTP {response.status_code} {response.reason} · {response.elapsed_ms} ms · "
            f"{result.name} ({result.size_bytes} bytes)"
        )

    def _show_response(self, response: RawResponse, *, header: str) -> None:
        """Vuelca la respuesta CRUDA — nada se enmascara salvo la contraseña
        del curl: los valores los tipeó el operador en esta sesión."""
        headers = "\n".join(f"{k}: {v}" for k, v in response.headers.items())
        self.query_one("#result", TextArea).text = "\n".join(
            [
                header,
                "",
                "## headers",
                headers,
                "",
                "## body",
                _pretty(response.body),
                "",
                "## curl",
                response.curl,
            ]
        )

    def result_text(self) -> str:
        """La respuesta mostrada (para los tests y para copiar)."""
        return self.query_one("#result", TextArea).text

    # ------------------------------------------------------------ historial

    def _push_attempt(self, attempt: Attempt) -> None:
        self.history.insert(0, attempt)
        del self.history[_MAX_HISTORY:]
        self.call_later(self.render_history)

    async def render_history(self) -> None:
        container = self.query_one("#history", Vertical)
        await container.remove_children()
        rows: list[Any] = []
        for index, attempt in enumerate(self.history):
            row = Horizontal(classes="hrow")
            row.compose_add_child(Static(attempt.line(), id=f"h-{index}", markup=False))
            if attempt.object_id and not attempt.deleted:
                row.compose_add_child(Button("borrar", id=f"del-{index}"))
            rows.append(row)
        if rows:
            await container.mount_all(rows)

    def request_delete_last(self) -> None:
        """Tecla ``d``: borra el último intento que dejó un ``objectId``."""
        for index, attempt in enumerate(self.history):
            if attempt.object_id and not attempt.deleted:
                self.request_delete(index)
                return
        self.console.notify("No hay nada subido para borrar", severity="warning")

    def request_delete(self, index: int) -> None:
        if self._guard_busy() or not 0 <= index < len(self.history):
            return
        attempt = self.history[index]
        if not attempt.object_id or attempt.deleted:
            return
        prd = self.console.config.environment == "prd"
        self.console.confirm(
            title="Borrar el documento de prueba",
            body=f"{attempt.name} · objectId {attempt.object_id}\nSe borra del Content Manager.",
            yes="borrar",
            no="cancelar",
            danger=True,
            confirm_text="PRD" if prd else None,
            cb=lambda ok: self._start_delete(index) if ok else None,
        )

    def _start_delete(self, index: int) -> None:
        attempt = self.history[index]
        object_id = attempt.object_id or ""
        self._set_busy(True)
        config = self.console.effective_config()
        secrets = self.console.state.creds.to_secrets()

        def work() -> None:
            try:
                response: RawResponse | None = build_uploader(config, secrets).delete_object(
                    object_id
                )
                error: str | None = None
            except Exception as exc:  # noqa: BLE001 — el worker nunca revienta la UI
                response, error = None, f"{type(exc).__name__}: {exc}"
            self.console.call_from_thread(self._apply_delete, index, response, error)

        self.console.run_worker(work, thread=True, exclusive=False)

    def _apply_delete(self, index: int, response: RawResponse | None, error: str | None) -> None:
        self._set_busy(False)
        with contextlib.suppress(NoMatches):
            if response is None:
                self.query_one("#result", TextArea).text = f"✘ no hubo respuesta: {error}"
                self.console.notify(str(error), severity="error", timeout=10)
                return
            attempt = self.history[index]
            self._show_response(
                response,
                header=(
                    f"HTTP {response.status_code} {response.reason} · "
                    f"{response.elapsed_ms} ms · borrar {attempt.name}"
                ),
            )
            if response.ok:
                attempt.deleted = True
                self.call_later(self.render_history)
            self.console.notify(
                f"borrado: HTTP {response.status_code}",
                severity="information" if response.ok else "error",
            )

    # ------------------------------------------------------------ estado

    def _guard_busy(self) -> bool:
        if self._busy:
            self.console.notify("esperá — hay una consulta en curso", severity="warning")
        return self._busy

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        with contextlib.suppress(NoMatches):
            self.query_one("#upload", Button).disabled = busy
            self.query_one("#validate", Button).disabled = busy

    # ------------------------------------------------------------ eventos

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "validate":
            event.stop()
            self.validate_code()
        elif bid == "upload":
            event.stop()
            self.request_upload()
        elif bid.startswith("del-"):
            event.stop()
            self.request_delete(int(bid.removeprefix("del-")))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "code":
            event.stop()
            self.validate_code()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "row" and not self._rendering:
            event.stop()
            with contextlib.suppress(ValueError, TypeError):
                self._row_index = int(str(event.value))
            self.call_later(self.render_target)


def _probe(call: Any) -> str:
    """``✓`` / ``✗ <motivo>`` — un fallo NO bloquea el upload."""
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001 — el motivo se muestra, no se propaga
        return f"✗ {type(exc).__name__}: {exc}"
    return "✓" if result is not False else "✗ no existe"


def _pretty(body: str) -> str:
    """JSON indentado si parsea; el cuerpo crudo si no."""
    try:
        return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return body
