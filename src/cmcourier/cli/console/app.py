"""ConsoleApp — el shell Textual de la consola de operación (123).

Fase 1: navegación de 8 tabs con bindings globales (1-8 y F1-F8
`priority`, inmunes al foco en inputs), badge de entorno, ayuda con
leyenda de stages, salida con confirmación, INICIO con la máquina de
estados, y los paneles CREDENCIALES y DOCTOR funcionales. CONFIG /
CORRER / MONITOR / BATCHES llegan en F2/F3.
"""

from __future__ import annotations

__all__ = ["ConsoleApp"]

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Select, Static, TabbedContent, TabPane

from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore
from cmcourier.cli.commands._lock import LockHeldError
from cmcourier.cli.console.batches_pane import BatchesPane
from cmcourier.cli.console.config_pane import ConfigPane
from cmcourier.cli.console.creds_pane import CredsPane, run_single_check
from cmcourier.cli.console.doctor_pane import DoctorPane
from cmcourier.cli.console.monitor_pane import MonitorPane
from cmcourier.cli.console.overrides import apply_overrides
from cmcourier.cli.console.persist import PersistError, persist_overrides
from cmcourier.cli.console.practice_pane import PracticePane
from cmcourier.cli.console.run_pane import RunPane
from cmcourier.cli.console.runner import ConsoleRunManager, LaunchSpec
from cmcourier.cli.console.state import ConsoleState
from cmcourier.cli.console.sync_pane import SyncPane
from cmcourier.cli.console.yaml_pane import YamlPane
from cmcourier.cli.doctor import (
    CHECK_NAMES,
    CheckResult,
    CheckStatus,
    DoctorReport,
    group_of,
    run_doctor,
)
from cmcourier.config.schema import PipelineConfig
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.domain.models import BatchInfo

_TABS = [
    "inicio",
    "credenciales",
    "config",
    "doctor",
    "correr",
    "monitor",
    "batches",
    "sync",
    "yaml",
    # 141: la pestaña del tiro de prueba va AL FINAL — los ids existentes
    # no se mueven, así que ninguna tecla vieja cambia de destino.
    "prueba",
]
# La décima pantalla no puede ser la tecla "10": usa el 0 (y F10).
_TAB_KEYS = [*(str(i + 1) for i in range(9)), "0"]


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

    HELP = """[b $accent]TECLAS GLOBALES[/] (F1–F10 funcionan aun con foco en un campo)
  1-9,0 / F1-F10   cambiar de pantalla     ?   esta ayuda
  q             salir (confirma si hay corrida)   Esc  cerrar modal / soltar foco

[b $accent]POR PANTALLA[/]
  [2] ↵ probar conexión · n nueva conexión (editar/quitar en cada tarjeta)
  [3] a guardar overrides · w escribirlos al YAML
  [4] d correr la selección · ↑↓ navegar · ↵ expandir
  [5] r lanzar                             [6] x cancelar (drain) · p pausar · r reanudar
                                               +/- techo manual de workers (en caliente)
  [7] ↑↓ navegar · ↵ detalle · R retry · E export
  [8] s estado del sync · simular antes de aplicar (recover) · resolver por txn
  [9] v validar · w escribir (backup) · u descartar · connections se edita en [2]
  [0] ↵ validar código · s generar y subir · d borrar el último subido

[b $accent]STAGES S0–S7[/]
  S0/S1 adquirir triggers · indexar RVABREP     S2/S3 mapear tipo CM · resolver metadata
  S4/S5 ensamblar PDF · subir por CMIS          S6/S7 tracking · idempotencia (no re-sube)

[b $accent]CHECKS DEL DOCTOR[/] (selector de [4]: todos · por grupo · de a uno)
{checks}

cerrar: Esc o ?"""

    @staticmethod
    def _checks_block() -> str:
        # 126: la lista sale de CHECK_NAMES, nunca re-tipeada (hallazgo A8).
        return "\n".join(f"  {name:<28} grupo · {group_of(name)}" for name in CHECK_NAMES)

    def compose(self) -> ComposeResult:
        yield Static(self.HELP.format(checks=self._checks_block()), markup=True)


