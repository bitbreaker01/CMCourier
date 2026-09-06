"""Panel CORRER de la consola (124) — el launcher con su cadena de guardas.

Pipeline fijado por ``trigger.kind`` del YAML (A7); resume solo cuando
el modo EFECTIVO es batched y el batch tiene trabajo pendiente (C1/A4);
gate del doctor propio de la consola (C2); interlock PRD tipeado;
aviso de corridas in_progress en el tracking (A6).
"""

from __future__ import annotations

__all__ = ["RunPane"]

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import Button, Input, Label, Select, Static

from cmcourier.cli.console.overrides import apply_overrides
from cmcourier.cli.console.runner import LaunchSpec
from cmcourier.services.duration import parse_duration

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp


class RunPane(Vertical):
    DEFAULT_CSS = """
    RunPane { padding: 1 2; }
    RunPane Grid { grid-size: 2; grid-gutter: 1 2; height: auto; }
    RunPane .card { border: solid $surface-lighten-2; padding: 1 2; height: auto; }
    RunPane .card-title { text-style: bold; margin-bottom: 1; }
    RunPane .frow { height: auto; margin-bottom: 1; }
    RunPane .frow Label { width: 16; color: $text-muted; padding-top: 1; }
    RunPane .frow Input { width: 24; }
    RunPane .frow Select { width: 34; }
    RunPane #run-summary { color: $text; }
    RunPane .guard { color: $warning; height: auto; margin-top: 1; }
    RunPane .launchrow { height: auto; margin-top: 1; align-horizontal: right; }
    """

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        kind = getattr(self.console.config.trigger, "kind", "?")
        single = kind == "single_doc"
        with Grid():
            with Vertical(classes="card"):
                yield Static("Qué correr", classes="card-title")
                with Horizontal(classes="frow"):
                    yield Label("pipeline")
                    yield Static(f"{kind} (fijado por el YAML)", id="run-kind")
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
                if single:
                    with Horizontal(classes="frow"):
                        yield Label("shortname")
                        yield Input(id="run-shortname")
                    with Horizontal(classes="frow"):
                        yield Label("system")
                        yield Input(id="run-system")
                    with Horizontal(classes="frow"):
                        yield Label("cif (opcional)")
                        yield Input(id="run-cif")
                else:
                    with Horizontal(classes="frow", id="row-total"):
                        yield Label("--total")
                        yield Input(placeholder="vacío: todo el origen", id="run-total")
                with Horizontal(classes="frow"):
                    yield Label("--max-duration")
                    yield Input(placeholder="ej. 30m, 1h30m — vacío: sin límite", id="run-dur")
            with Vertical(classes="card"):
                yield Static("Config efectiva (yaml + overrides aplicados)", classes="card-title")
                yield Static("", id="run-summary")
                yield Static("", classes="guard", id="run-guard")
        with Horizontal(classes="launchrow"):
            yield Button("▶ lanzar (r)", variant="primary", id="run-launch")

    def on_mount(self) -> None:
        self.refresh_summary()

    # ------------------------------------------------------------ resumen

    def effective_mode(self) -> str:
        return apply_overrides(self.console.config, self.console.state.overrides).processing.mode

    def refresh_summary(self) -> None:
        console = self.console
        eff = apply_overrides(console.config, console.state.overrides)
        resume_allowed = eff.processing.mode == "batched"
        self.query_one("#row-mode").display = resume_allowed
        is_resume = resume_allowed and self.query_one("#run-mode", Select).value == "resume"
        self.query_one("#row-batch").display = is_resume
        row_total = self.query("#row-total")
        if row_total:
            row_total.first().display = not is_resume
        if is_resume:
            self._load_resumables()
        need_as400 = console.as400_required()
        creds_ok = console.state.creds_ready(as400_required=need_as400)
        lines = [
            f"entorno       {'⚠ PRODUCCIÓN' if eff.environment == 'prd' else 'staging'}",
            f"modo          {eff.processing.mode}"
            + (
                f" · bucket {eff.processing.streaming.bucket_size}"
                if eff.processing.mode == "streaming"
                else ""
            )
            + ("  (resume no disponible en streaming)" if not resume_allowed else ""),
            f"workers S5    {eff.cmis.workers}"
            + (" → 50 (AIMD)" if eff.cmis.auto_tune.enabled else " (fijo)"),
            f"prep workers  {eff.processing.prep_workers}",
            f"overrides     {console.state.overrides.summary()}",
            f"credenciales  {'✔ listas' if creds_ok else '✘ faltan o vencidas'}"
            + ("" if need_as400 else " (AS400 no requerida)"),
            f"doctor        {console.state.doctor_verdict()}",
        ]
        self.query_one("#run-summary", Static).update("\n".join(lines))
        guards = []
        if not creds_ok:
            guards.append("Faltan credenciales frescas — cargalas en [2].")
        if console.draft_dirty():
            guards.append(
                "Tenés overrides SIN GUARDAR en [3] — la corrida usaría lo aplicado, "
                "no el borrador."
            )
        self.query_one("#run-guard", Static).update("\n".join(guards))

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
        dur_raw = self.query_one("#run-dur", Input).value.strip()
        max_duration_s = None
        if dur_raw:
            try:
                max_duration_s = parse_duration(dur_raw)
            except ValueError as exc:
                console.notify(f"--max-duration: {exc}", severity="error")
                return None
        kind = getattr(console.config.trigger, "kind", "")
        if kind == "single_doc":
            shortname = self.query_one("#run-shortname", Input).value.strip()
            system = self.query_one("#run-system", Input).value.strip()
            if not shortname or not system:
                console.notify("single-doc requiere shortname y system", severity="error")
                return None
            return LaunchSpec(
                max_duration_s=max_duration_s,
                shortname=shortname,
                system_id=system,
                cif=self.query_one("#run-cif", Input).value.strip() or None,
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
        total_raw = self.query_one("#run-total", Input).value.strip()
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

    def on_select_changed(self, _: Select.Changed) -> None:
        self.refresh_summary()
