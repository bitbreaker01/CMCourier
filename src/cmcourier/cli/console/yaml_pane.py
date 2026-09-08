"""Pantalla [9] YAML — edición completa de la configuración (139 REQ-002..004).

El formulario sale de ``build_form`` (schema pydantic → nodos) y se
edita sobre ``working`` (dict plano del YAML). ``v`` valida con
``PipelineConfig`` y pinta los errores por fila; ``w`` confirma y escribe
sólo el ``diff_edits(original, working)`` vía ``YamlDocument`` (137), así
comentarios y orden del archivo sobreviven. ``connections`` se muestra
pero se edita en [2] (138).
"""

from __future__ import annotations

__all__ = ["SectionBox", "YamlPane"]

import contextlib
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Input, Label, Select, Static, TextArea

from cmcourier.cli.console.connection_edit import value_text, write_error_text
from cmcourier.cli.console.schema_form import (
    DictSection,
    FieldSpec,
    ListSection,
    Node,
    Section,
    build_form,
    coerce,
    diff_edits,
)
from cmcourier.config.loader import _inject_default_kinds, load_config
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.yaml_doc import (
    MISSING,
    YamlDocument,
    YamlDocumentError,
    YamlPath,
    YamlWriteError,
    apply_edits,
    to_plain,
)

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

_KEEP: Final = object()  # el Select eligió algo que no toca ``working``
_RUN_ACTIVE_MSG: Final = "Hay una corrida activa — el YAML se escribe cuando termine"
_NAME_KEYS: Final = ("alias", "name")


class SectionBox(Collapsible):
    """Un ``Collapsible`` que sabe qué path del YAML representa
    (re-render de un subárbol, abrir ancestros de un error)."""

    def __init__(self, *children: Widget, path: YamlPath, title: str, collapsed: bool) -> None:
        super().__init__(*children, title=title, collapsed=collapsed)
        self.path = path


