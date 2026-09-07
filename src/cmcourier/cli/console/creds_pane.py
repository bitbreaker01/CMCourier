"""Panel CREDENCIALES de la consola (123; tarjetas por alias desde 131).

Contrato UX (mock v2 + informes adversariales):
* credenciales solo en memoria; editar invalida la prueba;
* "probar conexión" en worker thread (no bloquea la UI);
* una tarjeta por conexión que la config EFECTIVA usa (registro 129:
  ``<alias> · <kind> · <host>`` + los sitios que la usan) más CMIS, que
  siempre existe;
* las tarjetas ``as400`` llevan contador de intentos y confirmación antes
  del 3° (lockout del perfil en el iSeries); cmis/mssql no;
* toggle mostrar/ocultar contraseña.
"""

from __future__ import annotations

__all__ = ["CredsPane", "run_single_check"]

import time
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import Button, Input, Label, Static

from cmcourier.cli.console.state import AS400_MAX_TRIES, CMIS_ALIAS, ConnInfo, connection_infos
from cmcourier.cli.doctor import CheckResult, CheckStatus, check_cmis, check_connection

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

_INPUT_ROLES = ("user", "pass")


def _role_and_alias(widget_id: str) -> tuple[str, str] | None:
    """``user-<alias>`` / ``pass-<alias>`` → (rol, alias); los alias no llevan guiones."""
    role, sep, alias = widget_id.partition("-")
    return (role, alias) if sep and role in _INPUT_ROLES and alias else None


def _alias_of(widget_id: str) -> str | None:
    parsed = _role_and_alias(widget_id)
    return parsed[1] if parsed else None


class CredsPane(Vertical):
    """Formulario CMIS + una tarjeta por alias, con prueba de conexión en vivo."""

    DEFAULT_CSS = """
    CredsPane { padding: 1 2; }
    CredsPane .intro { color: $text-muted; margin-bottom: 1; }
    CredsPane .hint { color: $text-muted; margin-bottom: 1; }
    CredsPane Grid { grid-size: 2; grid-gutter: 1 2; height: auto; }
    CredsPane .card { border: solid $surface-lighten-2; padding: 1 2; height: auto; }
    CredsPane .card-title { text-style: bold; }
    CredsPane .card-sites { color: $text-muted; }
    CredsPane .chip-ok { color: $success; }
    CredsPane .chip-err { color: $error; }
    CredsPane .chip-run { color: $accent; }
    CredsPane .chip-idle { color: $text-muted; }
    CredsPane .msg { color: $text-muted; margin-top: 1; }
    CredsPane .tries { color: $warning; }
    CredsPane Input { margin-top: 0; }
    CredsPane .frow { height: auto; margin-top: 1; }
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
            self.render_all()
            return
        grid = self.query_one("#creds-grid", Grid)
        await grid.remove_children()
        await grid.mount_all([self._card(info) for info in self._infos()])
        self._render_hint()
        self.render_all()
        self.console.refresh_status()

    def _render_hint(self) -> None:
        n = len(self.console.state.conn) - 1
        self.query_one("#creds-hint", Static).update(
            "Esta config usa solo CMIS — no tiene conexiones AS400 ni SQL Server."
            if n == 0
            else f"{n} conexión{'es' if n > 1 else ''} del registro además de CMIS."
        )

    def render_all(self) -> None:
        for alias in self.console.state.conn:
            self._render_conn(alias)

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
                Static(
                    f"usada por: {' · '.join(info.sites)}",
                    classes="card-sites",
                    id=f"sites-{alias}",
                )
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
        if info.kind == "as400":
            row.compose_add_child(Static("", classes="tries", id=f"tries-{alias}"))
        row.compose_add_child(Button("probar conexión", variant="primary", id=f"test-{alias}"))
        card.compose_add_child(row)
        # markup=False: los mensajes traen cuerpos de error de CMIS/AS400
        # con corchetes y JSON que Textual leería como markup y rompería.
        card.compose_add_child(Static("", classes="msg", id=f"msg-{alias}", markup=False))
        return card

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
        self._render_conn(alias)
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
            self._render_conn(alias)
            return
        slot.status = "testing"
        self._render_conn(alias)
        self.console.run_check_worker(alias, self._apply_result)

    def _apply_result(self, alias: str, result: CheckResult, elapsed_ms: float) -> None:
        if alias not in self.console.state.conn:
            return  # la tarjeta desapareció mientras el worker corría
        ok = result.status is CheckStatus.PASS
        msg = f"{result.message} · {elapsed_ms:.0f} ms" if ok else result.message
        self.console.state.record_conn_result(alias, ok=ok, message=msg)
        if ok:
            self.console.state.reset_attempts(alias)
        self._render_conn(alias)
        self.console.refresh_status()
        self.console.notify(
            f"Conexión {alias} {'OK' if ok else 'falló'}",
            severity="information" if ok else "error",
        )

    # ------------------------------------------------------------ render

    def _render_conn(self, alias: str) -> None:
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
