"""ConsoleApp — el shell Textual de la consola de operación (123).

Fase 1: navegación de 7 tabs con bindings globales (1-7 y F1-F7
`priority`, inmunes al foco en inputs), badge de entorno, ayuda con
leyenda de stages, salida con confirmación, INICIO con la máquina de
estados, y los paneles CREDENCIALES y DOCTOR funcionales. CONFIG /
CORRER / MONITOR / BATCHES llegan en F2/F3.
"""

from __future__ import annotations

__all__ = ["ConsoleApp"]

import time
from collections.abc import Callable
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Select, Static, TabbedContent, TabPane

from cmcourier.cli.console.creds_pane import CredsPane, run_single_check
from cmcourier.cli.console.doctor_pane import DoctorPane
from cmcourier.cli.console.state import ConsoleState
from cmcourier.cli.doctor import CheckResult, CheckStatus, DoctorReport, run_doctor
from cmcourier.config.schema import PipelineConfig

_TABS = ["inicio", "credenciales", "config", "doctor", "correr", "monitor", "batches"]


class ConfirmScreen(ModalScreen[bool]):
    """Modal de confirmación keyboard-first: foco en la opción SEGURA,
    Esc cancela, y confirmación tipeada opcional para acciones PRD."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Grid { width: 64; height: auto; border: solid $accent;
        background: $panel; padding: 1 2; grid-size: 1; grid-gutter: 1; }
    ConfirmScreen.danger > Grid { border: solid $error; }
    ConfirmScreen .title { text-style: bold; color: $warning; }
    ConfirmScreen.danger .title { color: $error; }
    ConfirmScreen .body { color: $text-muted; }
    ConfirmScreen .btns { height: auto; align-horizontal: right; }
    ConfirmScreen Button { margin-left: 2; }
    """
    BINDINGS = [Binding("escape", "dismiss(False)", "cancelar")]

    def __init__(
        self,
        *,
        title: str,
        body: str,
        yes: str,
        no: str,
        danger: bool = False,
        confirm_text: str | None = None,
    ) -> None:
        super().__init__(classes="danger" if danger else "")
        self._title, self._body = title, body
        self._yes, self._no = yes, no
        self._confirm_text = confirm_text

    def compose(self) -> ComposeResult:
        with Grid():
            yield Static(self._title, classes="title")
            yield Static(self._body, classes="body")
            if self._confirm_text:
                yield Input(placeholder=f"escribí {self._confirm_text} para confirmar", id="ctext")
            with Horizontal(classes="btns"):
                yield Button(self._no, id="no")
                if self._confirm_text:
                    yield Button(self._yes, variant="error", id="yes")
                else:
                    yield Button(self._yes, variant="primary", id="yes")

    def on_mount(self) -> None:
        (self.query_one("#ctext", Input) if self._confirm_text else self.query_one("#no")).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "no":
            self.dismiss(False)
            return
        if (
            self._confirm_text
            and self.query_one("#ctext", Input).value.strip() != self._confirm_text
        ):
            self.app.notify(
                f"Confirmación incorrecta — escribí {self._confirm_text}", severity="error"
            )
            return
        self.dismiss(True)

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self.on_button_pressed(Button.Pressed(self.query_one("#yes", Button)))


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Static { width: 76; max-height: 90%; border: solid $accent;
        background: $panel; padding: 1 2; overflow-y: auto; }
    """
    BINDINGS = [
        Binding("escape", "dismiss", "cerrar"),
        Binding("question_mark", "dismiss", "cerrar"),
    ]

    HELP = """[b $accent]TECLAS GLOBALES[/] (F1–F7 funcionan aun con foco en un campo)
  1-7 / F1-F7   cambiar de pantalla        ?   esta ayuda
  q             salir (confirma si hay corrida)   Esc  cerrar modal / soltar foco

[b $accent]POR PANTALLA[/]
  [2] ↵ probar conexión del formulario     [3] a  guardar overrides
  [4] d correr grupo · ↑↓ navegar · ↵ expandir
  [5] r lanzar                             [6] x  cancelar (drain) · +/- workers
  [7] ↑↓ navegar · ↵ detalle · R retry · E export

