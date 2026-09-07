"""Panel MONITOR de la consola (125).

Reusa los renderers de :mod:`cmcourier.tui` contra el
``TUIDataProvider`` de la corrida activa. Refresca solo cuando el tab
está visible (lección spec 110). Cancelar con `x` se queda en la
consola para mostrar el resumen de cierre — a diferencia del
``CMCourierTUI`` clásico, que hace ``app.exit()``.
"""

from __future__ import annotations

__all__ = ["MonitorPane"]

from datetime import timedelta
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from cmcourier.tui.bucket_tab import render_bucket
from cmcourier.tui.prep_tab import render_prep
from cmcourier.tui.upload_tab import render_upload

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp
    from cmcourier.tui.data_provider import TUISnapshot


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
        if snap.is_complete:
            state = "completada"
        elif getattr(mgr, "paused", False):
            # 132
            state = (
                "PAUSADA · esperando credenciales CMIS"
                if getattr(mgr, "reauth_pending", False)
                else "PAUSADA"
            )
        else:
            state = "corriendo"
        header = (
            f"[b]batch[/b] {snap.batch_id or '—'}  [b]{state}[/b]  "
            f"[b]elapsed[/b] {int(snap.elapsed_s)}s\n"
            f"[green]subidos {done}[/green]  "
            + (f"[red]fallidos {failed}[/red]  " if failed else f"fallidos {failed}  ")
            + f"salteados {skipped}  {snap.throughput_docs_per_s:.1f} docs/s"
            + self._window_fragment(snap)
            + self._workers_fragment(mgr, snap)
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

    @staticmethod
    def _window_fragment(snap: TUISnapshot) -> str:
        """134: tasa de los últimos 60 s + ETA de corrida (sólo con total)."""
        rate = snap.throughput_window_docs_per_s
        frag = f" · {rate:.1f} docs/s (60 s)" if rate is not None else " · — docs/s (60 s)"
        if snap.is_complete:
            return frag
        if snap.planned_total is None:
            return frag + " · ETA — (sin total)"
        if snap.eta_run_s is None:
            return frag + f" · ETA — de {snap.planned_total}"
        eta = timedelta(seconds=int(snap.eta_run_s))
        return frag + f" · ETA {eta} de {snap.planned_total}"

    @staticmethod
    def _workers_fragment(mgr: object, snap: TUISnapshot) -> str:
        """133: ``workers <en uso>/<cap efectivo>`` y el techo manual si lo hay."""
        # El pipeline sabe el presupuesto en ambos modos (single/dual-lane);
        # ``snap.pool_capacity`` sólo ve el semáforo único.
        effective = getattr(mgr, "effective_workers", None)
        capacity = int(effective if effective is not None else snap.pool_capacity)
        if capacity <= 0:
            return ""
        frag = f"  workers {int(getattr(snap, 'pool_in_use', 0))}/{capacity}"
        cap = getattr(mgr, "worker_cap", None)
        if cap is not None:
            frag += f" · [yellow]techo manual {cap}[/yellow]"
        return frag

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
