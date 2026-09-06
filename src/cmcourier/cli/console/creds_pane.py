"""Panel CREDENCIALES de la consola (123).

Contrato UX (mock v2 + informes adversariales):
* credenciales solo en memoria; editar invalida la prueba;
* "probar conexión" en worker thread (no bloquea la UI);
* AS400 con contador de intentos y confirmación antes del 3° (lockout
  del perfil en el iSeries);
* toggle mostrar/ocultar contraseña.
"""

from __future__ import annotations

__all__ = ["CredsPane"]

import time
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import Button, Input, Label, Static

from cmcourier.cli.console.state import AS400_MAX_TRIES
from cmcourier.cli.doctor import CheckResult, CheckStatus, check_as400, check_cmis

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp


class CredsPane(Vertical):
    """Formulario CMIS + AS400 con prueba de conexión en vivo."""

    DEFAULT_CSS = """
    CredsPane { padding: 1 2; }
    CredsPane .intro { color: $text-muted; margin-bottom: 1; }
    CredsPane Grid { grid-size: 2; grid-gutter: 1 2; height: auto; }
    CredsPane .card { border: solid $surface-lighten-2; padding: 1 2; height: auto; }
    CredsPane .card-title { text-style: bold; }
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
        with Grid():
            yield self._card("cmis", "CMIS · Alfresco")
            yield self._card("as400", "AS400 · RVABREP")

    def _card(self, which: str, title: str) -> Vertical:
        creds = self.console.state.creds
        user = creds.cmis_username if which == "cmis" else creds.as400_username
        card = Vertical(classes="card", id=f"card-{which}")
        card.compose_add_child(
            Horizontal(
                Static(title, classes="card-title"),
                Static("  ●", classes="chip-idle", id=f"chip-{which}"),
                Static(" sin probar", classes="chip-idle", id=f"chiplbl-{which}"),
                classes="frow",
            )
        )
        card.compose_add_child(Label("usuario"))
        card.compose_add_child(Input(value=user, id=f"user-{which}"))
        card.compose_add_child(Label("contraseña"))
        card.compose_add_child(
            Horizontal(
                Input(password=True, id=f"pass-{which}"),
                Button("ver", id=f"reveal-{which}"),
                classes="frow",
            )
        )
        card.compose_add_child(
            Horizontal(
                Static("", classes="tries", id=f"tries-{which}"),
                Button("probar conexión", variant="primary", id=f"test-{which}"),
                classes="frow",
            )
        )
        # markup=False: los mensajes traen cuerpos de error de CMIS/AS400
        # con corchetes y JSON que Textual leería como markup y rompería.
        card.compose_add_child(Static("", classes="msg", id=f"msg-{which}", markup=False))
        return card

    # ------------------------------------------------------------ eventos

    def on_input_changed(self, event: Input.Changed) -> None:
        wid = event.input.id or ""
        which = "cmis" if wid.endswith("cmis") else "as400" if wid.endswith("as400") else None
        if which is None:
            return
        self._store_inputs(which)
        self.console.state.invalidate_conn(which)
        self._render_conn(which)
        self.console.refresh_status()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        wid = event.input.id or ""
        which = "cmis" if wid.endswith("cmis") else "as400" if wid.endswith("as400") else None
        if which:
            self.request_test(which)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("reveal-"):
            inp = self.query_one(f"#pass-{bid.removeprefix('reveal-')}", Input)
            inp.password = not inp.password
            event.button.label = "ver" if inp.password else "ocultar"
        elif bid.startswith("test-"):
            self.request_test(bid.removeprefix("test-"))

    # ------------------------------------------------------------ prueba

    def request_test(self, which: str) -> None:
        state = self.console.state
        self._store_inputs(which)
        if which == "as400" and state.as400_needs_lockout_confirm():
            self.console.confirm(
                title="Tercer intento contra el iSeries",
                body=(
                    f"Ya fallaron {AS400_MAX_TRIES - 1} intentos. Un tercer fallo BLOQUEA "
                    "el perfil en el AS400 (ticket a Seguridad para desbloquear).\n"
                    "Verificá la contraseña con [ver] antes de continuar."
                ),
                yes="probar igual",
                no="volver a verificar",
                danger=True,
                cb=lambda ok: self._start_test(which) if ok else None,
            )
            return
        self._start_test(which)

    def _store_inputs(self, which: str) -> None:
        creds = self.console.state.creds
        user = self.query_one(f"#user-{which}", Input).value.strip()
        pwd = self.query_one(f"#pass-{which}", Input).value
        if which == "cmis":
            creds.cmis_username, creds.cmis_password = user, pwd
        else:
            creds.as400_username, creds.as400_password = user, pwd

    def _start_test(self, which: str) -> None:
        state = self.console.state
        creds = state.creds
        complete = creds.cmis_complete() if which == "cmis" else creds.as400_complete()
        if not complete:
            state.record_conn_result(
                which, ok=False, message="Credencial vacía — completá usuario y contraseña."
            )
            if which == "as400":
                state.conn["as400"].attempts -= 1  # el vacío no gasta intento de lockout
            self._render_conn(which)
            return
        state.conn[which].status = "testing"
        self._render_conn(which)
        self.console.run_check_worker(which, self._apply_result)

    def _apply_result(self, which: str, result: CheckResult, elapsed_ms: float) -> None:
        ok = result.status is CheckStatus.PASS
        msg = result.message if ok else f"{result.message}"
        if ok:
            msg = f"{msg} · {elapsed_ms:.0f} ms"
        self.console.state.record_conn_result(which, ok=ok, message=msg)
        if ok and which == "as400":
            self.console.state.reset_as400_attempts()
        self._render_conn(which)
        self.console.refresh_status()
        self.console.notify(
            f"Conexión {which.upper()} {'OK' if ok else 'falló'}",
            severity="information" if ok else "error",
        )

    # ------------------------------------------------------------ render

    def _render_conn(self, which: str) -> None:
        c = self.console.state.conn[which]
        cls = {"ok": "chip-ok", "err": "chip-err", "testing": "chip-run"}.get(c.status, "chip-idle")
        label = {
            "ok": f" ok · {c.age_label(now=time.time())}",
            "err": " falló",
            "testing": " probando…",
        }.get(c.status, " sin probar")
        chip = self.query_one(f"#chip-{which}", Static)
        chip.set_classes(cls)
        lbl = self.query_one(f"#chiplbl-{which}", Static)
        lbl.set_classes(cls)
        lbl.update(label)
        self.query_one(f"#msg-{which}", Static).update(c.message)
        if which == "as400":
            tries = self.query_one("#tries-as400", Static)
            tries.update(
                f"intento {c.attempts} de {AS400_MAX_TRIES} — al 3° el perfil se bloquea"
                if c.attempts
                else ""
            )


def run_single_check(which: str, console: ConsoleApp) -> tuple[CheckResult, float]:
    """Cuerpo del worker: corre el check real y mide latencia."""
    start = time.monotonic()
    secrets = console.state.creds.to_secrets()
    fn = check_cmis if which == "cmis" else check_as400
    result = fn(console.config, secrets)
    return result, (time.monotonic() - start) * 1000.0
