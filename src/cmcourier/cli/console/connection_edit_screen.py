"""Modal de alta / edición de una conexión del registro (138 REQ-002).

Un ``Input`` por campo de la kind elegida (placeholder = default del
modelo), un ``Checkbox`` por sitio de la config que acepta esa kind, y
validación por campo ANTES de cerrar (``validate_draft``). El modal no
toca el disco: devuelve el ``ConnectionDraft`` y deja los sitios marcados
en ``use_at``; escribir es asunto de ``CredsPane`` (REQ-004).

Contrato UX:
* al editar, alias y kind quedan deshabilitados (cambiar de kind =
  borrar y crear) y los valores vienen del YAML crudo, no del modelo —
  así no se escriben defaults que el operador nunca puso;
* desmarcar un sitio que HOY usa este alias está bloqueado: soltarlo es
  apuntar el sitio a otra conexión desde su propio editor;
* Enter en cualquier Input guarda, Escape cancela.
"""

from __future__ import annotations

__all__ = ["ConnectionEditScreen"]

from collections.abc import Mapping
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from cmcourier.cli.console.connection_edit import (
    KIND_FIELDS,
    ConnectionDraft,
    DraftErrors,
    Site,
    field_default,
    validate_draft,
)
from cmcourier.config.schema import ConnectionKind
from cmcourier.config.yaml_doc import YamlPath

_KIND_OPTIONS = [("as400", "as400"), ("mssql", "mssql")]
_BLOCKED_HINT = (
    "Este sitio usa esta conexión — para soltarlo, apuntá el sitio a otra conexión desde su editor"
)


class ConnectionEditScreen(ModalScreen[ConnectionDraft | None]):
    """Alta (``editing=None``) o edición (``editing=alias``) de una conexión."""

    DEFAULT_CSS = """
    ConnectionEditScreen { align: center middle; }
    ConnectionEditScreen > Vertical { width: 72; height: auto; max-height: 90%;
        border: solid $accent; background: $panel; padding: 1 2; overflow-y: auto; }
    ConnectionEditScreen .title { text-style: bold; color: $accent; margin-bottom: 1; }
    ConnectionEditScreen Label { margin-top: 1; }
    ConnectionEditScreen .err { color: $error; height: auto; }
    ConnectionEditScreen #fields, ConnectionEditScreen #sites { height: auto; }
    ConnectionEditScreen .sites-title { margin-top: 1; color: $text-muted; }
    ConnectionEditScreen .btns { height: auto; align-horizontal: right; margin-top: 1; }
    ConnectionEditScreen Button { margin-left: 2; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "cancelar")]

    def __init__(
        self,
        *,
        sites: list[Site],
        existing: set[str],
        editing: str | None = None,
        kind: ConnectionKind = "as400",
        alias: str = "",
        fields: Mapping[str, str] | None = None,
        preselect: frozenset[YamlPath] = frozenset(),
        title: str = "",
    ) -> None:
        super().__init__()
        self._sites = sites
        self._existing = existing
        self._editing = editing
        self._kind: ConnectionKind = kind
        self._alias = alias
        self._fields = dict(fields or {})
        self._preselect = preselect
        self._title = title or (f"Editar conexión {editing}" if editing else "Nueva conexión")
        # Sitios marcados al guardar; lo lee CredsPane después del dismiss.
        self.use_at: set[YamlPath] = set()

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        locked = self._editing is not None
        with Vertical():
            yield Static(self._title, classes="title")
            yield Label("alias")
            yield Input(
                value=self._alias, placeholder="ej. clientes_sql", id="alias", disabled=locked
            )
            yield Static("", classes="err", id="err-alias")
            yield Label("kind")
            yield Select(
                _KIND_OPTIONS, value=self._kind, allow_blank=False, id="kind", disabled=locked
            )
            yield Vertical(*self._compose_fields(self._kind), id="fields")
            yield Vertical(*self._compose_sites(self._kind), id="sites")
            with Horizontal(classes="btns"):
                yield Button("cancelar", id="cancel")
                yield Button("guardar", variant="primary", id="save")

    def on_mount(self) -> None:
        target = "#f-host" if self._editing else "#alias"
        self.query_one(target, Input).focus()

    def _compose_fields(self, kind: str) -> list[Label | Input | Static]:
        """Label + Input + Static de error por cada campo de la kind."""
        widgets: list[Label | Input | Static] = []
        for name in KIND_FIELDS[kind]:
            widgets.append(Label(name))
            widgets.append(
                Input(
                    value=self._fields.get(name, ""),
                    placeholder=field_default(kind, name),
                    id=f"f-{name}",
                )
            )
            widgets.append(Static("", classes="err", id=f"err-{name}"))
        return widgets

    def _compose_sites(self, kind: str) -> list[Static | Checkbox]:
        """Un checkbox por sitio de la kind; el id es la posición en ``sites``."""
        boxes: list[Static | Checkbox] = []
        for n, site in enumerate(self._sites):
            if site.kind != kind:
                continue
            checked = self._uses_this(site) or site.path in self._preselect
            boxes.append(Checkbox(site.label, value=checked, id=f"site-{n}"))
        if boxes:
            boxes.insert(0, Static("usar esta conexión en:", classes="sites-title"))
        return boxes

    def _uses_this(self, site: Site) -> bool:
        return self._editing is not None and site.current == self._editing

    # ------------------------------------------------------------ eventos

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "kind" or event.value == self._kind:
            return
        self._kind = event.value  # type: ignore[assignment]
        fields = self.query_one("#fields", Vertical)
        sites = self.query_one("#sites", Vertical)
        await fields.remove_children()
        await sites.remove_children()
        await fields.mount_all(self._compose_fields(self._kind))
        await sites.mount_all(self._compose_sites(self._kind))

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        site = self._site_of(event.checkbox)
        if event.value or site is None or not self._uses_this(site):
            return
        # Bloqueado: el sitio quedaría sin conexión. Se re-marca y se avisa.
        event.checkbox.value = True
        self.app.notify(_BLOCKED_HINT, severity="warning")

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        elif event.button.id == "cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    # ------------------------------------------------------------ guardar

    def _site_of(self, box: Checkbox) -> Site | None:
        n = (box.id or "").removeprefix("site-")
        return self._sites[int(n)] if n.isdigit() and int(n) < len(self._sites) else None

    def _read_draft(self) -> ConnectionDraft:
        alias = self._editing or self.query_one("#alias", Input).value
        fields = {
            name: self.query_one(f"#f-{name}", Input).value for name in KIND_FIELDS[self._kind]
        }
        return ConnectionDraft(alias=alias.strip(), kind=self._kind, fields=fields)

    def _show_errors(self, errors: DraftErrors) -> None:
        for static in self.query(".err"):
            assert isinstance(static, Static)
            name = (static.id or "").removeprefix("err-")
            static.update(errors.get(name, ""))

    def _save(self) -> None:
        draft = self._read_draft()
        errors = validate_draft(draft, existing=self._existing, editing=self._editing)
        self._show_errors(errors)
        if errors:
            return
        self.use_at = {
            site.path
            for box in self.query(Checkbox)
            if box.value and (site := self._site_of(box)) is not None
        }
        self.dismiss(draft)
