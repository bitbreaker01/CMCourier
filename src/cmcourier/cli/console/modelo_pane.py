"""Panel ``M·MODELO`` de la consola (145 REQ-006).

El manifest de tipos CM sin salir de la consola: la tabla de clases
documentales que publica Content Manager, las propiedades escribibles de
la clase seleccionada, y las decisiones del operador — ``usar`` (la
propiedad viaja al wire) u ``omitir`` (gana el default del servidor).

Cuatro operaciones, las mismas que ``cmcourier types``:

* **Descubrir** — baja el árbol entero y escribe el manifest de cero.
* **Comparar** — qué cambió el servidor desde el último manifest.
* **Actualizar** — mergea esos cambios conservando lo que decidiste.
* **Verificar YAML** — cruce offline manifest ↔ YAML ↔ ``MapeoRVI_CM.csv``.

Las tres primeras hablan con el servidor: corren en worker thread, con la
botonera deshabilitada y el progreso (``SyncProgress`` de 144)
marshalado al hilo de UI. Sin credenciales `cmis` avisan en el log y no
rompen nada. La revisión y el ``check`` viven offline.

Cada tecla que cambia algo se guarda en el acto con el store atómico: no
hay "guardar" que apretar y no hay estado sucio que perder.
"""

from __future__ import annotations

__all__ = ["ModeloPane"]

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable, Input, Log, Static

from cmcourier.adapters.manifest.json_store import JsonTypeManifestStore
from cmcourier.cli.commands._formatting import truncate
from cmcourier.cli.doctor import build_uploader
from cmcourier.config.loader import Secrets
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.wiring import build_mapping_service, build_metadata_config
from cmcourier.domain.cm_types import (
    DECISION_OMIT,
    DECISION_USE,
    CmTypeEntry,
    CmTypeManifest,
    canonical_name,
)
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.services.manifest_check import run_manifest_check
from cmcourier.services.sync_progress import SyncProgress
from cmcourier.services.type_discovery import TypeDiscoveryService
from cmcourier.services.type_manifest import (
    ManifestDiff,
    apply_diff,
    diff_manifest,
    mark_reviewed,
    set_decision,
    set_folder,
)

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

_T = TypeVar("_T")

_TYPE_COLS = ("IDCM", "nombre", "usar", "omitir", "revisado", "carpeta")
_PROP_COLS = ("propiedad", "req", "tipo", "len", "default", "decisión")
#: ``folder_ok`` / ``reviewed`` para el operador: sí, no, sin verificar.
_MARK: dict[bool | None, str] = {True: "✓", False: "✗", None: "?"}
_BUTTONS = ("#md-discover", "#md-diff", "#md-update", "#md-check")
_MAX_LOG_LINES = 300


