"""Panel SYNC de la consola (128): SQLite ↔ AS400 NIARVILOG a mano.

Tres bloques sobre :mod:`cmcourier.cli.sync_ops` (la misma lógica que
``cmcourier sync``): estado, recuperar (dry-run obligatorio antes de
aplicar) y resolver una divergencia por TRNNUM. Todo corre en worker
thread; el resultado se acumula en un panel de salida.
"""

from __future__ import annotations

__all__ = ["SyncPane"]

import contextlib
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, TypeVar

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Log, Select, Static

from cmcourier.adapters.tracking.as400_niarvilog import As400CoordinationError
from cmcourier.cli.sync_ops import (
    SyncOpError,
    sync_recover,
    sync_resolve,
    sync_status,
    sync_unavailable_reason,
)
from cmcourier.services.recovery import RecoveryResult

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

_T = TypeVar("_T")
_Prefer = Literal["as400", "local"]

_PREFER_OPTIONS = [
    ("as400 manda (read-only)", "as400"),
    ("local manda (escribe en AS400)", "local"),
]
_MAX_OUT_LINES = 200


class SyncPane(VerticalScroll):
    """``VerticalScroll``: los tres bloques + la salida superan las 24
    filas de una terminal chica (hallazgo del antagonista 128)."""

    DEFAULT_CSS = """
    SyncPane { padding: 1 2; }
    SyncPane .intro { color: $text-muted; margin-bottom: 1; }
    SyncPane #sy-avail { margin-bottom: 1; }
    SyncPane #sy-avail.off { color: $warning; }
    SyncPane .block { height: auto; border: solid $surface-lighten-2; padding: 0 1;
                      margin-bottom: 1; }
    SyncPane .block .title { text-style: bold; }
    SyncPane .frow { height: 3; }
    SyncPane .frow Input { width: 34; }
    SyncPane .frow Select { width: 40; }
    SyncPane .frow Button { margin-left: 1; }
    SyncPane #sy-out { height: 10; border: solid $surface-lighten-2; padding: 0 1; }
    """

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console
        self._available = False
        self._busy = False
        # E4: `aplicar` sólo tras un dry-run del MISMO batch_id con filas.
        self._dry_batch: str | None = None
        self._dry_rows = 0

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static(
            "Sincronización manual del tracking SQLite con NIARVILOG (AS400). "
            "Misma lógica que `cmcourier sync status | recover | resolve`.",
            classes="intro",
        )
        yield Static("", id="sy-avail", markup=False)
        with Vertical(classes="block"):
            yield Static("ESTADO — cleanup de in_progress vencidos + conectividad", classes="title")
            with Horizontal(classes="frow"):
                yield Button("estado (s)", id="sy-status", variant="primary")
        with Vertical(classes="block"):
            yield Static(
                "RECUPERAR — filas faltantes en NIARVILOG para docs ya subidos (099). "
                "Simular primero; aplicar recién después.",
                classes="title",
            )
            with Horizontal(classes="frow"):
                yield Input(placeholder="batch_id (vacío = todo el tracking)", id="sy-batch")
                yield Button("simular", id="sy-dry")
                yield Button("aplicar", id="sy-apply", variant="error", disabled=True)
        with Vertical(classes="block"):
            yield Static("RESOLVER — una divergencia por TRNNUM", classes="title")
            with Horizontal(classes="frow"):
                yield Input(placeholder="txn (TRNNUM)", id="sy-txn")
                yield Select(_PREFER_OPTIONS, value="as400", allow_blank=False, id="sy-prefer")
            with Horizontal(classes="frow"):
                yield Input(placeholder="cm_object_id (solo con local)", id="sy-objid")
                yield Button("resolver", id="sy-resolve")
        # ``Log``: texto plano (sin markup), auto-scroll al final y tope de líneas.
        yield Log(id="sy-out", max_lines=_MAX_OUT_LINES, auto_scroll=True)

    def on_mount(self) -> None:
        self._sync_objid_visibility()
        self.refresh_availability()

    # ------------------------------------------------------------ disponibilidad

    def refresh_availability(self) -> None:
        """Re-evalúa YAML + credenciales de sesión (se llama al activar la tab)."""
        reason = sync_unavailable_reason(self.console.config, self.console.state.creds.to_secrets())
        self._available = reason is None
        avail = self.query_one("#sy-avail", Static)
        if reason is None:
            sync_cfg = self.console.config.tracking.as400_sync
            avail.update(f"✔ sync habilitado · {sync_cfg.library}.{sync_cfg.table}")
            avail.remove_class("off")
        else:
            hint = "  → cargalas en [2] CREDENCIALES" if "credentials" in reason else ""
            avail.update(f"✘ {reason}{hint}")
            avail.add_class("off")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        enabled = self._available and not self._busy
        for wid in ("#sy-status", "#sy-dry", "#sy-resolve"):
            self.query_one(wid, Button).disabled = not enabled
        self.query_one("#sy-apply", Button).disabled = not (enabled and self._apply_allowed())

    def _apply_allowed(self) -> bool:
        return self._dry_rows > 0 and self._dry_batch == self._batch_id()

    def _batch_id(self) -> str | None:
        return self.query_one("#sy-batch", Input).value.strip() or None

    # ------------------------------------------------------------ salida

    def _log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        out = self.query_one("#sy-out", Log)
        for line in text.splitlines() or [""]:
            out.write_line(f"{stamp}  {line}")

    def output_text(self) -> str:
        """Salida acumulada (para tests y para copiar)."""
        return "\n".join(self.query_one("#sy-out", Log).lines)

    def _run(self, label: str, op: Callable[[], _T], on_ok: Callable[[_T], None]) -> None:
        """Corre `op` en worker thread; errores al panel, nunca a la UI."""
        self._busy = True
        self._sync_buttons()
        self._log(f"▶ {label}…")

        def done(result: _T | None, error: str | None) -> None:
            self._busy = False
            # En teardown los widgets pueden no estar: nunca reventar el worker.
            with contextlib.suppress(NoMatches):
                if error is not None:
                    self._log(f"✘ {label}: {error}")
                else:
                    on_ok(result)  # type: ignore[arg-type]
                self._sync_buttons()

        def work() -> None:
            try:
                result: _T | None = op()
                error: str | None = None
            except SyncOpError as exc:
                result, error = None, str(exc)
            except As400CoordinationError as exc:
                result, error = None, f"AS400 error: {exc}"
            except Exception as exc:  # noqa: BLE001 — el worker nunca revienta la UI
                result, error = None, f"{type(exc).__name__}: {exc}"
            self.console.call_from_thread(done, result, error)

        self.console.run_worker(work, thread=True, exclusive=False)

    # ------------------------------------------------------------ acciones

    def run_status(self) -> None:
        if not self._available or self._busy:
            return
        config, secrets = self.console.config, self.console.state.creds.to_secrets()
        self._run(
            "estado",
            lambda: sync_status(config, secrets),
            lambda r: self._log(f"✔ estado: stale_cleaned={r.stale_cleaned} · AS400 responde"),
        )

    def dry_run_recover(self) -> None:
        if not self._available or self._busy:
            return
        batch_id = self._batch_id()
        config, secrets = self.console.config, self.console.state.creds.to_secrets()

        def ok(result: RecoveryResult) -> None:
            self._dry_batch, self._dry_rows = batch_id, len(result.recovered)
            self._log(self._recover_report(result, apply=False))

        self._run(
            f"simular recover ({batch_id or 'todo'})",
            lambda: sync_recover(config, secrets, batch_id=batch_id, apply=False),
            ok,
        )

    def apply_recover(self) -> None:
        if not self._available or self._busy or not self._apply_allowed():
            return
        batch_id = self._batch_id()
        prd = self.console.config.environment == "prd"
        self.console.confirm(
            title="Aplicar recover en AS400",
            body=(
                f"Se insertan {self._dry_rows} fila(s) en NIARVILOG "
                f"({batch_id or 'todo el tracking'}). Esto ESCRIBE en el AS400."
            ),
            yes="insertar",
            no="cancelar",
            danger=True,
            confirm_text="PRD" if prd else None,
            cb=lambda ok: self._do_apply_recover(batch_id) if ok else None,
        )

    def _do_apply_recover(self, batch_id: str | None) -> None:
        config, secrets = self.console.config, self.console.state.creds.to_secrets()

        def ok(result: RecoveryResult) -> None:
            # Un apply consume el dry-run: para repetir hay que simular de nuevo.
            self._dry_batch, self._dry_rows = None, 0
            self._log(self._recover_report(result, apply=True))

        self._run(
            f"aplicar recover ({batch_id or 'todo'})",
            lambda: sync_recover(config, secrets, batch_id=batch_id, apply=True),
            ok,
        )

    @staticmethod
    def _recover_report(result: RecoveryResult, *, apply: bool) -> str:
        mode = "APPLY" if apply else "DRY-RUN"
        verbo = "recuperadas" if apply else "a recuperar"
        lines = [
            f"✔ recover [{mode}]: {verbo}={len(result.recovered)} "
            f"already_present={len(result.already_present)} "
            f"unrecoverable={len(result.unrecoverable)}"
        ]
        lines.extend(f"  unrecoverable {i.txn_num}: {i.reason}" for i in result.unrecoverable[:8])
        if len(result.unrecoverable) > 8:
            lines.append(f"  … y {len(result.unrecoverable) - 8} más")
        if not apply and result.recovered:
            lines.append("  → `aplicar` quedó habilitado para este batch_id.")
        return "\n".join(lines)

    def resolve(self) -> None:
        if not self._available or self._busy:
            return
        txn = self.query_one("#sy-txn", Input).value.strip()
        prefer = self._prefer()
        cm_object_id = self.query_one("#sy-objid", Input).value.strip() or None
        if not txn:
            self.console.notify("Falta el txn (TRNNUM)", severity="warning")
            return
        if prefer == "local" and not cm_object_id:
            self.console.notify("local manda requiere cm_object_id", severity="warning")
            return
        if prefer == "as400":
            self._do_resolve(txn, prefer, cm_object_id)
            return
        self.console.confirm(
            title="Resolver escribiendo en AS400",
            body=(
                f"UPDATE NIARVILOG {txn}: STSCOD='O', OBJIDN={cm_object_id!r}. "
                "Esto ESCRIBE en el AS400."
            ),
            yes="escribir",
            no="cancelar",
            danger=True,
            confirm_text="PRD" if self.console.config.environment == "prd" else None,
            cb=lambda ok: self._do_resolve(txn, prefer, cm_object_id) if ok else None,
        )

    def _prefer(self) -> _Prefer:
        value = self.query_one("#sy-prefer", Select).value
        return "local" if value == "local" else "as400"

    def _do_resolve(self, txn: str, prefer: _Prefer, cm_object_id: str | None) -> None:
        config, secrets = self.console.config, self.console.state.creds.to_secrets()
        self._run(
            f"resolver {txn} ({prefer})",
            lambda: sync_resolve(
                config, secrets, txn=txn, prefer=prefer, cm_object_id=cm_object_id
            ),
            lambda msg: self._log(f"✔ {msg}"),
        )

    # ------------------------------------------------------------ eventos

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "sy-status": self.run_status,
            "sy-dry": self.dry_run_recover,
            "sy-apply": self.apply_recover,
            "sy-resolve": self.resolve,
        }
        handler = handlers.get(event.button.id or "")
        if handler is not None:
            event.stop()
            handler()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "sy-batch":
            self._sync_buttons()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sy-prefer":
            self._sync_objid_visibility()

    def _sync_objid_visibility(self) -> None:
        # cm_object_id sólo tiene sentido con `local manda`.
        self.query_one("#sy-objid", Input).display = self._prefer() == "local"
