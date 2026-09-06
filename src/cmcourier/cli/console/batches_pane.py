"""Panel BATCHES de la consola (125).

DataTable navegable por teclado (Textual no soporta botones en celda):
``↑↓`` fila, ``enter`` detalle, ``R`` retry fallidos, ``E`` export.
Reanudable = FAILED + PENDING > 0 (no el estado del batch).
"""

from __future__ import annotations

__all__ = ["BatchesPane"]

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Input, Static

from cmcourier.domain.models import BatchDetails, BatchInfo

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

_COLS = ("batch", "fecha", "por", "entorno", "subidos", "fallidos", "salteados", "estado")


class BatchesPane(Vertical):
    DEFAULT_CSS = """
    BatchesPane { padding: 1 2; }
    BatchesPane .intro { color: $text-muted; margin-bottom: 1; }
    BatchesPane #bt-filter { margin-bottom: 1; width: 48; }
    BatchesPane #bt-table { height: 1fr; }
    BatchesPane #bt-detail { height: auto; border: solid $surface-lighten-2;
                             padding: 1 1; margin-top: 1; }
    BatchesPane #bt-audit { color: $text-muted; }
    """

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console
        self._rows: list[BatchInfo] = []

    def compose(self) -> ComposeResult:
        yield Static(
            "↑↓ navegar · Enter detalle · R reintentar fallidos · "
            "E exportar. Reanudable = tiene docs fallidos o pendientes.",
            classes="intro",
        )
        yield Input(placeholder="filtrar por id, operador o entorno…", id="bt-filter")
        table: DataTable[str] = DataTable(id="bt-table", cursor_type="row", zebra_stripes=True)
        yield table
        yield Static("", id="bt-detail")

    def on_mount(self) -> None:
        table = self.query_one("#bt-table", DataTable)
        table.add_columns(*_COLS)
        self.query_one("#bt-detail", Static).display = False
        self.reload()

    # ------------------------------------------------------------ datos

    def reload(self) -> None:
        table = self.query_one("#bt-table", DataTable)
        table.clear()
        q = self.query_one("#bt-filter", Input).value.strip().lower()
        store = self.console.batches_store()
        self._rows = []
        for info in store.list_batches():
            audit = store.batch_audit(info.batch_id) if hasattr(store, "batch_audit") else {}
            hay = f"{info.batch_id} {audit.get('operator', '')} {audit.get('environment', '')}"
            if q and q not in hay.lower():
                continue
            self._rows.append(info)
            counts = self._counts(store.get_batch_details(info.batch_id))
            table.add_row(
                info.batch_id[:13] + "…",
                info.started_at.strftime("%m-%d %H:%M"),
                audit.get("operator", "—"),
                audit.get("environment", "—"),
                str(counts["done"]),
                str(counts["failed"]),
                str(counts["pending"]),
                audit.get("outcome") or info.status,
                key=info.batch_id,
            )

    def _counts(self, details: BatchDetails | None) -> dict[str, int]:
        out = {"done": 0, "failed": 0, "pending": 0}
        if details is None:
            return out
        for states in details.stage_counts.values():
            out["failed"] += states.get("FAILED", 0)
            out["pending"] += states.get("PENDING", 0)
        out["done"] = details.stage_counts.get("S5", {}).get("DONE", 0)
        return out

    # ------------------------------------------------------------ acciones

    def on_input_changed(self, _: Input.Changed) -> None:
        self.reload()

    def selected_batch(self) -> BatchInfo | None:
        table = self.query_one("#bt-table", DataTable)
        if not self._rows or table.cursor_row is None:
            return None
        idx = table.cursor_row
        return self._rows[idx] if 0 <= idx < len(self._rows) else None

    def show_detail(self) -> None:
        info = self.selected_batch()
        if info is None:
            return
        store = self.console.batches_store()
        details = store.get_batch_details(info.batch_id)
        audit = store.batch_audit(info.batch_id) if hasattr(store, "batch_audit") else {}
        panel = self.query_one("#bt-detail", Static)
        panel.display = True
        lines = [f"[b]{info.batch_id}[/b]"]
        if audit:
            lines.append(
                "auditoría: "
                + " · ".join(f"{k}={v}" for k, v in audit.items() if v and k != "config_hash")
            )
        if details and details.failed_records:
            lines.append("")
            lines.append(f"fallidos ({len(details.failed_records)}):")
            for fr in details.failed_records[:8]:
                lines.append(f"  {fr.txn_num}  {fr.status}  {fr.error_message[:60]}")
            if len(details.failed_records) > 8:
                lines.append(f"  … y {len(details.failed_records) - 8} más")
        else:
            lines.append("sin documentos fallidos")
        panel.update("\n".join(lines))

    def is_resumable(self, info: BatchInfo) -> bool:
        counts = self._counts(self.console.batches_store().get_batch_details(info.batch_id))
        return counts["failed"] + counts["pending"] > 0

    def retry_selected(self) -> None:
        info = self.selected_batch()
        if info is None:
            return
        if not self.is_resumable(info):
            self.console.notify("Ese batch no tiene fallidos ni pendientes", severity="warning")
            return
        counts = self._counts(self.console.batches_store().get_batch_details(info.batch_id))
        n = counts["failed"]
        self.console.confirm(
            title="Reintentar fallidos",
            body=(
                f"Se resetean {n} documentos de {info.batch_id} a *_PENDING. "
                "La próxima corrida los retoma sin re-subir lo ya migrado."
            ),
            yes="resetear",
            no="cancelar",
            confirm_text=info.batch_id if n > 100 else None,
            danger=n > 100,
            cb=lambda ok: self._do_retry(info) if ok else None,
        )

    def _do_retry(self, info: BatchInfo) -> None:
        store = self.console.batches_store()
        try:
            reset = store.retry_failed(info.batch_id)
        except Exception as exc:  # noqa: BLE001
            self.console.notify(f"retry_failed falló: {exc}", severity="error")
            return
        self.console.notify(f"{reset} documentos reseteados a PENDING en {info.batch_id}")
        self.reload()
        self.console.route_resume(info.batch_id)

    def export_selected(self) -> None:
        info = self.selected_batch()
        if info is None:
            return
        self.console.notify(
            f"Usá: cmcourier batch export-report {info.batch_id} — el CSV lleva "
            "metadata de documentos, tratalo como confidencial"
        )
