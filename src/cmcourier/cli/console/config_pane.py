"""Panel CONFIG de la consola (124) — overrides con modelo draft/applied.

C4 del informe UX v2: el resumen efectivo y el launcher leen SOLO los
overrides promovidos; un draft editado sin `a` jamás llega a una
corrida.
"""

from __future__ import annotations

__all__ = ["ConfigPane"]

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static

from cmcourier.cli.console.overrides import OverrideError, SessionOverrides

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp


class ConfigPane(Vertical):
    DEFAULT_CSS = """
    ConfigPane { padding: 1 2; }
    ConfigPane .intro { color: $text-muted; margin-bottom: 1; }
    ConfigPane .rows { height: 1fr; }
    ConfigPane .cfgrow { height: auto; margin-bottom: 1; }
    ConfigPane .cfgrow Label { width: 26; color: $text-muted; padding-top: 1; }
    ConfigPane .cfgrow Input { width: 16; }
    ConfigPane .cfgrow Select { width: 24; }
    ConfigPane .yamlval { color: $text-muted; padding-top: 1; }
    ConfigPane .pii-warn { color: $error; padding-top: 1; }
    ConfigPane .statusline { color: $warning; height: auto; margin-top: 1; }
    ConfigPane .btns { height: auto; margin-top: 1; align-horizontal: right; }
    ConfigPane Button { margin-left: 2; }
    """

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console

    def compose(self) -> ComposeResult:
        cfg = self.console.config
        yield Static(
            "Lo estructural vive en el YAML (solo lectura). Lo operativo se ajusta por "
            "sesión y aplica a la PRÓXIMA corrida — editar es un borrador hasta que lo "
            "guardás con a.",
            classes="intro",
        )
        with VerticalScroll(classes="rows"):
            yield self._row_select(
                "processing.mode",
                "ov-mode",
                [
                    ("(yaml) " + cfg.processing.mode, ""),
                    ("batched", "batched"),
                    ("streaming", "streaming"),
                ],
            )
            yield self._row_input(
                "processing.prep_workers", "ov-prep", str(cfg.processing.prep_workers)
            )
            yield self._row_input(
                "streaming.bucket_size", "ov-bucket", str(cfg.processing.streaming.bucket_size)
            )
            yield self._row_input("cmis.workers", "ov-workers", str(cfg.cmis.workers))
            yield self._row_select(
                "cmis.auto_tune.enabled",
                "ov-aimd",
                [
                    (f"(yaml) {cfg.cmis.auto_tune.enabled}", ""),
                    ("true", "true"),
                    ("false", "false"),
                ],
            )
            yield self._row_input(
                "cmis.max_bandwidth_mbps", "ov-bw", str(cfg.cmis.max_bandwidth_mbps)
            )
            with Horizontal(classes="cfgrow"):
                yield Label("observability.unmask_pii")
                yield Select(
                    [
                        (f"(yaml) {cfg.observability.unmask_pii}", ""),
                        ("true", "true"),
                        ("false", "false"),
                    ],
                    value="",
                    id="ov-pii",
                    allow_blank=False,
                )
                yield Static("", classes="pii-warn", id="pii-warn")
        yield Static("", classes="statusline", id="cfg-status")
        with Horizontal(classes="btns"):
            yield Button("descartar overrides", id="cfg-reset")
            yield Button("guardar para la próxima corrida (a)", variant="primary", id="cfg-apply")
            yield Button("escribir en el YAML (w)", variant="warning", id="cfg-persist")

    def refresh_yaml_values(self) -> None:
        """135: tras escribir el YAML, los ``(yaml) …`` reflejan el archivo nuevo."""
        cfg = self.console.config
        for wid, value in (
            ("ov-prep", cfg.processing.prep_workers),
            ("ov-bucket", cfg.processing.streaming.bucket_size),
            ("ov-workers", cfg.cmis.workers),
            ("ov-bw", cfg.cmis.max_bandwidth_mbps),
        ):
            inp = self.query_one(f"#{wid}", Input)
            inp.placeholder = f"(yaml) {value}"
            inp.value = ""
        selects: list[tuple[str, object, tuple[str, ...]]] = [
            ("ov-mode", cfg.processing.mode, ("batched", "streaming")),
            ("ov-aimd", cfg.cmis.auto_tune.enabled, ("true", "false")),
            ("ov-pii", cfg.observability.unmask_pii, ("true", "false")),
        ]
        for wid, yaml_value, choices in selects:
            sel = self.query_one(f"#{wid}", Select)
            sel.set_options([(f"(yaml) {yaml_value}", ""), *((c, c) for c in choices)])
            sel.value = ""
        self.query_one("#pii-warn", Static).update("")
        self._render_status()

    def _row_input(self, label: str, wid: str, yaml_value: str) -> Horizontal:
        row = Horizontal(classes="cfgrow")
        row.compose_add_child(Label(label))
        row.compose_add_child(Input(placeholder=f"(yaml) {yaml_value}", id=wid))
        return row

    def _row_select(self, label: str, wid: str, options: list[tuple[str, str]]) -> Horizontal:
        row = Horizontal(classes="cfgrow")
        row.compose_add_child(Label(label))
        row.compose_add_child(Select(options, value="", id=wid, allow_blank=False))
        return row

    # ------------------------------------------------------------ draft

    def read_draft(self) -> SessionOverrides:
        def num(wid: str) -> int | None:
            raw = self.query_one(f"#{wid}", Input).value.strip()
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError as exc:
                raise OverrideError(f"{wid.removeprefix('ov-')}: {raw!r} no es un entero") from exc

        def sel(wid: str) -> str | None:
            v = str(self.query_one(f"#{wid}", Select).value)
            return v or None

        def selbool(wid: str) -> bool | None:
            v = sel(wid)
            return None if v is None else v == "true"

        bw_raw = self.query_one("#ov-bw", Input).value.strip()
        return SessionOverrides(
            mode=sel("ov-mode"),
            prep_workers=num("ov-prep"),
            bucket_size=num("ov-bucket"),
            workers=num("ov-workers"),
            auto_tune_enabled=selbool("ov-aimd"),
            max_bandwidth_mbps=float(bw_raw) if bw_raw else None,
            unmask_pii=selbool("ov-pii"),
            # 127: el pipeline se elige en [5]; acá se arrastra tal cual.
            trigger=self.console.state.overrides.trigger,
        )

    def apply_draft(self) -> None:
        try:
            draft = self.read_draft()
        except (OverrideError, ValueError) as exc:
            self.console.notify(str(exc), severity="error")
            return
        try:
            self.console.state.promote_overrides(draft)
        except OverrideError as exc:
            self.console.notify(str(exc), severity="error")
            return
        applied = self.console.state.overrides
        self.console.notify(
            "Overrides guardados para la próxima corrida (el YAML no se toca)"
            + (" — la corrida en curso no cambia" if self.console.run_active else ""),
        )
        self._render_status()
        self.console.on_pii_override(applied.unmask_pii)
        self.console.refresh_status()

    def reset_draft(self) -> None:
        for wid in ("ov-prep", "ov-bucket", "ov-workers", "ov-bw"):
            self.query_one(f"#{wid}", Input).value = ""
        for wid in ("ov-mode", "ov-aimd", "ov-pii"):
            self.query_one(f"#{wid}", Select).value = ""
        self.console.state.overrides = self.console.state.overrides.cleared()
        self.console.state.mark_doctor_stale("cambiaron los overrides de sesión")
        self.console.on_pii_override(None)
        self.console.notify("Overrides descartados — la sesión vuelve al YAML")
        self._render_status()
        self.console.refresh_status()

    def draft_dirty(self) -> bool:
        """True cuando lo tipeado difiere de lo APLICADO (aviso del launcher)."""
        try:
            return self.read_draft() != self.console.state.overrides
        except (OverrideError, ValueError):
            return True

    # ------------------------------------------------------------ eventos

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-apply":
            self.apply_draft()
        elif event.button.id == "cfg-reset":
            self.reset_draft()
        elif event.button.id == "cfg-persist":
            self.console.persist_overrides()

    def on_input_changed(self, _: Input.Changed) -> None:
        self._render_status()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "ov-pii":
            self.query_one("#pii-warn", Static).update(
                "⚠ los eventos de upload emitirán PII cruda" if event.value == "true" else ""
            )
        self._render_status()

    def _render_status(self) -> None:
        status = self.query_one("#cfg-status", Static)
        applied = self.console.state.overrides
        if self.draft_dirty():
            status.update("▲ borrador sin guardar — apretá a para que aplique a la próxima corrida")
        elif applied.has_scalars():
            status.update(f"● aplicado a la sesión: {applied.scalar_summary()}")
        else:
            status.update("")
