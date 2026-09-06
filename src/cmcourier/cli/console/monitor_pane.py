"""Panel MONITOR de la consola (125).

Reusa los renderers de :mod:`cmcourier.tui` contra el
``TUIDataProvider`` de la corrida activa. Refresca solo cuando el tab
está visible (lección spec 110). Cancelar con `x` se queda en la
consola para mostrar el resumen de cierre — a diferencia del
``CMCourierTUI`` clásico, que hace ``app.exit()``.
"""

from __future__ import annotations

__all__ = ["MonitorPane"]

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from cmcourier.tui.bucket_tab import render_bucket
from cmcourier.tui.prep_tab import render_prep
from cmcourier.tui.upload_tab import render_upload

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp


class MonitorPane(Vertical):
    DEFAULT_CSS = """
    MonitorPane { padding: 1 2; }
    MonitorPane #mon-header { height: auto; margin-bottom: 1; }
    MonitorPane #mon-summary { height: auto; color: $success; margin-bottom: 1; }
    MonitorPane #mon-body { height: 1fr; }
    """

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console

    def compose(self) -> ComposeResult:
        yield Static("No hay corrida activa. Lanzá desde [5] CORRER.", id="mon-header")
        yield Static("", id="mon-summary")
        yield VerticalScroll(Static("", id="mon-body", markup=False))

    def on_mount(self) -> None:
        self.query_one("#mon-summary", Static).display = False
        self.set_interval(0.25, self._tick)

    def _tick(self) -> None:
        # Solo si el tab está visible y hay corrida — el resto del tiempo,
        # cero trabajo (spec 110).
        from textual.widgets import TabbedContent

        try:
            if self.console.q("#tabs", TabbedContent).active != "monitor":
                return
        except Exception:  # noqa: BLE001 — durante el teardown
            return
        self.refresh_monitor()

    def refresh_monitor(self) -> None:
        mgr = self.console.run_manager
        provider = getattr(mgr, "provider", None)
        if provider is None:
            return
        snap = provider.snapshot()
        s5 = snap.stages.get("S5", {}) if snap.stages else {}
        done = int(s5.get("count", 0))
        failed = snap.failed_total
        skipped = snap.s1_filtered
        state = "completada" if snap.is_complete else "corriendo"
        header = (
            f"[b]batch[/b] {snap.batch_id or '—'}  [b]{state}[/b]  "
            f"[b]elapsed[/b] {int(snap.elapsed_s)}s\n"
            f"[green]subidos {done}[/green]  "
            + (f"[red]fallidos {failed}[/red]  " if failed else f"fallidos {failed}  ")
            + f"salteados {skipped}  {snap.throughput_docs_per_s:.1f} docs/s"
        )
        breakdown = getattr(snap, "failures_by_type", None)
        if breakdown:
            header += "\nfallos por tipo: " + " · ".join(
                f"{n}× {t}" for t, n in sorted(breakdown.items(), key=lambda kv: -kv[1])
            )
        header += self._bottleneck_line(snap)
        self.query_one("#mon-header", Static).update(header)
        body = render_prep(snap) + "\n\n" + render_upload(snap)
        if self.console.config.processing.mode == "streaming":
            body += "\n\n" + render_bucket(snap)
        self.query_one("#mon-body", Static).update(body)

    def _bottleneck_line(self, snap: object) -> str:
        stages = getattr(snap, "stages", {}) or {}
        counts = {s: int(d.get("count", 0)) for s, d in stages.items() if s.startswith("S")}
        ordered = [counts.get(f"S{i}", 0) for i in range(1, 6)]
        neck, worst = None, 0
        for i in range(1, len(ordered)):
            gap = ordered[i - 1] - ordered[i]
            if gap > worst and gap > 10:
                worst, neck = gap, f"S{i + 1}"
        return f"\ncuello de botella: {neck}" if neck else ""

    def show_summary(self, text: str) -> None:
        summary = self.query_one("#mon-summary", Static)
        summary.display = True
        summary.update(text)