[b $accent]STAGES S0–S7[/]
  S0/S1 adquirir triggers · indexar RVABREP     S2/S3 mapear tipo CM · resolver metadata
  S4/S5 ensamblar PDF · subir por CMIS          S6/S7 tracking · idempotencia (no re-sube)

cerrar: Esc o ?"""

    def compose(self) -> ComposeResult:
        yield Static(self.HELP, markup=True)


class ConsoleApp(App[None]):
    """La consola. `config` ya validado; `config_path` para el lock (F2)."""

    TITLE = "CMCourier · consola"
    CSS = """
    #topbar { dock: top; height: 1; background: $panel; padding: 0 1; }
    #topbar Static { width: auto; }
    #env-badge.staging { color: $success; text-style: bold; }
    #env-badge.prd { color: $text; background: $error; text-style: bold; }
    #top-status { color: $text-muted; }
    #clock { dock: right; color: $text-muted; }
    .placeholder { padding: 2 4; color: $text-muted; }
    #inicio-body { padding: 1 2; }
    """
    BINDINGS = [
        *[
            Binding(str(i + 1), f"switch_tab('{t}')", t.upper(), show=False)
            for i, t in enumerate(_TABS)
        ],
        *[
            Binding(f"f{i + 1}", f"switch_tab('{t}')", t.upper(), show=False, priority=True)
            for i, t in enumerate(_TABS)
        ],
        Binding("question_mark", "help", "ayuda"),
        Binding("q", "quit_confirm", "salir"),
        Binding("d", "doctor_run", "doctor", show=False),
    ]

    def __init__(self, *, config: PipelineConfig, config_path: Path) -> None:
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.state = ConsoleState()
        self.run_active = False  # F2 lo maneja de verdad

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        env = self.config.environment
        with Horizontal(id="topbar"):
            yield Static("CMCourier ▸ consola  ", id="app-title")
            yield Static(
                " ⚠ PRODUCCIÓN " if env == "prd" else " STAGING ",
                id="env-badge",
                classes=env,
            )
            yield Static("", id="top-status")
            yield Static("", id="clock")
        with TabbedContent(initial="inicio", id="tabs"):
            with TabPane("1·INICIO", id="inicio"):
                yield Static("", id="inicio-body")
            with TabPane("2·CREDENCIALES", id="credenciales"):
                yield CredsPane(self)
            with TabPane("3·CONFIG", id="config"):
                yield Static(
                    "CONFIG llega en la Fase 2 (overrides de sesión).", classes="placeholder"
                )
            with TabPane("4·DOCTOR", id="doctor"):
                yield DoctorPane(self)
            with TabPane("5·CORRER", id="correr"):
                yield Static("CORRER llega en la Fase 2 (launcher + lock).", classes="placeholder")
            with TabPane("6·MONITOR", id="monitor"):
                yield Static("MONITOR llega en la Fase 3.", classes="placeholder")
            with TabPane("7·BATCHES", id="batches"):
                yield Static("BATCHES llega en la Fase 3.", classes="placeholder")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick_clock)
        self.refresh_status()

    def _tick_clock(self) -> None:
        self.query_one("#clock", Static).update(time.strftime("%H:%M:%S"))

    # ------------------------------------------------------------ navegación

    def action_switch_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_confirm(self) -> None:
        if self.run_active:
            self.confirm(
                title="Salir de la consola",
                body=(
                    "Hay una corrida activa. Salir NO la cancela: sigue en background.\n"
                    "Para cancelarla usá x en [6] MONITOR."
                ),
                yes="salir, que siga",
                no="quedarme",
                cb=lambda ok: self.exit() if ok else None,
            )
            return
        self.confirm(
            title="Salir de la consola",
            body="Las credenciales de sesión se descartan al salir.",
            yes="salir",
            no="quedarme",
            cb=lambda ok: self.exit() if ok else None,
        )

    def action_doctor_run(self) -> None:
        if self.query_one(TabbedContent).active == "doctor":
            self.query_one(DoctorPane).run_group()

    def on_key(self, event: events.Key) -> None:
        """Rutea ↑↓/Enter al panel DOCTOR cuando el foco no está en un widget de entrada."""
        if self.query_one(TabbedContent).active != "doctor":
            return
        if isinstance(self.focused, (Input, Select, Button)):
            return
        pane = self.query_one(DoctorPane)
        if event.key == "down":
            pane.move_selection(1)
        elif event.key == "up":
            pane.move_selection(-1)
        elif event.key == "enter":
            pane.toggle_selected()
        else:
            return
        event.stop()

    # ------------------------------------------------------------ helpers

    def confirm(
        self,
        *,
        title: str,
        body: str,
        yes: str,
        no: str,
        cb: Callable[[bool], object],
        danger: bool = False,
        confirm_text: str | None = None,
    ) -> None:
        screen = ConfirmScreen(
            title=title, body=body, yes=yes, no=no, danger=danger, confirm_text=confirm_text
        )

        def _done(ok: bool | None) -> None:
            cb(bool(ok))

        self.push_screen(screen, _done)

    def as400_required(self) -> bool:
        """AS400 hace falta si alguna fuente/función configurada lo usa.

        A7 (informe UX v2): la derivación sale del YAML — indexing,
        fuentes de metadata y el sync NIARVILOG son ejes independientes
        del `trigger.kind`.
        """
        if getattr(self.config.indexing.source, "kind", "") == "as400":
            return True
        if getattr(self.config.tracking.as400_sync, "enabled", False):
            return True
        sources = getattr(self.config.metadata, "sources", ()) or ()
        return any(getattr(s, "kind", "") == "as400" for s in sources)

    # ------------------------------------------------------------ workers

    def run_check_worker(
        self, which: str, apply_cb: Callable[[str, CheckResult, float], None]
    ) -> None:
        def work() -> None:
            try:
                result, ms = run_single_check(which, self)
            except Exception as exc:  # noqa: BLE001 — el worker nunca revienta la UI
                result, ms = (
                    CheckResult(name=which, status=CheckStatus.FAIL, message=str(exc)),
                    0.0,
                )
            self.call_from_thread(apply_cb, which, result, ms)

        self.run_worker(work, thread=True, exclusive=False)

    def run_doctor_worker(self, group: str, apply_cb: Callable[[DoctorReport, str], None]) -> None:
        def work() -> None:
            report = run_doctor(self.config, self.state.creds.to_secrets(), selected=group)
            self.call_from_thread(apply_cb, report, group)

        self.run_worker(work, thread=True, exclusive=True)

    # ------------------------------------------------------------ estado

    def refresh_status(self) -> None:
        st = self.state
        conn_bits = []
        for which in ("cmis", "as400"):
            s = st.conn[which].status
            mark = {"ok": "✔", "err": "✘", "testing": "…"}.get(s, "·")
            conn_bits.append(f"{which} {mark}")
        self.query_one("#top-status", Static).update(
            f"  {' · '.join(conn_bits)} · doctor: {st.doctor_verdict()}"
        )
        self._render_inicio()

    def _render_inicio(self) -> None:
        st = self.state
        env = "⚠ PRODUCCIÓN" if self.config.environment == "prd" else "staging"
        steps = st.next_steps(as400_required=self.as400_required())
        lines = [
            f"[b]config[/b]  {self.config_path}",
            f"[b]entorno[/b] {env}    [b]trigger[/b] {getattr(self.config.trigger, 'kind', '?')}"
            f"    [b]modo[/b] {self.config.processing.mode}",
            "",
            "[b $accent]SIGUIENTE PASO[/]",
            *[("  [green]✔[/green] " if done else "  [b]→[/b] ") + text for done, text in steps],
            "",
            "[b $accent]CONEXIONES[/]",
            f"  CMIS   {st.conn['cmis'].status:<8} {st.conn['cmis'].message[:70]}",
            f"  AS400  {st.conn['as400'].status:<8} "
            + ("(no requerida para esta config) " if not self.as400_required() else "")
            + st.conn["as400"].message[:60],
        ]
        self.query_one("#inicio-body", Static).update("\n".join(lines))
