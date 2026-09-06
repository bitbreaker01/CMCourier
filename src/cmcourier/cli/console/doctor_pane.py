"""Panel DOCTOR de la consola (123).

Corre los checks reales (`run_doctor`) en un worker thread, con
selector de grupo, lista navegable (↑↓ / Enter expande FAIL/WARN) y
estado *stale* cuando cambian credenciales u overrides.
"""

from __future__ import annotations

__all__ = ["DoctorPane"]

from typing import TYPE_CHECKING, Literal

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Select, Static

from cmcourier.cli.doctor import _CHECK_GROUPS, CheckStatus, DoctorReport

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

# Los grupos REALES del doctor, importados — nunca re-tipeados (informe
# UX v2, hallazgo A8: el mock los tenía mal).
_GROUPS = ["all", *sorted(_CHECK_GROUPS)]
_ICON = {
    CheckStatus.PASS: ("✔", "st-pass"),
    CheckStatus.FAIL: ("✘", "st-fail"),
    CheckStatus.WARN: ("▲", "st-warn"),
    CheckStatus.SKIP: ("·", "st-skip"),
}


class DoctorPane(Vertical):
    DEFAULT_CSS = """
    DoctorPane { padding: 1 2; }
    DoctorPane .intro { color: $text-muted; margin-bottom: 1; }
    DoctorPane .toolbar { height: auto; margin-bottom: 1; }
    DoctorPane .toolbar > * { margin-right: 2; }
    DoctorPane #doc-summary { color: $text-muted; padding-top: 1; }
    DoctorPane .stale { color: $warning; border: solid $warning; padding: 0 1;
                        margin-bottom: 1; height: auto; }
    DoctorPane #doc-results { border: solid $surface-lighten-2; height: 1fr; padding: 0 1; }
    DoctorPane .check-row { height: auto; }
    DoctorPane .check-row.selected { background: $surface-lighten-1; }
    DoctorPane .st-pass { color: $success; }
    DoctorPane .st-fail { color: $error; }
    DoctorPane .st-warn { color: $warning; }
    DoctorPane .st-skip { color: $text-muted; }
    DoctorPane .check-detail { color: $text-muted; padding-left: 4; }
    """

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console
        self.selected = 0
        self.open: set[str] = set()
        self.running = False

    def compose(self) -> ComposeResult:
        yield Static(
            "Los checks de cmcourier doctor, en vivo. d corre el grupo · ↑↓ navega · "
            "Enter expande el detalle · el resultado queda stale si cambian "
            "credenciales u overrides.",
            classes="intro",
        )
        with Horizontal(classes="toolbar"):
            yield Button("correr (d)", variant="primary", id="doc-run")
            yield Select([(g, g) for g in _GROUPS], value="all", id="doc-group", allow_blank=False)
            yield Static("sin correr", id="doc-summary")
        yield Static("", classes="stale", id="doc-stale")
        yield VerticalScroll(id="doc-results")

    def on_mount(self) -> None:
        self.query_one("#doc-stale", Static).display = False
        self.render_results()

    # ------------------------------------------------------------ acciones

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "doc-run":
            self.run_group()

    def run_group(self) -> None:
        if self.running:
            return
        self.running = True
        group = str(self.query_one("#doc-group", Select).value)
        self.query_one("#doc-run", Button).disabled = True
        self.query_one("#doc-summary", Static).update("corriendo…")
        self.console.run_doctor_worker(group, self._apply_report)

    def _apply_report(self, report: DoctorReport, group: str) -> None:
        self.running = False
        self.query_one("#doc-run", Button).disabled = False
        self.console.state.set_doctor_report(report, group=group)
        self.selected = 0
        self.open = {
            r.name for r in report.results if r.status in (CheckStatus.FAIL, CheckStatus.WARN)
        }
        self.render_results()
        self.console.refresh_status()
        sev: Literal["error", "warning", "information"] = (
            "error" if report.has_failures else "warning" if report.warn_count else "information"
        )
        self.console.notify(
            f"Doctor: {report.passed_count} ok · {report.warn_count} warn · "
            f"{report.failed_count} fail",
            severity=sev,
        )

    def move_selection(self, delta: int) -> None:
        report = self.console.state.doctor_report
        if report is None:
            return
        self.selected = max(0, min(len(report.results) - 1, self.selected + delta))
        self.render_results()

    def toggle_selected(self) -> None:
        report = self.console.state.doctor_report
        if report is None:
            return
        name = report.results[self.selected].name
        self.open.symmetric_difference_update({name})
        self.render_results()

    # ------------------------------------------------------------ render

    def render_results(self) -> None:
        state = self.console.state
        stale = self.query_one("#doc-stale", Static)
        stale.display = state.doctor_stale
        if state.doctor_stale:
            stale.update(
                f"▲ Doctor desactualizado — {state.doctor_stale_reason}. "
                "Los resultados de abajo no valen para la config actual."
            )
        box = self.query_one("#doc-results", VerticalScroll)
        box.remove_children()
        report = state.doctor_report
        if report is None:
            box.mount(Static("Todavía no corrió — apretá d.", classes="st-skip"))
            return
        for i, r in enumerate(report.results):
            icon, cls = _ICON[r.status]
            row = Static(
                f" {icon}  {r.name:<28} {r.status.value:<5} {r.message}",
                classes=f"check-row {cls}" + (" selected" if i == self.selected else ""),
                markup=False,
            )
            box.mount(row)
            if r.name in self.open and r.details:
                detail = "\n".join(f"{k}={v}" for k, v in sorted(r.details.items()))
                box.mount(Static(detail, classes="check-detail", markup=False))
        self._render_summary(report)

    def _render_summary(self, report: DoctorReport) -> None:
        parts = [f"{report.passed_count} ok"]
        if report.warn_count:
            parts.append(f"{report.warn_count} warn")
        if report.failed_count:
            parts.append(f"{report.failed_count} fail")
        if report.skip_count:
            parts.append(f"{report.skip_count} salteados")
        suffix = (
            ""
            if self.console.state.doctor_group == "all"
            else (f" · grupo {self.console.state.doctor_group}")
        )
        self.query_one("#doc-summary", Static).update(
            " · ".join(parts) + f" · {report.elapsed_seconds:.1f}s{suffix}"
        )