class ModeloPane(VerticalScroll):
    """``VerticalScroll``: dos tablas + botonera + log no entran en 24 filas."""

    DEFAULT_CSS = """
    ModeloPane { padding: 1 2; }
    ModeloPane .intro { color: $text-muted; margin-bottom: 1; }
    ModeloPane .frow { height: 3; }
    ModeloPane .frow Button { margin-right: 1; }
    ModeloPane .title { text-style: bold; }
    ModeloPane #md-progress { height: 1; color: $text-muted; }
    ModeloPane #md-types { height: 10; }
    ModeloPane #md-props { height: 10; }
    ModeloPane #md-folder { width: 60; }
    ModeloPane #md-log { height: 8; border: solid $surface-lighten-2; padding: 0 1; }
    """
    BINDINGS = [
        Binding("space", "toggle_decision", "usar/omitir", show=False),
        Binding("enter", "mark_reviewed", "marcar revisado", show=False),
        Binding("f", "focus_folder", "editar carpeta", show=False),
    ]

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console
        self._manifest: CmTypeManifest | None = None
        self._codes: list[str] = []
        self._busy = False
        # El hint del log se repite en cada activación de la pestaña si no
        # se recuerda cuál fue el último: una línea, no veinte.
        self._last_hint = ""

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static(
            "Manifest de tipos CM (145): lo que el servidor publica + tus decisiones. "
            "espacio usar/omitir · ↵ marcar el tipo revisado · f editar la carpeta. "
            "Todo se guarda al JSON al toque.",
            classes="intro",
        )
        with Horizontal(classes="frow"):
            yield Button("Descubrir", id="md-discover", variant="primary")
            yield Button("Comparar", id="md-diff")
            yield Button("Actualizar", id="md-update")
            yield Button("Verificar YAML", id="md-check")
        yield Static("", id="md-progress", markup=False)
        yield Static("CLASES DOCUMENTALES", classes="title")
        types: DataTable[str] = DataTable(id="md-types", cursor_type="row", zebra_stripes=True)
        yield types
        yield Static("PROPIEDADES ESCRIBIBLES DEL TIPO SELECCIONADO", classes="title")
        props: DataTable[str] = DataTable(id="md-props", cursor_type="row", zebra_stripes=True)
        yield props
        yield Input(placeholder="carpeta destino (↵ la fija a mano)", id="md-folder")
        yield Log(id="md-log", max_lines=_MAX_LOG_LINES, auto_scroll=True)

    def on_mount(self) -> None:
        self.query_one("#md-types", DataTable).add_columns(*_TYPE_COLS)
        self.query_one("#md-props", DataTable).add_columns(*_PROP_COLS)
        self.reload()

    # ------------------------------------------------------------ manifest

    def _manifest_path(self) -> Path | None:
        """``mapping.type_manifest_path`` del YAML, si la config lo declara."""
        config = getattr(self.console, "config", None)
        configured = getattr(getattr(config, "mapping", None), "type_manifest_path", None)
        return Path(str(configured)) if configured else None

    def _store(self) -> JsonTypeManifestStore | None:
        path = self._manifest_path()
        return JsonTypeManifestStore(path) if path is not None else None

    def reload(self) -> None:
        """Re-lee el manifest del disco y repinta (se llama al activar la tab)."""
        store = self._store()
        self._manifest = None
        if store is None:
            self._hint("no hay manifest: configurá mapping.type_manifest_path en el YAML ([9])")
        elif not store.exists():
            self._hint(f"todavía no hay manifest en {store.path} — empezá por Descubrir")
        else:
            try:
                self._manifest = store.load()
            except ConfigurationError as exc:
                self._hint(f"✘ el manifest no se puede leer: {exc}")
        self._render_types()
        self._sync_buttons()

    # ------------------------------------------------------------ pintado

    def _render_types(self, *, cursor: int = 0) -> None:
        table = self.query_one("#md-types", DataTable)
        table.clear()
        manifest = self._manifest
        self._codes = sorted(manifest.types) if manifest is not None else []
        for code in self._codes:
            entry = manifest.types[code]  # type: ignore[union-attr]
            usar = len(entry.usable_properties())
            table.add_row(
                code,
                truncate(entry.display_name or "-", 28),
                str(usar),
                str(len(entry.properties) - usar),
                _MARK[entry.reviewed],
                f"{_MARK[entry.folder_ok]} {truncate(entry.folder, 32)}",
                key=code,
            )
        if self._codes:
            table.move_cursor(row=min(max(cursor, 0), len(self._codes) - 1))
        self._render_props()

    def _render_props(self) -> None:
        table = self.query_one("#md-props", DataTable)
        table.clear()
        entry = self.selected_entry()
        folder = self.query_one("#md-folder", Input)
        if entry is None:
            folder.value = ""
            return
        for prop in entry.properties:
            table.add_row(
                prop.id,
                "sí" if prop.required else "no",
                prop.property_type or "-",
                str(prop.max_length) if prop.max_length is not None else "-",
                truncate(prop.default_value or "-", 16),
                entry.decisions.get(prop.id, "-"),
                key=prop.id,
            )
        folder.value = entry.folder

    def _repaint(self) -> None:
        """Repinta conservando dónde estaba el cursor de las dos tablas."""
        row_types = self.query_one("#md-types", DataTable).cursor_row
        row_props = self.query_one("#md-props", DataTable).cursor_row
        self._render_types(cursor=row_types)
        props = self.query_one("#md-props", DataTable)
        if props.row_count:
            props.move_cursor(row=min(max(row_props, 0), props.row_count - 1))

    def _sync_buttons(self) -> None:
        enabled = self._store() is not None and not self._busy
        self.query_one("#md-discover", Button).disabled = not enabled
        for wid in ("#md-diff", "#md-update", "#md-check"):
            self.query_one(wid, Button).disabled = not (enabled and self._manifest is not None)

    # ------------------------------------------------------------ selección

    def selected_code(self) -> str | None:
        """El ID corto de la fila seleccionada en la tabla de tipos."""
        row = self.query_one("#md-types", DataTable).cursor_row
        return self._codes[row] if 0 <= row < len(self._codes) else None

    def selected_entry(self) -> CmTypeEntry | None:
        code = self.selected_code()
        if code is None or self._manifest is None:
            return None
        return self._manifest.types.get(code)

    def selected_prop_id(self) -> str | None:
        """El id de wire de la propiedad seleccionada (``clbNonGroup.BAC_CIF``)."""
        entry = self.selected_entry()
        row = self.query_one("#md-props", DataTable).cursor_row
        if entry is None or not 0 <= row < len(entry.properties):
            return None
        return entry.properties[row].id

    # ------------------------------------------------------------ salida

    def _log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        out = self.query_one("#md-log", Log)
        for line in text.splitlines() or [""]:
            out.write_line(f"{stamp}  {line}")

    def _hint(self, text: str) -> None:
        """Un aviso de estado: no se repite si es el mismo de la vez pasada."""
        if text == self._last_hint:
            return
        self._last_hint = text
        self._log(text)

    def log_text(self) -> str:
        """Salida acumulada (para tests y para copiar)."""
        return "\n".join(self.query_one("#md-log", Log).lines)

    def progress_text(self) -> str:
        """144: la línea viva de progreso (vacía si no hay op en curso)."""
        return str(self.query_one("#md-progress", Static).renderable)

    def _show_progress(self, event: SyncProgress) -> None:
        """Pinta un evento de progreso (hilo UI). Reemplaza la línea previa."""
        self.query_one("#md-progress", Static).update(f"⋯ {event.phase} {event.done}/{event.total}")

    def _on_progress(self, event: SyncProgress) -> None:
        """Callback del servicio: corre en el worker, marshalea con la guarda."""
        self.console._apply_on_ui(self._show_progress, event)

    # ------------------------------------------------------------ worker

    def _run(self, label: str, op: Callable[[], _T], on_ok: Callable[[_T], None]) -> None:
        """Corre *op* en worker thread; los errores van al log, nunca a la UI."""
        self._busy = True
        self._sync_buttons()
        self._log(f"▶ {label}…")

        def done(result: _T | None, error: str | None) -> None:
            self._busy = False
            # En teardown los widgets pueden no estar: nunca reventar el worker.
            with contextlib.suppress(NoMatches):
                self.query_one("#md-progress", Static).update("")
                if error is not None:
                    self._log(f"✘ {label}: {error}")
                else:
                    on_ok(result)  # type: ignore[arg-type]
                self._sync_buttons()

        def work() -> None:
            try:
                result: _T | None = op()
                error: str | None = None
            except Exception as exc:  # noqa: BLE001 — el worker nunca revienta la UI
                result, error = None, f"{type(exc).__name__}: {exc}"
            self.console._apply_on_ui(done, result, error)

        self.console.run_worker(work, thread=True, exclusive=False, exit_on_error=False)

    def _ready(self) -> tuple[PipelineConfig, Secrets] | None:
        """Config + credenciales para hablar con el servidor, o ``None`` con aviso."""
        if self._busy or self._store() is None:
            return None
        if not self.console.state.creds_ready(required=()):
            self._log("sin credenciales CMIS frescas — cargalas y probalas en [2] CREDENCIALES")
            return None
        # 127: la config EFECTIVA — el techo de ancho de banda de [3] también
        # rige para el descubrimiento, que baja 368 definiciones de tipo.
        return self.console.effective_config(), self.console.state.creds.to_secrets()

    def _discovery(self, config: PipelineConfig, secrets: Secrets) -> TypeDiscoveryService:
        return TypeDiscoveryService(build_uploader(config, secrets))

    def _fetch_live(self, config: PipelineConfig, secrets: Secrets) -> CmTypeManifest:
        """La foto fresca del servidor, sin verificar carpetas."""
        return self._discovery(config, secrets).fetch_live(
            service_url=config.cmis.base_url,
            repository_id=config.cmis.repo_id,
            on_progress=self._on_progress,
        )

    # ------------------------------------------------------------ acciones

    def discover(self) -> None:
        """Descubre de cero. Con tipos ya firmados se planta: eso es Actualizar."""
        reviewed = sorted(
            code
            for code, entry in (self._manifest.types.items() if self._manifest else ())
            if entry.reviewed
        )
        if reviewed:
            self._log(
                f"el manifest ya tiene {len(reviewed)} tipo(s) revisado(s) "
                f"({', '.join(reviewed[:5])}): usá Actualizar para traer los cambios "
                "del servidor sin perder las decisiones."
            )
            return
        ready = self._ready()
        if ready is None:
            return
        config, secrets = ready
        self._run(
            "descubrir",
            lambda: self._discovery(config, secrets).discover(
                service_url=config.cmis.base_url,
                repository_id=config.cmis.repo_id,
                verify_folders=True,
                on_progress=self._on_progress,
            ),
            self._discovered,
        )

    def _discovered(self, manifest: CmTypeManifest) -> None:
        if not self._write(manifest):
            return
        folders = [e.folder_ok for e in manifest.types.values()]
        self._log(
            f"✔ descubrir: {len(manifest.types)} tipo(s), "
            f"{len(manifest.without_code)} sin código corto · carpetas "
            f"ok={folders.count(True)} faltan={folders.count(False)} "
            f"sin verificar={folders.count(None)}"
        )
        self._render_types()
        self._sync_buttons()

    def compare(self) -> None:
        """Compara el manifest local contra lo que hoy publica el servidor."""
        local = self._manifest
        ready = self._ready()
        if local is None or ready is None:
            return
        config, secrets = ready
        self._run(
            "comparar",
            lambda: diff_manifest(local, self._fetch_live(config, secrets)),
            self._compared,
        )

    def _compared(self, diff: ManifestDiff) -> None:
        self._log("✔ comparar: sin diferencias" if diff.is_empty else diff.render())

    def update(self) -> None:
        """Mergea la foto del servidor conservando decisiones y carpetas manuales."""
        local = self._manifest
        ready = self._ready()
        if local is None or ready is None:
            return
        config, secrets = ready
        self._run(
            "actualizar",
            lambda: apply_diff(local, self._fetch_live(config, secrets), None),
            lambda merged: self._updated(local, merged),
        )

    def _updated(self, before: CmTypeManifest, merged: CmTypeManifest) -> None:
        if not self._write(merged):
            return
        touched = [
            code
            for code in sorted(merged.types)
            if self._moved(before.types.get(code), merged.types[code])
        ]
        if not touched:
            self._log("✔ actualizar: sin cambios")
        else:
            self._log(f"✔ actualizar: {len(touched)} tipo(s) — {', '.join(touched[:12])}")
            for code in touched[:12]:
                for line in merged.types[code].changes:
                    self._log(f"  {code}: {line}")
        self._render_types()
        self._sync_buttons()

    @staticmethod
    def _moved(before: CmTypeEntry | None, after: CmTypeEntry) -> bool:
        """``True`` sii el merge tocó este tipo (nuevo, con cambios o ausente)."""
        if before is None:
            return True
        return (
            before.changes != after.changes
            or before.reviewed != after.reviewed
            or before.missing_on_server != after.missing_on_server
        )

    def verify(self) -> None:
        """145 REQ-005 offline: cruza manifest ↔ YAML ↔ ``MapeoRVI_CM.csv``."""
        manifest = self._manifest
        if manifest is None or self._busy:
            return
        config = self.console.config
        self._run(
            "verificar YAML",
            lambda: run_manifest_check(
                build_mapping_service(config.mapping),
                manifest,
                build_metadata_config(config.metadata).field_sources,
            ),
            lambda report: self._log(report.render()),
        )

    # ------------------------------------------------------------ edición

    def _write(self, manifest: CmTypeManifest) -> bool:
        """Guarda el manifest en el acto (store atómico). ``False`` si falló."""
        store = self._store()
        if store is None:
            return False
        try:
            store.save(manifest)
        except OSError as exc:
            self._log(f"✘ no se pudo guardar el manifest: {exc}")
            return False
        self._manifest = manifest
        return True

    def _save(self, manifest: CmTypeManifest, message: str) -> None:
        if self._write(manifest):
            self._log(f"✔ {message}")
            self._repaint()

    def action_toggle_decision(self) -> None:
        """``espacio``: alterna ``usar``/``omitir`` en la propiedad seleccionada."""
        code, prop_id = self.selected_code(), self.selected_prop_id()
        entry = self.selected_entry()
        if self._manifest is None or code is None or prop_id is None or entry is None:
            return
        decision = DECISION_OMIT if entry.decisions.get(prop_id) == DECISION_USE else DECISION_USE
        self._save(
            set_decision(self._manifest, code, prop_id, decision),
            f"{code}/{canonical_name(prop_id)} → {decision}",
        )

    def action_mark_reviewed(self) -> None:
        """``↵``: el operador se hace cargo del tipo (limpia sus cambios)."""
        code = self.selected_code()
        if self._manifest is None or code is None:
            return
        self._save(mark_reviewed(self._manifest, code), f"{code} marcado como revisado")

    def action_focus_folder(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#md-folder", Input).focus()

    # ------------------------------------------------------------ eventos

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "md-discover": self.discover,
            "md-diff": self.compare,
            "md-update": self.update,
            "md-check": self.verify,
        }
        handler = handlers.get(event.button.id or "")
        if handler is not None:
            event.stop()
            handler()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Mover el cursor de tipos cambia QUÉ propiedades se muestran.
        if event.data_table.id == "md-types":
            self._render_props()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # ``↵`` sobre la tabla de tipos: la DataTable se come la tecla antes
        # de que llegue al BINDING del pane, así que se atiende el mensaje.
        if event.data_table.id == "md-types":
            event.stop()
            self.action_mark_reviewed()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "md-folder":
            return
        event.stop()
        code = self.selected_code()
        folder = event.value.strip()
        if self._manifest is None or code is None:
            return
        if not folder:
            self.console.notify("La carpeta no puede quedar vacía", severity="warning")
            return
        self._save(set_folder(self._manifest, code, folder), f"{code}: carpeta manual {folder}")