class ConsoleApp(App[None]):
    """La consola. `config` ya validado; `config_path` para el lock (F2)."""

    TITLE = "CMCourier · consola"
    CSS = """
    #topbar { dock: top; height: 1; background: $panel; padding: 0 1; }
    #topbar Static { width: auto; }
    #env-badge.staging { color: $success; text-style: bold; }
    #env-badge.prd { color: $text; background: $error; text-style: bold; }
    #pii-badge { color: $text; background: $error; text-style: bold; margin-left: 1; }
    #top-status { color: $text-muted; }
    #clock { dock: right; color: $text-muted; }
    .placeholder { padding: 2 4; color: $text-muted; }
    #inicio-body { padding: 1 2; }
    """
    BINDINGS = [
        *[
            Binding(key, f"switch_tab('{t}')", t.upper(), show=False)
            for key, t in zip(_TAB_KEYS, _TABS, strict=True)
        ],
        *[
            Binding(f"f{i + 1}", f"switch_tab('{t}')", t.upper(), show=False, priority=True)
            for i, t in enumerate(_TABS)
        ],
        Binding("question_mark", "help", "ayuda"),
        Binding("q", "quit_confirm", "salir"),
        Binding("d", "doctor_run", "doctor", show=False),
        Binding("a", "apply_overrides", "guardar overrides", show=False),
        Binding("w", "persist_overrides", "escribir al YAML", show=False),
        Binding("v", "validate_yaml", "validar YAML", show=False),
        Binding("u", "discard_yaml", "descartar cambios", show=False),
        Binding("n", "new_connection", "nueva conexión", show=False),
        Binding("r", "launch", "lanzar", show=False),
        Binding("x", "cancel_run", "cancelar corrida", show=False),
        Binding("p", "pause_run", "pausar corrida", show=False),
        Binding("plus", "adjust_workers(1)", "+workers", show=False),
        Binding("equals_sign", "adjust_workers(1)", "+workers", show=False),
        Binding("minus", "adjust_workers(-1)", "-workers", show=False),
        Binding("s", "sync_status", "estado sync", show=False),
    ]

    def __init__(
        self, *, config: PipelineConfig, config_path: Path, log_level: str = "WARNING"
    ) -> None:
        super().__init__()
        self.config = config
        self.config_path = config_path
        # `console --log-level`: el runner re-configura la observabilidad en
        # cada lanzamiento (unmask_pii es de proceso) y tiene que respetarlo.
        self.log_level = log_level
        self.state = ConsoleState.for_config(config)
        self.run_manager = ConsoleRunManager(self)
        self._batches_store: SQLiteTrackingStore | None = None

    @property
    def run_active(self) -> bool:
        return self.run_manager.active

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
            yield Static(" PII SIN MÁSCARA ", id="pii-badge")
            yield Static("", id="top-status")
            yield Static("", id="clock")
        with TabbedContent(initial="inicio", id="tabs"):
            with TabPane("1·INICIO", id="inicio"):
                yield Static("", id="inicio-body")
            with TabPane("2·CREDENCIALES", id="credenciales"):
                yield CredsPane(self)
            with TabPane("3·CONFIG", id="config"):
                yield ConfigPane(self)
            with TabPane("4·DOCTOR", id="doctor"):
                yield DoctorPane(self)
            with TabPane("5·CORRER", id="correr"):
                yield RunPane(self)
            with TabPane("6·MONITOR", id="monitor"):
                yield MonitorPane(self)
            with TabPane("7·BATCHES", id="batches"):
                yield BatchesPane(self)
            with TabPane("8·SYNC", id="sync"):
                yield SyncPane(self)
            with TabPane("9·YAML", id="yaml"):
                yield YamlPane(self)
            with TabPane("0·PRUEBA", id="prueba"):
                yield PracticePane(self)
        yield Footer()

    def on_mount(self) -> None:
        # Con un ModalScreen arriba, ``App.query_one`` apunta a la
        # pantalla ACTIVA — cacheamos la base para que el reloj y los
        # refresh no exploten con un confirm abierto.
        self._base_screen = self.screen
        self.set_interval(1.0, self._tick_clock)
        self.on_pii_override(None)
        self.refresh_status()

    def q(self, selector: str, expect_type: type | None = None):  # type: ignore[no-untyped-def]
        """query_one contra la pantalla BASE (inmune a modales)."""
        base = getattr(self, "_base_screen", None) or self.screen
        if expect_type is None:
            return base.query_one(selector)
        return base.query_one(selector, expect_type)

    def _tick_clock(self) -> None:
        # El intervalo puede dispararse una vez más durante el teardown,
        # cuando los widgets ya no están montados — no debe explotar.
        with contextlib.suppress(NoMatches):
            self.q("#clock", Static).update(time.strftime("%H:%M:%S"))

    # ------------------------------------------------------------ navegación

    def action_switch_tab(self, tab: str) -> None:
        # Soltar el foco de cualquier Input antes de cambiar: si un
        # descendiente del tab actual retiene el foco, Textual no muda
        # la vista al nuevo tab (el F-key corre la acción pero el panel
        # no cambia). Reproducido con un campo de contraseña tipeado.
        if isinstance(self.focused, Input):
            self.set_focus(None)
        self.q("#tabs", TabbedContent).active = tab

    def on_tabbed_content_tab_activated(self, _: TabbedContent.TabActivated) -> None:
        """Al activar un tab (tecla o click) su contenido se refresca —
        lección de la spec 110: nada de mirar estado viejo 250 ms."""
        active = self.q("#tabs", TabbedContent).active
        if active == "correr":
            self.q("RunPane", RunPane).refresh_summary()
        elif active == "inicio":
            self._render_inicio()
        elif active == "batches":
            self.q("BatchesPane", BatchesPane).reload()
        elif active == "sync":
            # 128: las credenciales pueden haber cambiado en [2].
            self.q("SyncPane", SyncPane).refresh_availability()
        elif active == "credenciales":
            # 131: los overrides (127) pueden cambiar qué conexiones hacen falta.
            self.call_later(self.q("CredsPane", CredsPane).rebuild_cards)
        elif active == "monitor" and self.run_active:
            self.q("MonitorPane", MonitorPane).refresh_monitor()
        elif active == "yaml":
            # 139: refleja escrituras de [2]/[3]; con cambios pendientes no pisa.
            self.call_later(self.q("YamlPane", YamlPane).reload_if_clean)

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
        """``d`` despacha por tab: [4] corre el grupo del doctor, [0] borra
        el último documento de prueba subido (141)."""
        active = self.q("#tabs", TabbedContent).active
        if active == "doctor":
            self.q("DoctorPane", DoctorPane).run_group()
        elif active == "prueba":
            self.q("PracticePane", PracticePane).request_delete_last()

    def action_apply_overrides(self) -> None:
        if self.q("#tabs", TabbedContent).active == "config":
            self.q("ConfigPane", ConfigPane).apply_draft()

    def action_new_connection(self) -> None:
        """138: ``n`` en [2] abre el alta de conexión (la pane guarda la corrida activa)."""
        if self.q("#tabs", TabbedContent).active == "credenciales":
            self.q("CredsPane", CredsPane).open_new()

    def action_persist_overrides(self) -> None:
        """``w`` despacha por tab: [3] overrides (135), [9] el formulario YAML (139)."""
        active = self.q("#tabs", TabbedContent).active
        if active == "config":
            self.persist_overrides()
        elif active == "yaml":
            self.call_later(self.q("YamlPane", YamlPane).write)

    def action_validate_yaml(self) -> None:
        if self.q("#tabs", TabbedContent).active == "yaml":
            self.q("YamlPane", YamlPane).validate()

    def action_discard_yaml(self) -> None:
        """I5: ``u`` en [9] tira el borrador del formulario — cambiar un `kind`
        se lleva puesto el bloque y no había forma de deshacerlo."""
        if self.q("#tabs", TabbedContent).active == "yaml":
            self.call_later(self.q("YamlPane", YamlPane).discard)

    def persist_overrides(self) -> None:
        """135: escribe los overrides APLICADOS al YAML — con confirmación.

        Sólo lo aplicado con `a`: un borrador sucio no llega al disco.
        El trigger (127) no se persiste. Una corrida activa no cambia.
        """
        pane = self.q("ConfigPane", ConfigPane)
        if pane.draft_dirty():
            self.notify("Primero guardá el borrador con a — después w", severity="error")
            return
        applied = self.state.overrides
        if not applied.has_scalars():
            self.notify("No hay nada aplicado para escribir al YAML", severity="warning")
            return

        def _done(ok: bool) -> None:
            if not ok:
                return
            try:
                result = persist_overrides(self.config_path, self.config, applied)
            except (PersistError, OSError) as exc:
                self.notify(f"No se escribió el YAML: {exc}", severity="error", timeout=8)
                return
            self.config = result.config
            self.state.overrides = applied.cleared()
            self.state.mark_doctor_stale("cambió el YAML")
            pane.refresh_yaml_values()
            self.call_later(self.q("YamlPane", YamlPane).reload_if_clean)
            self.on_pii_override(None)
            self.refresh_status()
            self.notify(
                f"YAML actualizado ({len(result.changed)} claves) · backup en {result.backup_path}",
                timeout=8,
            )

        self.confirm(
            title="Escribir los overrides en el YAML",
            body=(
                f"{self.config_path}\n\n{applied.summary()}\n\n"
                "Se hace un backup config.yaml.bak-<fecha> y se reemplaza el archivo. "
                "Los overrides de sesión se limpian (ya viven en el YAML)."
            ),
            yes="escribir",
            no="cancelar",
            danger=True,
            cb=_done,
        )

    def action_launch(self) -> None:
        active = self.q("#tabs", TabbedContent).active
        if active == "correr":
            self.try_launch()
        elif active == "monitor":
            self.resume_run()  # 132: en [6] la misma tecla reanuda

    def action_sync_status(self) -> None:
        """``s`` despacha por tab: [8] estado del sync, [0] generar y subir
        el documento de prueba (141)."""
        active = self.q("#tabs", TabbedContent).active
        if active == "sync":
            self.q("SyncPane", SyncPane).run_status()
        elif active == "prueba":
            self.q("PracticePane", PracticePane).request_upload()

    def action_cancel_run(self) -> None:
        if self.q("#tabs", TabbedContent).active != "monitor" or not self.run_active:
            return
        self.confirm(
            title="Cancelar la corrida",
            body=(
                "Drain cooperativo: los uploads en vuelo terminan, no se pierde "
                "estado, y el batch queda con su trabajo pendiente auditado."
            ),
            yes="cancelar (drain)",
            no="seguir corriendo",
            cb=lambda ok: self.run_manager.cancel() if ok else None,
        )

    # ------------------------------------------------ 132: pausa / re-auth

    def action_pause_run(self) -> None:
        mgr = self.run_manager
        if self.q("#tabs", TabbedContent).active != "monitor" or not self.run_active:
            return
        if mgr.paused:
            self.notify("La corrida ya está pausada — r para reanudar", severity="warning")
            return
        self.confirm(
            title="Pausar la corrida",
            body=(
                "Pausa cooperativa: los uploads en vuelo terminan; ningún worker toma "
                "trabajo nuevo hasta que reanudes con r. El límite de --max-duration "
                "sigue corriendo mientras está pausada."
            ),
            yes="pausar",
            no="seguir corriendo",
            cb=self._after_pause_confirm,
        )

    def _after_pause_confirm(self, ok: bool) -> None:
        if not ok:
            return
        self.run_manager.pause()
        self._refresh_run_state()

    # ------------------------------------------- 133: techo manual de workers

    def action_adjust_workers(self, delta: int) -> None:
        """``+``/``-`` en [6] con corrida activa. Sin confirmación: es
        reversible y el pipeline lo acota a ``[1, techo del pool]``."""
        if self.q("#tabs", TabbedContent).active != "monitor" or not self.run_active:
            return
        mgr = self.run_manager
        effective = mgr.adjust_workers(delta)
        if effective is None:
            return
        cap = mgr.worker_cap
        ceiling = mgr.pool_ceiling
        # El mensaje dice la CAUSA del efectivo: si tocó el techo del pool
        # no hay más para pedir; si el AIMD lo sostiene por debajo del
        # techo manual, el operador tiene que saber que no fue él.
        if ceiling is not None and effective >= ceiling:
            msg = f"workers: {effective} · techo del pool"
        elif cap is not None and cap > effective:
            msg = f"workers: {effective} (techo manual {cap} · el AIMD sostiene {effective})"
        elif cap is not None:
            msg = f"workers: {effective} (techo manual {cap})"
        else:
            msg = f"workers: {effective}"
        self.notify(msg, timeout=3)
        self._refresh_run_state()

    def resume_run(self) -> None:
        mgr = self.run_manager
        if not self.run_active or not mgr.paused:
            return
        cmis = self.state.conn.get("cmis")
        if mgr.reauth_pending and cmis is not None and cmis.status != "ok":
            self.confirm(
                title="Reanudar sin probar la credencial CMIS",
                body=(
                    "La sesión CMIS fue rechazada y la credencial de [2] no está "
                    "probada OK. Si sigue mal, el próximo 401 vuelve a pausar la "
                    "corrida (o marca el doc como fallido al tercer intento)."
                ),
                yes="reanudar igual",
                no="ir a probarla",
                danger=True,
                cb=self._after_resume_confirm,
            )
            return
        self._do_resume()

    def _after_resume_confirm(self, ok: bool) -> None:
        if ok:
            self._do_resume()
        else:
            self.action_switch_tab("credenciales")

    def _do_resume(self) -> None:
        self.run_manager.resume()
        self.notify("Corrida reanudada", severity="information")
        self._refresh_run_state()

    def on_auth_expired(self, manager: ConsoleRunManager) -> None:
        """132: llega (vía call_from_thread) cuando un worker recibió 401.
        La corrida ya está pausada; acá sólo se guía al operador."""
        # I6: la tarjeta CMIS decía "ok · probada hace 3 min" y `r`
        # reanudaba sin preguntar. El servidor rechazó la sesión: la
        # tarjeta pasa a err hasta que se vuelva a probar.
        self.state.record_conn_result(
            "cmis",
            ok=False,
            message="Sesión rechazada por el servidor (401) — probá la credencial nueva.",
        )
        with contextlib.suppress(NoMatches):
            self.q("CredsPane", CredsPane).render_conn("cmis")
        self.notify(
            "Sesión CMIS rechazada (401) — corrida PAUSADA. Cargá la credencial "
            "nueva en [2] y reanudá con r en [6].",
            severity="error",
            timeout=12,
        )
        self._refresh_run_state()
        self.action_switch_tab("credenciales")
        with contextlib.suppress(NoMatches):
            self.q("CredsPane", CredsPane).show_reauth_hint()

    def _refresh_run_state(self) -> None:
        self.refresh_status()
        with contextlib.suppress(NoMatches):
            self.q("MonitorPane", MonitorPane).refresh_monitor()

    def on_key(self, event: events.Key) -> None:
        """Rutea teclas locales a DOCTOR / BATCHES cuando el foco no está
        en un widget de entrada."""
        active = self.q("#tabs", TabbedContent).active
        if isinstance(self.focused, (Input, Select, Button)):
            return
        if active == "doctor":
            self._doctor_keys(event)
        elif active == "batches":
            self._batches_keys(event)

    def _doctor_keys(self, event: events.Key) -> None:
        pane = self.q("DoctorPane", DoctorPane)
        if event.key == "down":
            pane.move_selection(1)
        elif event.key == "up":
            pane.move_selection(-1)
        elif event.key == "enter":
            pane.toggle_selected()
        else:
            return
        event.stop()

    def _batches_keys(self, event: events.Key) -> None:
        pane = self.q("BatchesPane", BatchesPane)
        if event.key == "enter":
            pane.show_detail()
        elif event.key in ("R", "r"):
            pane.retry_selected()
        elif event.key in ("E", "e"):
            pane.export_selected()
        else:
            return
        event.stop()

    def route_resume(self, batch_id: str) -> None:
        """125: tras un retry, saltar al launcher en modo reanudar."""
        run_pane = self.q("RunPane", RunPane)
        self.action_switch_tab("correr")
        run_pane.preselect_resume(batch_id)

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

    def effective_config(self) -> PipelineConfig:
        """127: yaml + overrides de sesión aplicados — lo que realmente corre."""
        return apply_overrides(self.config, self.state.overrides)

    def required_aliases(self) -> tuple[str, ...]:
        """131: aliases de conexión que la config EFECTIVA usa (registro 129).

        A7 (informe UX v2): la derivación sale del YAML — indexing, fuentes
        de metadata y el sync NIARVILOG son ejes independientes del
        `trigger.kind`; ``connection_refs()`` es la única fuente de verdad.
        """
        return self.effective_config().required_aliases()

    # ------------------------------------------------------------ launch (124)

    def draft_dirty(self) -> bool:
        pane = self.q("ConfigPane", ConfigPane)
        assert isinstance(pane, ConfigPane)
        return pane.draft_dirty()

    def batches_store(self) -> SQLiteTrackingStore:
        if self._batches_store is None:
            self._batches_store = SQLiteTrackingStore(self.config.tracking.db_path)
        return self._batches_store

    def resumable_batches(self) -> list[tuple[BatchInfo, int]]:
        """A4: (batch, docs FAILED+PENDING) — solo los que tienen trabajo."""
        store = self.batches_store()
        out: list[tuple[BatchInfo, int]] = []
        for info in store.list_batches()[:20]:
            details = store.get_batch_details(info.batch_id)
            if details is None:
                continue
            # stage_counts = {Sn: {DONE/FAILED/PENDING: n}} (forma fija).
            pending = sum(
                n
                for states in details.stage_counts.values()
                for outcome_name, n in states.items()
                if outcome_name in ("FAILED", "PENDING")
            )
            if pending > 0:
                out.append((info, pending))
        return out

    def try_launch(self) -> None:
        """La cadena de guardas del launcher (spec 124 REQ-004)."""
        if self.run_active:
            self.action_switch_tab("monitor")
            self.notify("Ya hay una corrida activa", severity="warning")
            return
        spec = self.q("RunPane", RunPane).build_spec()
        if spec is None:
            return
        if not self.state.creds_ready(required=self.required_aliases()):
            self.notify("Sin credenciales frescas de sesión — cargalas en [2]", severity="error")
            self.action_switch_tab("credenciales")
            return
        self._guard_doctor(spec)

    def _guard_doctor(self, spec: LaunchSpec) -> None:
        if self.state.doctor_verdict() == "aprobado":
            self._guard_prd(spec)
            return
        self.confirm(
            title=f"Doctor {self.state.doctor_verdict()}",
            body=(
                "El pre-flight no está aprobado para esta config. Lanzar así "
                "SALTEA el doctor embebido y queda registrado en la auditoría "
                "del batch."
            ),
            yes="lanzar igual",
            no="ir a doctor",
            cb=lambda ok: self._guard_prd(spec) if ok else self.action_switch_tab("doctor"),
        )

    def _guard_prd(self, spec: LaunchSpec) -> None:
        if self.config.environment != "prd":
            self._guard_in_progress(spec)
            return
        self.confirm(
            title="Lanzar contra PRODUCCIÓN",
            body=(
                "Vas a escribir en el Content Manager PRODUCTIVO. La acción "
                "queda auditada (operador, config, overrides, doctor)."
            ),
            yes="lanzar en PRD",
            no="cancelar",
            danger=True,
            confirm_text="PRD",
            cb=lambda ok: self._guard_in_progress(spec) if ok else None,
        )

    def _guard_in_progress(self, spec: LaunchSpec) -> None:
        try:
            in_progress = self.batches_store().list_batches(status="in_progress")
        except Exception:  # noqa: BLE001 — el tracking puede no existir aún
            in_progress = []
        if not in_progress:
            self._do_launch(spec)
            return
        ids = ", ".join(b.batch_id[:13] + "…" for b in in_progress[:3])
        self.confirm(
            title="Hay corridas sin cerrar en el tracking",
            body=(
                f"Batches in_progress: {ids}. Puede ser otra estación corriendo "
                "AHORA contra este tracking — o una corrida vieja que crasheó. "
                "Dos corridas concurrentes duplican documentos."
            ),
            yes="lanzar igual",
            no="cancelar",
            danger=True,
            cb=lambda ok: self._do_launch(spec) if ok else None,
        )

    def _do_launch(self, spec: LaunchSpec) -> None:
        try:
            self.run_manager.launch(spec)
        except LockHeldError as exc:
            self.confirm(
                title="Config bloqueada en esta estación",
                body=(
                    f"Otra instancia LOCAL tiene el lock de esta config:\n{exc}\n"
                    "(El lock es por estación — no ve otras máquinas.)"
                ),
                yes="entendido",
                no="cerrar",
                cb=lambda _ok: None,
            )
            return
        except ConfigurationError as exc:
            self.notify(f"ConfigurationError: {exc}", severity="error")
            return
        self.action_switch_tab("monitor")
        self.refresh_status()

    def on_run_finished(self, manager: ConsoleRunManager) -> None:
        # I10: un 401 abierto muere con la corrida — la pista de re-auth no
        # tiene que sobrevivir a la corrida siguiente.
        manager.reauth_pending = False
        outcome = manager.outcome()
        report = manager.report
        done = sum(r.s5_done for r in report.chunks) if report else 0
        failed = sum(r.s5_failed for r in report.chunks) if report else 0
        if manager.exception is not None:
            self.notify(f"La corrida terminó con excepción: {manager.exception}", severity="error")
        else:
            sev: Literal["warning", "information"] = (
                "warning" if (outcome == "cancelled" or failed) else "information"
            )
            self.notify(f"Corrida {outcome}: {done} subidos · {failed} fallidos", severity=sev)
        with contextlib.suppress(NoMatches):
            monitor = self.q("MonitorPane", MonitorPane)
            monitor.refresh_monitor()
            summary = f"■ corrida {outcome} · {done} subidos · {failed} fallidos"
            if failed:
                summary += " · reintentá desde [7] BATCHES"
            monitor.show_summary(summary)
        self.q("RunPane", RunPane).refresh_summary()
        self.refresh_status()

    def on_pii_override(self, unmask: bool | None) -> None:
        effective = self.config.observability.unmask_pii if unmask is None else unmask
        self.q("#pii-badge", Static).display = bool(effective)

    def on_unmount(self) -> None:
        if self._batches_store is not None:
            self._batches_store.close()

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
        # 127: el doctor valida la config EFECTIVA (yaml + overrides, incluido
        # el pipeline elegido en [5]) — la que realmente se va a lanzar.
        effective = apply_overrides(self.config, self.state.overrides)

        def work() -> None:
            try:
                report = run_doctor(effective, self.state.creds.to_secrets(), selected=group)
            except Exception as exc:  # noqa: BLE001 — el worker nunca revienta la UI
                report = DoctorReport(
                    results=(
                        CheckResult(
                            name="doctor",
                            status=CheckStatus.FAIL,
                            message=f"{type(exc).__name__}: {exc}",
                        ),
                    ),
                    elapsed_seconds=0.0,
                )
            self.call_from_thread(apply_cb, report, group)

        self.run_worker(work, thread=True, exclusive=True)

    # ------------------------------------------------------------ estado

    def refresh_status(self) -> None:
        st = self.state
        conn_bits = []
        for alias, conn in st.conn.items():
            mark = {"ok": "✔", "err": "✘", "testing": "…"}.get(conn.status, "·")
            conn_bits.append(f"{alias} {mark}")
        run_bit = ""
        if self.run_active:
            run_bit = " · ⏸ pausada" if self.run_manager.paused else " · ▶ corriendo"
        self.q("#top-status", Static).update(
            f"  {' · '.join(conn_bits)} · doctor: {st.doctor_verdict()}{run_bit}"
        )
        self._render_inicio()
        for pane in (getattr(self, "_base_screen", None) or self.screen).query(RunPane):
            pane.refresh_summary()

    def _render_inicio(self) -> None:
        st = self.state
        env = "⚠ PRODUCCIÓN" if self.config.environment == "prd" else "staging"
        steps = st.next_steps(required=self.required_aliases())
        markup = Content.from_markup
        kind = getattr(self.config.trigger, "kind", "?")
        lines = [
            markup(f"[b]config[/b]  {self.config_path}"),
            markup(
                f"[b]entorno[/b] {env}    [b]trigger[/b] {kind}"
                f"    [b]modo[/b] {self.config.processing.mode}"
            ),
            Content(""),
            markup("[b $accent]SIGUIENTE PASO[/]"),
            *[
                markup("  [green]✔[/green] " if done else "  [b]→[/b] ") + Content(text)
                for done, text in steps
            ],
            Content(""),
            markup("[b $accent]CONEXIONES[/]"),
            # Los mensajes de conexión son cuerpos de error EXTERNOS (`[IBM][...]`,
            # JSON): van como Content plano — `textual.markup.escape` NO cubre tags
            # en mayúscula y el parser sí los traga (bug de Textual 5.3).
            *self._inicio_connection_lines(),
        ]
        self.q("#inicio-body", Static).update(Content("\n").join(lines))

    def _inicio_connection_lines(self) -> list[Content]:
        """131: una línea por conexión (cmis + cada alias de la config efectiva)."""
        st = self.state
        width = max(len(alias) for alias in st.conn)
        lines = []
        for alias, conn in st.conn.items():
            bits = [b for b in ("" if alias == "cmis" else conn.kind, conn.message[:60]) if b]
            lines.append(Content(f"  {alias:<{width}}  {conn.status:<8} {' · '.join(bits)}"))
        if len(st.conn) == 1:
            lines.append(Content("  (esta config no usa conexiones AS400 ni SQL Server)"))
        return lines
