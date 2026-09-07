"""Panel CORRER de la consola (124) — el launcher con su cadena de guardas.

127: el pipeline se ELIGE acá (csv / rvabrep / local_scan / single_doc)
con sus parámetros, y se aplica como override de sesión del bloque
``trigger`` — el YAML no se toca. Resume solo cuando el modo EFECTIVO
es batched y el batch tiene trabajo pendiente (C1/A4); gate del doctor
propio de la consola (C2); interlock PRD tipeado; aviso de corridas
in_progress en el tracking (A6).
"""

from __future__ import annotations

__all__ = ["RunPane"]

from typing import TYPE_CHECKING

from pydantic import ValidationError
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static

from cmcourier.cli.console.overrides import TriggerOverride, apply_overrides
from cmcourier.cli.console.runner import LaunchSpec
from cmcourier.config.schema import (
    CsvTriggerConfig,
    LocalScanTriggerConfig,
    PipelineConfig,
    RvabrepFiltersModel,
    RvabrepTriggerConfig,
    SingleDocTriggerConfig,
)
from cmcourier.services.duration import parse_duration

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

_KINDS: list[tuple[str, str]] = [
    ("csv — lista de triggers en un CSV", "csv"),
    ("rvabrep — directo desde RVABREP (con filtros)", "rvabrep"),
    ("local_scan — escanear una carpeta local", "local_scan"),
    ("single_doc — un solo documento (diagnóstico)", "single_doc"),
]
_KIND_BOX = {
    "csv": "kind-csv",
    "rvabrep": "kind-rvabrep",
    "local_scan": "kind-scan",
    "single_doc": "kind-single",
}


