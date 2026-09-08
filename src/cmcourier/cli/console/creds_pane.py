"""Panel CREDENCIALES de la consola (123; tarjetas por alias desde 131).

Contrato UX (mock v2 + informes adversariales):
* credenciales solo en memoria; editar invalida la prueba;
* "probar conexión" en worker thread (no bloquea la UI);
* una tarjeta por conexión que la config EFECTIVA usa (registro 129:
  ``<alias> · <kind> · <host>`` + los sitios que la usan) más CMIS, que
  siempre existe;
* las tarjetas ``as400`` llevan contador de intentos y confirmación antes
  del 3° (lockout del perfil en el iSeries); cmis/mssql no;
* toggle mostrar/ocultar contraseña;
* (138) alta / edición / baja de conexiones del registro y "mover al
  registro" una inline, escribiendo el YAML vía ``YamlDocument`` (137)
  con verificación ``load_config`` + backup. Los overrides de sesión
  (127) se conservan: siguen aplicando encima del config nuevo.
"""

from __future__ import annotations

__all__ = ["CredsPane", "run_single_check"]

import contextlib
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Static

from cmcourier.cli.console.config_pane import ConfigPane
from cmcourier.cli.console.connection_edit import (
    ConnectionDraft,
    connection_sites,
    inline_connection,
    plan_delete,
    plan_write,
    prefill_fields,
    write_error_text,
)
from cmcourier.cli.console.connection_edit_screen import ConnectionEditScreen
from cmcourier.cli.console.state import AS400_MAX_TRIES, CMIS_ALIAS, ConnInfo, connection_infos
from cmcourier.cli.console.yaml_pane import YamlPane
from cmcourier.cli.doctor import CheckResult, CheckStatus, check_cmis, check_connection
from cmcourier.config.loader import load_config
from cmcourier.config.schema import INLINE_CONNECTION_ALIAS, PipelineConfig
from cmcourier.config.yaml_doc import (
    Edit,
    WriteResult,
    YamlDocument,
    YamlDocumentError,
    YamlPath,
    YamlWriteError,
    apply_edits,
    to_plain,
)

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

_INPUT_ROLES = ("user", "pass")
_RUN_ACTIVE_MSG = "Hay una corrida activa — las conexiones se editan cuando termine"


def _role_and_alias(widget_id: str) -> tuple[str, str] | None:
    """``user-<alias>`` / ``pass-<alias>`` → (rol, alias); los alias no llevan guiones."""
    role, sep, alias = widget_id.partition("-")
    return (role, alias) if sep and role in _INPUT_ROLES and alias else None


def _alias_of(widget_id: str) -> str | None:
    parsed = _role_and_alias(widget_id)
    return parsed[1] if parsed else None


def _sites_text(info: ConnInfo) -> str:
    """Subtítulo de la tarjeta: dónde se usa — o (138) que todavía no se usa."""
    if not info.sites:
        return "sin uso — asignala a un sitio desde editar"
    return f"usada por: {' · '.join(info.sites)}"