class YamlPane(Vertical):
    DEFAULT_CSS = """
    YamlPane { height: 1fr; }
    #yaml-head { height: auto; padding: 0 1; color: $text-muted; }
    #form { height: 1fr; }
    YamlPane .yamlrow { height: auto; }
    YamlPane .yamlrow > Horizontal { height: auto; }
    YamlPane .yamlrow Label { width: 26; padding: 1 1 0 0; }
    YamlPane .yamlrow Input, YamlPane .yamlrow Select { width: 1fr; }
    YamlPane .yamlrow TextArea { width: 1fr; height: 5; }
    YamlPane .err { color: $error; padding: 0 0 0 26; height: auto; }
    YamlPane .actions { height: auto; }
    YamlPane .actions Select, YamlPane .actions Input { width: 28; }
    YamlPane .ro { color: $text-muted; padding: 0 1; }
    """

    INLINE: Final = "(inline — editar en [2])"
    NONE: Final = "(ninguna)"

    def __init__(self, console: ConsoleApp) -> None:
        super().__init__()
        self.console = console
        self.original: dict[str, Any] = {}
        self.working: dict[str, Any] = {}
        self.rows: dict[str, tuple[YamlPath, FieldSpec]] = {}
        self._actions: dict[str, tuple[str, YamlPath]] = {}
        self._seq = 0
        self._mtime: float | None = None
        self._validation = ""
        self._rendered = False

    def compose(self) -> ComposeResult:
        yield Static("", id="yaml-head")
        yield VerticalScroll(id="form")

    def on_mount(self) -> None:
        self._refresh_head()

    # ------------------------------------------------------------ consultas

    @property
    def dirty(self) -> bool:
        return self.working != self.original

    def row_id(self, path: YamlPath) -> str:
        """El id del widget de la fila con ese path (``KeyError`` si no hay)."""
        for rid, (row_path, _) in self.rows.items():
            if row_path == path:
                return rid
        raise KeyError(path)

    def err_id(self, path: YamlPath) -> str:
        return self.row_id(path).replace("row-", "err-", 1)

    def box(self, path: YamlPath) -> SectionBox:
        for candidate in self.query(SectionBox):
            if candidate.path == path:
                return candidate
        raise KeyError(path)

    # ------------------------------------------------------------ recarga (REQ-004)

    async def reload_if_clean(self) -> None:
        """Relee el disco si no hay ediciones pendientes (o nunca se renderizó)."""
        if self._rendered and self.dirty:
            self._refresh_head()
            return
        try:
            doc = YamlDocument.load(self.console.config_path)
        except (YamlDocumentError, OSError) as exc:
            self.console.notify(f"No se pudo leer el YAML: {exc}", severity="error", timeout=8)
            return
        plain = to_plain(doc.root)
        data: dict[str, Any] = plain if isinstance(plain, dict) else {}
        if self._rendered and data == self.original:
            self._refresh_head()
            return
        self.original = deepcopy(data)
        self.working = deepcopy(data)
        self._mtime = self._disk_mtime()
        self._validation = ""
        await self.render_form()

    async def render_form(self) -> None:
        """Vacía ``#form`` y lo monta de nuevo desde ``working`` (``render`` es de Textual)."""
        form = self.query_one("#form", VerticalScroll)
        open_paths = self._open_paths() if self._rendered else None
        self.rows.clear()
        self._actions.clear()
        await form.remove_children()
        nodes = build_form(PipelineConfig, self.working)
        await form.mount(*[self._build(node, open_paths) for node in nodes])
        self._rendered = True
        self._refresh_head()

    def _open_paths(self) -> set[YamlPath]:
        return {box.path for box in self.query(SectionBox) if not box.collapsed}

    def _disk_mtime(self) -> float | None:
        with contextlib.suppress(OSError):
            return self.console.config_path.stat().st_mtime
        return None

    def _refresh_head(self) -> None:
        changes = len(diff_edits(self.original, self.working))
        parts = [str(self.console.config_path), f"{changes} cambios" if changes else "sin cambios"]
        if self._validation:
            parts.append(self._validation)
        if changes and self._mtime is not None and self._disk_mtime() != self._mtime:
            parts.append("⚠ el archivo cambió en disco (tus cambios no se pisan)")
        parts.append("v validar · w escribir · u descartar")
        with contextlib.suppress(Exception):
            self.query_one("#yaml-head", Static).update("  ·  ".join(parts))

    # ------------------------------------------------------------ construcción

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def _collapsed(self, path: YamlPath, open_paths: set[YamlPath] | None) -> bool:
        return len(path) > 1 if open_paths is None else path not in open_paths

    def _build(self, node: Node, open_paths: set[YamlPath] | None) -> Widget:
        if isinstance(node, FieldSpec):
            return self._build_row(node)
        if isinstance(node, Section):
            return self._build_section(node, open_paths)
        if isinstance(node, ListSection):
            return self._build_list(node, open_paths)
        return self._build_dict(node, open_paths)

    def _build_section(
        self, node: Section, open_paths: set[YamlPath] | None, *, removable: bool = False
    ) -> SectionBox:
        collapsed = self._collapsed(node.path, open_paths)
        if node.readonly:
            body = Static(f"{node.description}\n{self._connections_text()}", classes="ro")
            return SectionBox(body, path=node.path, title=node.label, collapsed=collapsed)
        children = [self._build(child, open_paths) for child in node.children]
        if removable:
            children.append(self._button("quitar", "delbtn", "del", node.path))
        title = self._item_title(node) if removable else node.label
        return SectionBox(*children, path=node.path, title=title, collapsed=collapsed)

    def _item_title(self, node: Section) -> str:
        data = self._get(node.path)
        if not isinstance(data, Mapping):
            return node.label
        name = next((str(data[k]) for k in _NAME_KEYS if data.get(k)), "")
        kind = str(data.get("kind") or "")
        return " ".join(part for part in (node.label, name, kind) if part)

    def _build_list(self, node: ListSection, open_paths: set[YamlPath] | None) -> SectionBox:
        items = [self._build_section(item, open_paths, removable=True) for item in node.items]
        actions: list[Widget] = []
        if node.discriminator:
            actions.append(
                Select(
                    [(c, c) for c in node.choices],
                    allow_blank=False,
                    value=node.choices[0],
                    classes="addkind",
                    id=self._new_id("kind"),
                )
            )
        actions.append(self._button("+ agregar", "addbtn", "add", node.path))
        return SectionBox(
            *items,
            Horizontal(*actions, classes="actions"),
            path=node.path,
            title=f"{node.label} ({len(node.items)})",
            collapsed=self._collapsed(node.path, open_paths),
        )

    def _build_dict(self, node: DictSection, open_paths: set[YamlPath] | None) -> SectionBox:
        items = [self._build_section(item, open_paths, removable=True) for item in node.items]
        actions = Horizontal(
            Input(placeholder="clave nueva", classes="addkey", id=self._new_id("key")),
            self._button("+ agregar", "addbtn", "add", node.path),
            classes="actions",
        )
        return SectionBox(
            *items,
            actions,
            path=node.path,
            title=f"{node.label} ({len(node.items)})",
            collapsed=self._collapsed(node.path, open_paths),
        )

    def _button(self, label: str, cls: str, action: str, path: YamlPath) -> Button:
        bid = self._new_id("act")
        self._actions[bid] = (action, path)
        return Button(label, id=bid, classes=cls, compact=True)

    def _build_row(self, spec: FieldSpec) -> Vertical:
        rid = self._new_id("row")
        self.rows[rid] = (spec.path, spec)
        widget = self._widget_for(spec, self._get(spec.path), rid)
        if spec.description:
            widget.tooltip = spec.description
        label = Label(spec.label + (" ?" if spec.description else ""))
        err = Static("", id=rid.replace("row-", "err-", 1), classes="err")
        err.display = False
        return Vertical(Horizontal(label, widget), err, classes="yamlrow")

    def _widget_for(self, spec: FieldSpec, current: object, rid: str) -> Widget:
        if spec.kind == "bool":
            value: Any = current if isinstance(current, bool) else Select.BLANK
            options: list[tuple[str, Any]] = [("sí", True), ("no", False)]
            return Select(options, prompt=_placeholder(spec), value=value, id=rid)
        if spec.kind == "choice":
            value = current if current in spec.choices else Select.BLANK
            options = [(c, c) for c in spec.choices]
            allow_blank = not spec.required or value is Select.BLANK
            return Select(
                options, prompt=_placeholder(spec), allow_blank=allow_blank, value=value, id=rid
            )
        if spec.kind == "connection":
            return self._connection_select(spec, current, rid)
        if spec.kind == "str_map":
            return TextArea(_map_text(current), id=rid)
        return Input(value=_input_text(current), placeholder=_placeholder(spec), id=rid)

    def _connection_select(self, spec: FieldSpec, current: object, rid: str) -> Select[str]:
        """Alias del registro (de ``working``, no de ``app.config``) de la
        kind que pide el sitio + inline / ninguna según corresponda."""
        kind = spec.choices[0] if spec.choices else None
        registry = self.working.get("connections")
        aliases = [
            str(alias)
            for alias, conn in (registry.items() if isinstance(registry, Mapping) else ())
            if isinstance(conn, Mapping) and (kind is None or conn.get("kind") == kind)
        ]
        if isinstance(current, str) and current not in aliases:
            aliases.append(current)
        options = [(alias, alias) for alias in aliases]
        if isinstance(current, Mapping):
            options.append((self.INLINE, self.INLINE))
        if spec.nullable:
            options.append((self.NONE, self.NONE))
        value: Any = Select.BLANK
        if isinstance(current, str):
            value = current
        elif isinstance(current, Mapping):
            value = self.INLINE
        elif current is None and spec.nullable:
            value = self.NONE
        return Select(options, prompt="(elegir alias)", value=value, id=rid)

    def _connections_text(self) -> str:
        registry = self.working.get("connections")
        if not isinstance(registry, Mapping) or not registry:
            return "(registro vacío)"
        return "  ".join(
            f"{alias} ({conn.get('kind', '?') if isinstance(conn, Mapping) else '?'})"
            for alias, conn in registry.items()
        )

    # ------------------------------------------------------------ eventos

    def on_input_changed(self, event: Input.Changed) -> None:
        row = self.rows.get(event.input.id or "")
        if row is not None:
            self._apply(row[0], coerce(row[1], event.value))

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        row = self.rows.get(event.text_area.id or "")
        if row is not None:
            self._apply(row[0], coerce(row[1], event.text_area.text))

    def on_select_changed(self, event: Select.Changed) -> None:
        row = self.rows.get(event.select.id or "")
        if row is None:
            return  # el Select de kind de "+ agregar" no toca ``working``
        path, spec = row
        value = self._select_value(spec, event.value)
        if value is _KEEP:
            return
        if spec.discriminator:
            # Un Select recién montado también avisa: sólo re-render si la
            # variante cambia respecto de la que está dibujada.
            current = self._get(path)
            shown = spec.choices[0] if current is MISSING else current
            if value != shown:
                self._change_kind(path, str(value))
            return
        self._apply(path, value)

    def _select_value(self, spec: FieldSpec, raw: object) -> object:
        if raw is Select.BLANK:
            return None if spec.nullable else MISSING
        if spec.kind == "connection":
            if raw == self.INLINE:
                return _KEEP
            if raw == self.NONE:
                return None
        return raw

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = self._actions.get(event.button.id or "")
        if action is None:
            return
        kind, path = action
        if kind == "del":
            self._remove_item(path)
        elif kind == "add":
            parent = event.button.parent
            assert isinstance(parent, Widget)
            self._add(path, parent)

    # ------------------------------------------------------------ mutaciones de working

    def _get(self, path: YamlPath) -> object:
        node: object = self.working
        for step in path:
            node = _child(node, step)
            if node is MISSING:
                return MISSING
        return node

    def _set(self, path: YamlPath, value: object) -> None:
        node: Any = self.working
        for step in path[:-1]:
            if isinstance(step, str) and not isinstance(node.get(step), dict | list):
                node[step] = {}
            node = node[step]
        last = path[-1]
        if isinstance(last, int) and isinstance(node, list) and last == len(node):
            node.append(value)
        else:
            node[last] = value

    def _delete(self, path: YamlPath) -> None:
        parent = self._get(path[:-1])
        last = path[-1]
        if isinstance(parent, dict) and isinstance(last, str):
            parent.pop(last, None)
        elif isinstance(parent, list) and isinstance(last, int) and 0 <= last < len(parent):
            del parent[last]

    def _apply(self, path: YamlPath, value: object) -> None:
        """``working[path] = value`` (o borra la clave si ``MISSING``); no-op
        si no cambia nada — un Input recién montado también avisa."""
        current = self._get(path)
        if value is MISSING:
            if current is MISSING:
                return
            self._delete(path)
        else:
            if current is not MISSING and _same(current, value):
                return
            self._set(path, value)
        self._refresh_head()

    def _change_kind(self, kind_path: YamlPath, kind: str) -> None:
        """REQ-002: el item queda ``{kind: nuevo, alias/name: <si tenía>}`` y se re-renderiza."""
        section = kind_path[:-1]
        current = self._get(section)
        item: dict[str, Any] = {"kind": kind}
        if isinstance(current, Mapping):
            item.update({k: current[k] for k in _NAME_KEYS if k in current})
        self._set(section, item)
        self._rerender(section)

    def _add(self, path: YamlPath, actions: Widget) -> None:
        node = self._node_at(path)
        if isinstance(node, ListSection):
            kind = actions.query_one("Select.addkind", Select).value if node.discriminator else None
            items = self._get(path)
            if not isinstance(items, list):
                items = []
                self._set(path, items)
            items.append({"kind": kind} if isinstance(kind, str) else {})
            self._rerender(path, open_extra=((*path, len(items) - 1),))
        elif isinstance(node, DictSection):
            key_input = actions.query_one("Input.addkey", Input)
            key = key_input.value.strip()
            if not key:
                self.console.notify("Escribí la clave nueva primero", severity="warning")
                return
            if self._get((*path, key)) is not MISSING:
                self.console.notify(f"{key} ya existe", severity="warning")
                return
            self._set((*path, key), {})
            self._rerender(path, open_extra=((*path, key),))

    def _remove_item(self, item_path: YamlPath) -> None:
        self._delete(item_path)
        self._rerender(item_path[:-1])

    def _rerender(self, path: YamlPath, *, open_extra: Iterable[YamlPath] = ()) -> None:
        """Reemplaza el ``SectionBox`` de ``path`` por uno nuevo desde ``working``."""
        old = self.box(path)
        open_paths = self._open_paths() | set(open_extra) | {path}
        self._prune(path)
        new = self._build(self._node_at(path), open_paths)
        parent = old.parent
        assert isinstance(parent, Widget)
        parent.mount(new, after=old)
        old.remove()
        self._refresh_head()

    def _prune(self, prefix: YamlPath) -> None:
        n = len(prefix)
        self.rows = {rid: row for rid, row in self.rows.items() if row[0][:n] != prefix}
        self._actions = {bid: act for bid, act in self._actions.items() if act[1][:n] != prefix}

    def _node_at(self, path: YamlPath) -> Node:
        nodes: Sequence[Node] = build_form(PipelineConfig, self.working)
        while True:
            for node in nodes:
                if node.path == path:
                    return node
                if path[: len(node.path)] == node.path:
                    nodes = node.children if isinstance(node, Section) else _items(node)
                    break
            else:
                raise KeyError(path)

    # ------------------------------------------------------------ descartar (I5)

    async def discard(self) -> None:
        """``u`` en [9]: ``working`` vuelve a ``original`` y el formulario se
        redibuja entero.

        I5: cambiar un discriminador (``kind``) deja el item en
        ``{kind: nuevo}`` y se lleva puesto el bloque que había — sin esto,
        el operador tenía que salir de la consola para recuperarlo.
        """
        if not self.dirty:
            self.console.notify("El YAML no tiene cambios pendientes (sin cambios)")
            return
        self.working = deepcopy(self.original)
        self._validation = ""
        await self.render_form()
        self.console.notify("Cambios descartados — el formulario volvió al archivo en disco")

    # ------------------------------------------------------------ validar (REQ-003)

    def validate(self) -> int:
        """Valida ``working`` como lo haría ``load_config``; pinta los errores
        por fila (prefijo más largo) y devuelve cuántos hubo."""
        data = deepcopy(self.working)
        _inject_default_kinds(data)
        self._clear_errors()
        errors: list[dict[str, Any]] = []
        try:
            PipelineConfig.model_validate(data)
        except ValidationError as exc:
            errors = [dict(e) for e in exc.errors()]
        loose: list[str] = []
        for err in errors:
            loc = self._normalise_loc(tuple(err["loc"]), missing=err.get("type") == "missing")
            rid = self._row_for_loc(loc)
            msg = str(err["msg"])
            if rid is None:
                loose.append(f"{'.'.join(map(str, loc)) or '<raíz>'}: {msg}")
            else:
                self._show_error(rid, msg)
        self._validation = "válido ✓" if not errors else f"{len(errors)} errores"
        if loose:
            self._validation += " — " + "; ".join(loose)
        self._refresh_head()
        return len(errors)

    def _normalise_loc(self, loc: tuple[Any, ...], *, missing: bool) -> YamlPath:
        """Saca los tags de unión (``"csv"``, ``"As400ConnectionConfig"``) que
        pydantic mete en ``loc`` y que no son claves de ``working``. Con
        ``missing`` (``Field required``) la última clave, ausente, sí cuenta."""
        out: list[str | int] = []
        node: object = self.working
        for i, step in enumerate(loc):
            child = _child(node, step)
            if child is not MISSING:
                node = child
            elif not (missing and i == len(loc) - 1 and isinstance(step, str)):
                continue
            out.append(step)
        return tuple(out)

    def _row_for_loc(self, loc: YamlPath) -> str | None:
        best: tuple[int, str] | None = None
        for rid, (path, _) in self.rows.items():
            if loc[: len(path)] == path and (best is None or len(path) > best[0]):
                best = (len(path), rid)
        if best is not None:
            return best[1]
        # Error a nivel de modelo (``exactly one of …``): la primera fila del bloque.
        n = len(loc)
        return next((rid for rid, (path, _) in self.rows.items() if path[:n] == loc), None)

    def _show_error(self, rid: str, msg: str) -> None:
        path = self.rows[rid][0]
        err = self.query_one(f"#{rid.replace('row-', 'err-', 1)}", Static)
        err.update(msg)
        err.display = True
        for box in self.query(SectionBox):
            if path[: len(box.path)] == box.path:
                box.collapsed = False

    def _clear_errors(self) -> None:
        for err in self.query(".err"):
            if isinstance(err, Static):
                err.update("")
                err.display = False

    # ------------------------------------------------------------ escribir (REQ-003)

    async def write(self) -> None:
        """``w`` en [9]: sin cambios / inválido / corrida activa → aviso; si no, confirma."""
        console = self.console
        if not self.dirty:
            console.notify("El YAML no tiene cambios pendientes (sin cambios)", severity="warning")
            return
        errors = self.validate()
        if errors:
            console.notify(
                f"El YAML no es válido ({errors} errores) — corregí antes de escribir",
                severity="error",
                timeout=8,
            )
            return
        if console.run_active:
            console.notify(_RUN_ACTIVE_MSG, severity="error")
            return
        changes = len(diff_edits(self.original, self.working))
        console.confirm(
            title="Escribir YAML",
            body=(
                f"{changes} cambios → {console.config_path}\n\n"
                "Se hace un backup config.yaml.bak-<fecha> y se reemplaza el archivo. "
                "Los overrides de sesión ([3]) se conservan."
            ),
            yes="escribir",
            no="cancelar",
            danger=True,
            cb=lambda ok: console.call_later(self._commit) if ok else None,
        )

    async def _commit(self) -> None:
        console = self.console
        edits = diff_edits(self.original, self.working)
        try:
            doc = YamlDocument.load(console.config_path)
            apply_edits(doc, edits)
            result = doc.write(verify=load_config)
        except (YamlDocumentError, YamlWriteError, OSError) as exc:
            text = write_error_text(exc)
            console.notify(f"No se escribió el YAML: {text}", severity="error", timeout=10)
            return
        console.config = result.value  # los overrides de sesión quedan
        self.original = deepcopy(self.working)
        self._mtime = self._disk_mtime()
        self._validation = "escrito ✓"
        console.state.mark_doctor_stale("cambió el YAML")
        await console.q("CredsPane").rebuild_cards()  # rebuild_conn hace el prefill
        console.q("ConfigPane").refresh_yaml_values()
        console.on_pii_override(None)
        console.refresh_status()
        self._refresh_head()
        console.notify(
            f"YAML actualizado ({len(edits)} cambios) · backup en {result.backup_path}", timeout=8
        )