def _csv_list(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


class RunPane(VerticalScroll):
    # VerticalScroll: con el selector de pipeline (127) el formulario supera
    # las ~24 filas y `#run-launch` se iba de la pantalla (antagonista 128).
    DEFAULT_CSS = """
    RunPane { padding: 1 2; }
    RunPane Grid { grid-size: 2; grid-gutter: 1 2; height: auto; }
    RunPane .card { border: solid $surface-lighten-2; padding: 1 2; height: auto; }
    RunPane .card-title { text-style: bold; margin-bottom: 1; }
    RunPane .frow { height: auto; margin-bottom: 1; }
    RunPane .frow Label { width: 16; color: $text-muted; padding-top: 1; }
    RunPane .frow Input { width: 34; }
    RunPane .frow Select { width: 44; }
    RunPane .kindbox { height: auto; }
    RunPane #run-summary { color: $text; }
    RunPane .guard { color: $warning; height: auto; margin-top: 1; }
    RunPane .launchrow { height: auto; margin-top: 1; align-horizontal: right; }
    """

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console
        self._trigger_error = ""

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yaml_trigger = self.console.config.trigger
        with Grid():
            with Vertical(classes="card"):
                yield Static("Qué correr", classes="card-title")
                with Horizontal(classes="frow"):
                    yield Label("pipeline")
                    yield Select(_KINDS, value=yaml_trigger.kind, id="run-kind", allow_blank=False)
                yield from self._compose_kind_boxes()
                with Horizontal(classes="frow", id="row-mode"):
                    yield Label("modo")
                    yield Select(
                        [("corrida nueva", "nueva"), ("reanudar un batch", "resume")],
                        value="nueva",
                        id="run-mode",
                        allow_blank=False,
                    )
                with Horizontal(classes="frow", id="row-batch"):
                    yield Label("batch")
                    yield Select([("—", "")], value="", id="run-batch", allow_blank=False)
                with Horizontal(classes="frow", id="row-total"):
                    yield Label("--total")
                    yield Input(placeholder="vacío: todo el origen", id="run-total")
                with Horizontal(classes="frow"):
                    yield Label("--max-duration")
                    yield Input(placeholder="ej. 30m, 1h30m — vacío: sin límite", id="run-dur")
            with Vertical(classes="card"):
                yield Static("Config efectiva (yaml + overrides aplicados)", classes="card-title")
                yield Static("", id="run-summary")
                yield Static("", classes="guard", id="run-guard", markup=False)
        with Horizontal(classes="launchrow"):
            yield Button("▶ lanzar (r)", variant="primary", id="run-launch")

    def _compose_kind_boxes(self) -> ComposeResult:
        """Los parámetros de cada pipeline; solo el elegido se muestra.
        Los de csv se pre-cargan del YAML cuando el YAML es csv."""
        yt = self.console.config.trigger
        csv = yt if isinstance(yt, CsvTriggerConfig) else None
        with Vertical(id="kind-csv", classes="kindbox"):
            yield self._row("csv_path", "run-csv-path", str(csv.csv_path) if csv else "")
            yield self._row(
                "col. shortname", "run-csv-shortname", csv.shortname_column if csv else "ShortName"
            )
            yield self._row("col. cif", "run-csv-cif", csv.cif_column if csv else "CIF")
            yield self._row(
                "col. system", "run-csv-system", csv.system_id_column if csv else "SystemID"
            )
        rv = yt if isinstance(yt, RvabrepTriggerConfig) else None
        with Vertical(id="kind-rvabrep", classes="kindbox"):
            yield self._row(
                "systems",
                "run-rv-systems",
                ", ".join(rv.filters.systems) if rv else "",
                placeholder="separados por coma — vacío: todos",
            )
            yield self._row(
                "document_types",
                "run-rv-doctypes",
                ", ".join(rv.filters.document_types) if rv else "",
                placeholder="separados por coma — vacío: todos",
            )
        scan = yt if isinstance(yt, LocalScanTriggerConfig) else None
        with Vertical(id="kind-scan", classes="kindbox"):
            yield self._row("scan_path", "run-scan-path", str(scan.scan_path) if scan else "")
            with Horizontal(classes="frow"):
                yield Label("recursive")
                yield Select(
                    [("no — solo la raíz", "false"), ("sí — todos los subdirectorios", "true")],
                    value="true" if scan and scan.recursive else "false",
                    id="run-scan-recursive",
                    allow_blank=False,
                )
        with Vertical(id="kind-single", classes="kindbox"):
            yield self._row("shortname", "run-shortname", "")
            yield self._row("system", "run-system", "")
            yield self._row("cif (opcional)", "run-cif", "")

    @staticmethod
    def _row(label: str, wid: str, value: str, *, placeholder: str = "") -> Horizontal:
        return Horizontal(
            Label(label), Input(value=value, placeholder=placeholder, id=wid), classes="frow"
        )

    def on_mount(self) -> None:
        self._sync_trigger()
        self.refresh_summary()

    # ------------------------------------------------------------ pipeline

    def current_kind(self) -> str:
        return str(self.query_one("#run-kind", Select).value)

    def _inp(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _required_path(self, wid: str, field: str) -> str:
        # ``Path("")`` es ``.`` — el cwd EXISTE y validaría como carpeta.
        # Un path vacío tiene que ser un error, no un escaneo silencioso.
        raw = self._inp(wid)
        if not raw:
            raise ValueError(f"{field}: requerido")
        return raw

    def _build_trigger(self) -> TriggerOverride:
        """Arma el bloque ``trigger`` desde el formulario. Levanta
        ``ValidationError`` (pydantic) o ``ValueError`` si algo no cierra —
        p. ej. el CSV o la carpeta no existen, o el path está vacío."""
        kind = self.current_kind()
        if kind == "csv":
            return CsvTriggerConfig(
                csv_path=self._required_path("run-csv-path", "csv_path"),  # type: ignore[arg-type]
                shortname_column=self._inp("run-csv-shortname") or "ShortName",
                cif_column=self._inp("run-csv-cif") or "CIF",
                system_id_column=self._inp("run-csv-system") or "SystemID",
            )
        if kind == "rvabrep":
            return RvabrepTriggerConfig(
                kind="rvabrep",
                filters=RvabrepFiltersModel(
                    systems=_csv_list(self._inp("run-rv-systems")),
                    document_types=_csv_list(self._inp("run-rv-doctypes")),
                ),
            )
        if kind == "local_scan":
            return LocalScanTriggerConfig(
                kind="local_scan",
                scan_path=self._required_path("run-scan-path", "scan_path"),  # type: ignore[arg-type]
                recursive=str(self.query_one("#run-scan-recursive", Select).value) == "true",
            )
        return SingleDocTriggerConfig(kind="single_doc")

    def _sync_trigger(self) -> None:
        """Muestra las filas del kind elegido y aplica el override si valida.
        Si NO valida, lo aplicado no cambia (build_spec vuelve a validar)."""
        kind = self.current_kind()
        for k, box in _KIND_BOX.items():
            self.query_one(f"#{box}").display = k == kind
        try:
            trigger: TriggerOverride | None = self._build_trigger()
        except ValidationError as exc:
            err = exc.errors()[0]
            loc = ".".join(str(p) for p in err["loc"]) or kind
            self._trigger_error = f"pipeline {kind} · {loc}: {err['msg']}"
            return
        except ValueError as exc:
            self._trigger_error = f"pipeline {kind} · {exc}"
            return
        self._trigger_error = ""
        if trigger == self.console.config.trigger:
            trigger = None
        if self.console.state.set_trigger_override(trigger):
            self.console.refresh_status()

    # ------------------------------------------------------------ resumen

    def effective_mode(self) -> str:
        return apply_overrides(self.console.config, self.console.state.overrides).processing.mode

    def refresh_summary(self) -> None:
        console = self.console
        eff = apply_overrides(console.config, console.state.overrides)
        single = self.current_kind() == "single_doc"
        resume_allowed = eff.processing.mode == "batched" and not single
        self.query_one("#row-mode").display = resume_allowed
        is_resume = resume_allowed and self.query_one("#run-mode", Select).value == "resume"
        self.query_one("#row-batch").display = is_resume
        self.query_one("#row-total").display = not (is_resume or single)
        if is_resume:
            self._load_resumables()
        required = eff.required_aliases()
        creds_ok = console.state.creds_ready(required=required)
        self.query_one("#run-summary", Static).update(
            "\n".join(self._summary_lines(eff, creds_ok, required, resume_allowed))
        )
        guards = []
        if self._trigger_error:
            guards.append(f"✘ {self._trigger_error}")
        if not creds_ok:
            guards.append("Faltan credenciales frescas — cargalas en [2].")
        if console.draft_dirty():
            guards.append(
                "Tenés overrides SIN GUARDAR en [3] — la corrida usaría lo aplicado, "
                "no el borrador."
            )
        self.query_one("#run-guard", Static).update("\n".join(guards))

    def _summary_lines(
        self, eff: PipelineConfig, creds_ok: bool, required: tuple[str, ...], resume_allowed: bool
    ) -> list[str]:
        console = self.console
        kind = eff.trigger.kind
        return [
            f"pipeline      {kind}"
            + (" (override)" if console.state.overrides.trigger is not None else " (del YAML)"),
            f"entorno       {'⚠ PRODUCCIÓN' if eff.environment == 'prd' else 'staging'}",
            f"modo          {eff.processing.mode}"
            + (
                f" · bucket {eff.processing.streaming.bucket_size}"
                if eff.processing.mode == "streaming"
                else ""
            )
            + (
                "  (resume no disponible en streaming)"
                if not resume_allowed and eff.processing.mode == "streaming"
                else ""
            ),
            f"workers S5    {eff.cmis.workers}"
            + (" → 50 (AIMD)" if eff.cmis.auto_tune.enabled else " (fijo)"),
            f"prep workers  {eff.processing.prep_workers}",
            f"overrides     {console.state.overrides.summary()}",
            f"credenciales  {'✔ listas' if creds_ok else '✘ faltan o vencidas'}"
            + (f" (cmis + {', '.join(required)})" if required else " (solo CMIS)"),
            f"doctor        {console.state.doctor_verdict()}",
        ]

    def _load_resumables(self) -> None:
        """A4: reanudable = FAILED + PENDING > 0, no el estado del batch."""
        options: list[tuple[str, str]] = []
        for info, pending in self.console.resumable_batches():
            options.append((f"{info.batch_id[:13]}… · {pending} docs pendientes", info.batch_id))
        sel = self.query_one("#run-batch", Select)
        sel.set_options(options or [("no hay batches con trabajo pendiente", "")])

    # ------------------------------------------------------------ lanzar

    def build_spec(self) -> LaunchSpec | None:
        """Valida los campos y arma el LaunchSpec — None si algo está mal."""
        console = self.console
        self._sync_trigger()  # nunca lanzar con un trigger inválido o viejo
        if self._trigger_error:
            console.notify(self._trigger_error, severity="error")
            self.refresh_summary()
            return None
        dur_raw = self._inp("run-dur")
        max_duration_s = None
        if dur_raw:
            try:
                max_duration_s = parse_duration(dur_raw)
            except ValueError as exc:
                console.notify(f"--max-duration: {exc}", severity="error")
                return None
        if self.current_kind() == "single_doc":
            shortname, system = self._inp("run-shortname"), self._inp("run-system")
            if not shortname or not system:
                console.notify("single-doc requiere shortname y system", severity="error")
                return None
            return LaunchSpec(
                max_duration_s=max_duration_s,
                shortname=shortname,
                system_id=system,
                cif=self._inp("run-cif") or None,
            )
        resume_visible = self.query_one("#row-mode").display
        if resume_visible and self.query_one("#run-mode", Select).value == "resume":
            batch = str(self.query_one("#run-batch", Select).value)
            if not batch:
                console.notify(
                    "Elegí un batch con trabajo pendiente para reanudar", severity="error"
                )
                return None
            return LaunchSpec(max_duration_s=max_duration_s, resume_batch_id=batch)
        total_raw = self._inp("run-total")
        total = None
        if total_raw:
            if not total_raw.isdigit() or int(total_raw) < 1:
                console.notify("--total debe ser un entero ≥ 1", severity="error")
                return None
            total = int(total_raw)
        return LaunchSpec(total=total, max_duration_s=max_duration_s)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-launch":
            self.console.try_launch()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("run-kind", "run-scan-recursive"):
            self._sync_trigger()
        self.refresh_summary()

    def on_input_changed(self, event: Input.Changed) -> None:
        wid = event.input.id or ""
        if wid.startswith(("run-csv-", "run-rv-", "run-scan-")):
            self._sync_trigger()
            self.refresh_summary()

    def preselect_resume(self, batch_id: str) -> None:
        """125: activar modo reanudar con este batch preseleccionado
        (tras un retry desde [7]). Solo tiene efecto si el modo efectivo
        es batched."""
        if self.effective_mode() != "batched":
            self.console.notify(
                "El batch quedó con docs en PENDING, pero el YAML corre en "
                "streaming — para retomarlo, corré con mode: batched",
                severity="warning",
            )
            return
        self.query_one("#run-mode", Select).value = "resume"
        self.refresh_summary()  # recarga las opciones reanudables
        import contextlib

        with contextlib.suppress(Exception):
            self.query_one("#run-batch", Select).value = batch_id