class CredsPane(VerticalScroll):
    """Formulario CMIS + una tarjeta por alias, con prueba de conexión en vivo.

    ``VerticalScroll`` (no ``Vertical``): con 3+ conexiones la grilla de dos
    columnas mide más que la terminal y un ``Vertical`` (overflow oculto)
    dejaba la última tarjeta cortada sin forma de llegar.
    """

    DEFAULT_CSS = """
    CredsPane { padding: 1 2; }
    CredsPane .intro { color: $text-muted; margin-bottom: 1; }
    CredsPane .hint { color: $text-muted; margin-bottom: 1; }
    CredsPane Grid { grid-size: 2; grid-gutter: 1 2; height: auto; }
    CredsPane .card { border: solid $surface-lighten-2; padding: 1 2; height: auto; }
    CredsPane .card-title { text-style: bold; }
    CredsPane .card-sites { color: $text-muted; }
    /* Un Static sin width dentro de un Horizontal llena la fila y empuja
       lo que viene después fuera de la tarjeta (chip de estado, botones).
       Los chips van a `auto`; título, input y contador de intentos toman
       el resto (`1fr`) y se envuelven en vez de empujar. */
    CredsPane .frow Static { width: auto; }
    CredsPane .frow .card-title { width: 1fr; }
    CredsPane .frow .tries { width: 1fr; }
    CredsPane .frow Input { width: 1fr; }
    CredsPane .chip-ok { color: $success; }
    CredsPane .chip-err { color: $error; }
    CredsPane .chip-run { color: $accent; }
    CredsPane .chip-idle { color: $text-muted; }
    CredsPane .msg { color: $text-muted; margin-top: 1; }
    CredsPane .tries { color: $warning; padding: 1 0 0 1; }
    CredsPane Input { margin-top: 0; }
    CredsPane .frow { height: auto; margin-top: 1; }
    CredsPane .toolbar { height: auto; margin-bottom: 1; }
    CredsPane Button { margin-left: 1; }
    """

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console

    def compose(self) -> ComposeResult:
        yield Static(
            "Las credenciales viven solo en memoria — nunca van al YAML ni a disco; "
            "se descartan al salir. Editar un campo invalida la prueba anterior.",
            classes="intro",
        )
        yield Static("", classes="hint", id="creds-hint")
        # 138: las conexiones del registro se dan de alta / editan desde acá.
        yield Horizontal(
            Button("nueva conexión (n)", id="new-conn", classes="conn-edit"), classes="toolbar"
        )
        yield Grid(*(self._card(info) for info in self._infos()), id="creds-grid")

    def on_mount(self) -> None:
        self._render_hint()
        self.render_all()

    # ------------------------------------------------------------ tarjetas

    def _infos(self) -> list[ConnInfo]:
        cmis = ConnInfo(alias=CMIS_ALIAS, kind="cmis", host="", sites=("destino",))
        return [cmis, *connection_infos(self.console.effective_config())]

    async def rebuild_cards(self) -> None:
        """Al entrar a la pestaña: si la config efectiva cambió qué conexiones
        hacen falta, recompone la grilla; si no, sólo re-renderiza.

        Hoy los overrides (127) no tocan `indexing` / `metadata` / `tracking`,
        así que la rama de remonte es defensiva — pero si corre, conserva el
        estado probado (ver `on_input_changed`) y tolera workers tardíos."""
        if not self.console.state.rebuild_conn(self.console.effective_config()):
            self._render_meta()  # 138: editar un alias cambia host / sitios sin cambiar tarjetas
            self.render_all()
            return
        grid = self.query_one("#creds-grid", Grid)
        await grid.remove_children()
        await grid.mount_all([self._card(info) for info in self._infos()])
        self._render_hint()
        self.render_all()
        self.console.refresh_status()

    def _render_meta(self) -> None:
        """Título y sitios de cada tarjeta desde la config efectiva actual."""
        for info in self._infos():
            if info.alias == CMIS_ALIAS or not self.query(f"#title-{info.alias}"):
                continue
            self.query_one(f"#title-{info.alias}", Static).update(info.title)
            self.query_one(f"#sites-{info.alias}", Static).update(_sites_text(info))

    _REAUTH_HINT = (
        "⏸ Sesión CMIS rechazada — corrida PAUSADA. Cargá usuario/contraseña "
        "nuevos en la tarjeta cmis, probá la conexión y reanudá con r en [6]."
    )

    def _render_hint(self) -> None:
        if getattr(self.console.run_manager, "reauth_pending", False):
            self.query_one("#creds-hint", Static).update(self._REAUTH_HINT)  # 132
            return
        n = len(self.console.state.conn) - 1
        self.query_one("#creds-hint", Static).update(
            "Esta config usa solo CMIS — no tiene conexiones AS400 ni SQL Server."
            if n == 0
            else f"{n} conexión{'es' if n > 1 else ''} del registro además de CMIS."
        )

    def render_all(self) -> None:
        # 138 E7: con corrida activa el YAML no se toca — botones apagados.
        busy = self.console.run_active
        for button in self.query(".conn-edit"):
            button.disabled = busy
        for alias in self.console.state.conn:
            self.render_conn(alias)

    def show_reauth_hint(self) -> None:
        """132: la corrida está pausada esperando una credencial CMIS nueva —
        la pista sobrevive a `rebuild_cards` porque `_render_hint` mira
        `run_manager.reauth_pending`."""
        self._render_hint()
        with contextlib.suppress(Exception):
            self.query_one(f"#pass-{CMIS_ALIAS}", Input).focus()

    def _card(self, info: ConnInfo) -> Vertical:
        alias = info.alias
        card = Vertical(classes="card", id=f"card-{alias}")
        title = "CMIS · Alfresco" if alias == CMIS_ALIAS else info.title
        card.compose_add_child(
            Horizontal(
                Static(title, classes="card-title", id=f"title-{alias}"),
                Static("  ●", classes="chip-idle", id=f"chip-{alias}"),
                Static(" sin probar", classes="chip-idle", id=f"chiplbl-{alias}"),
                classes="frow",
            )
        )
        if alias != CMIS_ALIAS:
            card.compose_add_child(
                Static(_sites_text(info), classes="card-sites", id=f"sites-{alias}")
            )
        cred = self.console.state.creds.get(alias)
        card.compose_add_child(Label("usuario"))
        card.compose_add_child(Input(value=cred.username, id=f"user-{alias}"))
        card.compose_add_child(Label("contraseña"))
        card.compose_add_child(
            Horizontal(
                Input(value=cred.password, password=True, id=f"pass-{alias}"),
                Button("ver", id=f"reveal-{alias}"),
                classes="frow",
            )
        )
        row = Horizontal(classes="frow")
        row.compose_add_child(Button("probar conexión", variant="primary", id=f"test-{alias}"))
        if info.kind == "as400":
            row.compose_add_child(Static("", classes="tries", id=f"tries-{alias}"))
        card.compose_add_child(row)
        # Las acciones de edición van en su propia fila: junto a "probar" no
        # entran en una tarjeta de la grilla (2 columnas) a 120 cols.
        actions = self._card_actions(info)
        if actions:
            card.compose_add_child(Horizontal(*actions, classes="frow"))
        # markup=False: los mensajes traen cuerpos de error de CMIS/AS400
        # con corchetes y JSON que Textual leería como markup y rompería.
        card.compose_add_child(Static("", classes="msg", id=f"msg-{alias}", markup=False))
        return card

    @staticmethod
    def _card_actions(info: ConnInfo) -> list[Button]:
        """138 REQ-003: CMIS no vive en ``connections`` (sin botones); la
        inline ``as400`` sólo se puede mover al registro; el resto se edita/quita."""
        alias = info.alias
        if alias == CMIS_ALIAS:
            return []
        if alias == INLINE_CONNECTION_ALIAS:
            return [Button("mover al registro…", id=f"move-{alias}", classes="conn-edit")]
        return [
            Button("editar", id=f"edit-{alias}", classes="conn-edit"),
            Button("quitar", id=f"del-{alias}", classes="conn-edit"),
        ]

    # ------------------------------------------------------------ eventos

    def on_input_changed(self, event: Input.Changed) -> None:
        parsed = _role_and_alias(event.input.id or "")
        if parsed is None:
            return
        role, alias = parsed
        cred = self.console.state.creds.get(alias)
        stored = cred.username if role == "user" else cred.password
        if event.value.strip() == stored:
            # Un Input recién montado (rebuild_cards) postea Changed con el
            # valor prefilled: no es una edición, no invalida lo probado.
            return
        self._store_inputs(alias)
        self.console.state.invalidate_conn(alias)
        self.render_conn(alias)
        self.console.refresh_status()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        alias = _alias_of(event.input.id or "")
        if alias:
            self.request_test(alias)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("reveal-"):
            inp = self.query_one(f"#pass-{bid.removeprefix('reveal-')}", Input)
            inp.password = not inp.password
            event.button.label = "ver" if inp.password else "ocultar"
        elif bid.startswith("test-"):
            self.request_test(bid.removeprefix("test-"))
        elif bid == "new-conn":
            self.open_new()
        elif bid.startswith("edit-"):
            self.open_edit(bid.removeprefix("edit-"))
        elif bid.startswith("del-"):
            self.request_delete(bid.removeprefix("del-"))
        elif bid.startswith("move-"):
            self.open_move()

    # ------------------------------------------------- 138: editor de conexiones

    def _editing_allowed(self) -> bool:
        if self.console.run_active:
            self.console.notify(_RUN_ACTIVE_MSG, severity="warning")
            return False
        return True

    def _push_editor(
        self,
        screen: ConnectionEditScreen,
        *,
        editing: str | None,
        move_from: str | None = None,
    ) -> None:
        """Abre el modal; al guardar, ``commit_draft`` con los sitios marcados."""

        async def _done(draft: ConnectionDraft | None) -> None:
            if draft is not None:
                await self.commit_draft(
                    draft, use_at=screen.use_at, editing=editing, move_from=move_from
                )

        self.console.push_screen(screen, _done)

    def open_new(self) -> None:
        if not self._editing_allowed():
            return
        config = self.console.config
        self._push_editor(
            ConnectionEditScreen(sites=connection_sites(config), existing=set(config.connections)),
            editing=None,
        )

    def open_edit(self, alias: str) -> None:
        """Precarga desde el YAML crudo (no el modelo) para no escribir defaults
        que el operador nunca puso; el modelo sólo si el alias no está (raro)."""
        if not self._editing_allowed():
            return
        config = self.console.config
        spec = config.connections.get(alias)
        if spec is None:
            self.console.notify(f"{alias} no está en connections", severity="error")
            return
        try:
            raw = to_plain(YamlDocument.load(self.console.config_path).get(("connections", alias)))
        except (YamlDocumentError, OSError) as exc:
            self.console.notify(f"No se pudo leer el YAML: {exc}", severity="error")
            return
        if not isinstance(raw, dict):
            raw = spec.model_dump(mode="json", exclude_none=True)
        self._push_editor(
            ConnectionEditScreen(
                sites=connection_sites(config),
                existing=set(config.connections),
                editing=alias,
                kind=spec.kind,
                alias=alias,
                fields=prefill_fields(spec.kind, raw),
            ),
            editing=alias,
        )

    def open_move(self) -> None:
        """La tarjeta inline ``as400``: mismo modal, kind y campos del inline,
        alias vacío, y los sitios inline pre-marcados (E6)."""
        if not self._editing_allowed():
            return
        config = self.console.config
        inline = [s for s in connection_sites(config) if s.kind == "as400" and s.current is None]
        if not inline:
            self.console.notify("No hay conexiones inline para mover", severity="warning")
            return
        model = inline_connection(inline[0], config)
        self._push_editor(
            ConnectionEditScreen(
                sites=connection_sites(config),
                existing=set(config.connections),
                kind="as400",
                fields=prefill_fields("as400", model.model_dump(mode="json", exclude_none=True)),
                preselect=frozenset(s.path for s in inline),
                title="Mover la conexión inline al registro",
            ),
            editing=None,
            move_from=INLINE_CONNECTION_ALIAS,
        )

    def request_delete(self, alias: str) -> None:
        """Baja con confirmación — salvo que algún sitio la use: ahí se rechaza."""
        if not self._editing_allowed():
            return
        plan = plan_delete(alias, self.console.config)
        if not plan.ok:
            used = ", ".join(site.label for site in plan.blocked_by)
            self.console.notify(f"{alias} está en uso por: {used}", severity="error", timeout=8)
            return
        self.console.confirm(
            title=f"Quitar la conexión {alias}",
            body=(
                f"Se borra connections.{alias} de {self.console.config_path} "
                "(con backup). Las credenciales de sesión de ese alias se descartan."
            ),
            yes="quitar",
            no="cancelar",
            danger=True,
            cb=lambda ok: self.call_later(self._commit_delete, alias, plan.edits) if ok else None,
        )

    async def _commit_delete(self, alias: str, edits: Iterable[Edit]) -> None:
        """I3: el confirm promete descartar las credenciales de sesión del alias
        — recién cuando el YAML se escribió de verdad se descartan (si el write
        falla, el alias sigue existiendo y su credencial también)."""
        if await self.apply_connection_edits(edits, f"Conexión {alias} quitada"):
            self.console.state.creds.discard(alias)

    async def commit_draft(
        self,
        draft: ConnectionDraft,
        *,
        use_at: set[YamlPath],
        editing: str | None,
        move_from: str | None = None,
    ) -> None:
        """Del borrador validado al disco. ``plan_write`` corta ANTES de tocar
        el YAML si un sitio no es de la kind (E8)."""
        state = self.console.state
        try:
            edits = plan_write(draft, self.console.config, use_at=use_at, editing=editing)
        except ValueError as exc:
            self.console.notify(f"No se escribió el YAML: {exc}", severity="error", timeout=8)
            return
        alias = draft.alias
        if move_from is not None:
            # E6: las credenciales de sesión del inline pasan al alias nuevo
            # ANTES del rebuild, así la tarjeta nace prefilled.
            cred = state.creds.get(move_from)
            state.creds.set(alias, cred.username, cred.password)
        verb = "editada" if editing else ("movida al registro" if move_from else "creada")
        ok = await self.apply_connection_edits(edits, f"Conexión {alias} {verb}")
        if not ok:
            if move_from is not None:
                state.creds.credentials.pop(alias, None)
            return
        if editing is not None:
            # E3: cambió host/puerto — la prueba anterior ya no vale.
            state.invalidate_conn(editing)
            state.mark_doctor_stale("cambió connections")
            self.render_conn(editing)
            self.console.refresh_status()

    async def apply_connection_edits(self, edits: Iterable[Edit], notice: str) -> bool:
        """REQ-004: carga el YAML, aplica, verifica con ``load_config`` y
        reemplaza (137). Éxito → config nueva + tarjetas + [3] + status."""
        result = self._write_edits(edits)
        if result is None:
            return False
        console = self.console
        console.config = result.value  # los overrides de sesión quedan (REQ-005)
        console.state.mark_doctor_stale("cambió connections")
        await self.rebuild_cards()  # rebuild_conn hace el prefill del alias nuevo
        self._render_hint()
        console.q("ConfigPane", ConfigPane).refresh_yaml_values()
        await console.q("YamlPane", YamlPane).reload_if_clean()  # 139
        console.on_pii_override(None)
        console.refresh_status()
        console.notify(f"{notice} — backup en {result.backup_path}", timeout=8)
        return True

    def _write_edits(self, edits: Iterable[Edit]) -> WriteResult[PipelineConfig] | None:
        try:
            doc = YamlDocument.load(self.console.config_path)
            apply_edits(doc, edits)
            return doc.write(verify=load_config)
        except (YamlDocumentError, YamlWriteError, OSError) as exc:
            text = write_error_text(exc)
            self.console.notify(f"No se escribió el YAML: {text}", severity="error", timeout=10)
            return None

    # ------------------------------------------------------------ prueba

    def request_test(self, alias: str) -> None:
        state = self.console.state
        self._store_inputs(alias)
        if state.as400_needs_lockout_confirm(alias):
            self.console.confirm(
                title=f"Tercer intento contra el iSeries ({alias})",
                body=(
                    f"Ya fallaron {AS400_MAX_TRIES - 1} intentos. Un tercer fallo BLOQUEA "
                    "el perfil en el AS400 (ticket a Seguridad para desbloquear).\n"
                    "Verificá la contraseña con [ver] antes de continuar."
                ),
                yes="probar igual",
                no="volver a verificar",
                danger=True,
                cb=lambda ok: self._start_test(alias) if ok else None,
            )
            return
        self._start_test(alias)

    def _store_inputs(self, alias: str) -> None:
        user = self.query_one(f"#user-{alias}", Input).value
        pwd = self.query_one(f"#pass-{alias}", Input).value
        self.console.state.creds.set(alias, user, pwd)

    def _start_test(self, alias: str) -> None:
        state = self.console.state
        slot = state.conn.get(alias)
        if slot is None:
            return
        if not state.creds.complete(alias):
            state.record_conn_result(
                alias, ok=False, message="Credencial vacía — completá usuario y contraseña."
            )
            if state.conn[alias].attempts:
                state.conn[alias].attempts -= 1  # el vacío no gasta intento de lockout
            self.render_conn(alias)
            return
        slot.status = "testing"
        self.render_conn(alias)
        self.console.run_check_worker(alias, self._apply_result)

    def _apply_result(self, alias: str, result: CheckResult, elapsed_ms: float) -> None:
        if alias not in self.console.state.conn:
            return  # la tarjeta desapareció mientras el worker corría
        ok = result.status is CheckStatus.PASS
        msg = f"{result.message} · {elapsed_ms:.0f} ms" if ok else result.message
        self.console.state.record_conn_result(alias, ok=ok, message=msg)
        if ok:
            self.console.state.reset_attempts(alias)
        self.render_conn(alias)
        self.console.refresh_status()
        self.console.notify(
            f"Conexión {alias} {'OK' if ok else 'falló'}",
            severity="information" if ok else "error",
        )

    # ------------------------------------------------------------ render

    def render_conn(self, alias: str) -> None:
        """Redibuja chip + mensaje de una tarjeta desde ``state.conn``."""
        c = self.console.state.conn.get(alias)
        if c is None or not self.query(f"#chip-{alias}"):
            return  # la tarjeta ya no existe (config efectiva cambió)
        cls = {"ok": "chip-ok", "err": "chip-err", "testing": "chip-run"}.get(c.status, "chip-idle")
        label = {
            "ok": f" ok · {c.age_label(now=time.time())}",
            "err": " falló",
            "testing": " probando…",
        }.get(c.status, " sin probar")
        chip = self.query_one(f"#chip-{alias}", Static)
        chip.set_classes(cls)
        lbl = self.query_one(f"#chiplbl-{alias}", Static)
        lbl.set_classes(cls)
        lbl.update(label)
        self.query_one(f"#msg-{alias}", Static).update(c.message)
        if c.kind == "as400":
            self.query_one(f"#tries-{alias}", Static).update(
                f"intento {c.attempts} de {AS400_MAX_TRIES} — al 3° el perfil se bloquea"
                if c.attempts
                else ""
            )


def run_single_check(alias: str, console: ConsoleApp) -> tuple[CheckResult, float]:
    """Cuerpo del worker: corre el check real de UNA conexión y mide latencia."""
    start = time.monotonic()
    secrets = console.state.creds.to_secrets()
    # 127: misma config efectiva que el doctor y la corrida.
    effective = console.effective_config()
    if alias == CMIS_ALIAS:
        result = check_cmis(effective, secrets)
    else:
        result = check_connection(effective, secrets, alias)
    return result, (time.monotonic() - start) * 1000.0