# ------------------------------------------------------------ helpers puros


def _items(node: Node) -> Sequence[Node]:
    if isinstance(node, ListSection | DictSection):
        return node.items
    return ()


def _child(node: object, step: object) -> object:
    """El hijo ``step`` de un dict/list plano, o ``MISSING``."""
    if isinstance(step, str) and isinstance(node, Mapping) and step in node:
        return node[step]
    if isinstance(step, int) and isinstance(node, list) and 0 <= step < len(node):
        return node[step]
    return MISSING


def _same(a: object, b: object) -> bool:
    """Igualdad tolerante para el no-op de ``_apply`` (``4`` y ``4.0`` son lo
    mismo para el operador; ``True`` y ``1`` no)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _placeholder(spec: FieldSpec) -> str:
    parts: list[str] = []
    if spec.required:
        parts.append("requerido")
    elif spec.default is not None:
        parts.append(f"default: {_input_text(spec.default)}")
    if spec.constraints:
        parts.append(spec.constraints)
    return " · ".join(parts)


def _input_text(value: object) -> str:
    if value is MISSING or value is None:
        return ""
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    if isinstance(value, Mapping):
        return _map_text(value)
    return value_text(value)


def _map_text(value: object) -> str:
    if isinstance(value, Mapping):
        return "\n".join(f"{key}: {val}" for key, val in value.items())
    return _input_text(value)
