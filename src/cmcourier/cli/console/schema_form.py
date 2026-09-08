"""Formulario derivado del schema pydantic — la parte pura (139 REQ-001).

Sin Textual. Camina ``PipelineConfig.model_fields`` recursivamente y
produce un árbol de nodos (``FieldSpec`` / ``Section`` / ``ListSection``
/ ``DictSection``) que la pantalla [9] YAML renderiza como widgets. El
schema es la ÚNICA fuente de verdad: un campo nuevo en ``schema.py``
aparece en el formulario sin tocar este módulo.

Además: ``coerce`` (texto del widget → valor tipado, tolerante: si no
parsea devuelve el texto y pydantic lo rechaza al validar) y
``diff_edits`` (``original`` vs ``working`` → los ``Edit`` mínimos que
``YamlDocument`` (137) aplica sin perder comentarios).
"""

from __future__ import annotations

__all__ = [
    "DictSection",
    "FieldSpec",
    "Kind",
    "ListSection",
    "Node",
    "Section",
    "build_form",
    "coerce",
    "diff_edits",
]

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import UnionType
from typing import Annotated, Final, Literal, TypeGuard, Union, get_args, get_origin

from annotated_types import Ge, Gt, Le, Lt, MinLen
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from cmcourier.config.schema import As400ConnectionConfig, MssqlConnectionConfig
from cmcourier.config.yaml_doc import DELETE, MISSING, Edit, YamlPath

Kind = Literal["str", "int", "float", "bool", "choice", "path", "str_list", "str_map", "connection"]

_CONNECTIONS_PATH: Final[YamlPath] = ("connections",)
_CONNECTIONS_HINT: Final = "Se edita en [2] CREDENCIALES"
_CONNECTION_MODELS: Final[dict[type[BaseModel], str]] = {
    As400ConnectionConfig: "as400",
    MssqlConnectionConfig: "mssql",
}
_BOOL_WORDS: Final[dict[str, bool]] = {
    "true": True,
    "false": False,
    "sí": True,
    "si": True,
    "no": False,
    "1": True,
    "0": False,
}


# ------------------------------------------------------------ nodos


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Un campo escalar del formulario (un widget)."""

    path: YamlPath
    label: str
    kind: Kind
    choices: tuple[str, ...] = ()
    default: object = None
    required: bool = False
    description: str = ""
    nullable: bool = False
    constraints: str = ""
    # ``True`` para el ``kind`` de una unión discriminada: cambiarlo
    # re-renderiza la sección con los campos de la variante elegida.
    discriminator: bool = False


@dataclass(frozen=True, slots=True)
class Section:
    """Un sub-modelo (``BaseModel``) — un ``Collapsible`` con sus hijos."""

    path: YamlPath
    label: str
    description: str = ""
    children: list[Node] = field(default_factory=list)
    # ``connections`` se edita en [2] (138): se muestra, no se toca.
    readonly: bool = False


@dataclass(frozen=True, slots=True)
class ListSection:
    """``list[Modelo]`` / ``tuple[Modelo, ...]`` — items con agregar / quitar.

    ``discriminator`` + ``choices`` cuando el item es una unión discriminada
    (``metadata.sources``); ``item_model`` es la primera variante.
    """

    path: YamlPath
    label: str
    item_model: type[BaseModel]
    discriminator: str | None = None
    choices: tuple[str, ...] = ()
    items: list[Section] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DictSection:
    """``dict[str, Modelo]`` (``metadata.field_sources``) — entradas con clave."""

    path: YamlPath
    label: str
    value_model: type[BaseModel]
    items: list[Section] = field(default_factory=list)


Node = FieldSpec | Section | ListSection | DictSection


# ------------------------------------------------------------ introspección


@dataclass(frozen=True, slots=True)
class _Resolved:
    """Un annotation ya desarmado: tipo base, metadata y si admite ``None``."""

    tp: object
    meta: tuple[object, ...]
    nullable: bool
    discriminator: str | None

    @property
    def union_args(self) -> tuple[object, ...]:
        return get_args(self.tp) if get_origin(self.tp) in (Union, UnionType) else ()


def _resolve(annotation: object, meta: Sequence[object] = ()) -> _Resolved:
    """Pela ``Annotated`` (juntando metadata) y el ``None`` de las uniones."""
    collected = list(meta)
    nullable = False
    discriminator = None
    while True:
        if get_origin(annotation) is Annotated:
            annotation, *extra = get_args(annotation)
            collected.extend(extra)
            continue
        if get_origin(annotation) in (Union, UnionType):
            args = [a for a in get_args(annotation) if a is not type(None)]
            if len(args) < len(get_args(annotation)):
                nullable = True
            if len(args) == 1:
                annotation = args[0]
                continue
            annotation = Union[tuple(args)]  # noqa: UP007 — se arma en runtime
        break
    for item in collected:
        if isinstance(item, FieldInfo) and item.discriminator:
            discriminator = str(item.discriminator)
    return _Resolved(annotation, tuple(collected), nullable, discriminator)


def _is_model(tp: object) -> TypeGuard[type[BaseModel]]:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _kind_of(model: type[BaseModel], discriminator: str) -> str:
    """El valor ``Literal`` del discriminador de una variante (``"csv"``…)."""
    literal = model.model_fields[discriminator].annotation
    return str(get_args(literal)[0])


def _variant(
    models: Sequence[type[BaseModel]], data: object, discriminator: str
) -> type[BaseModel]:
    """La variante que corresponde a ``data`` — la primera si no hay ``kind``
    (mismo criterio que ``_inject_default_kinds`` del loader)."""
    wanted = data.get(discriminator) if isinstance(data, Mapping) else None
    for model in models:
        if wanted == _kind_of(model, discriminator):
            return model
    return models[0]


def _constraints(meta: Sequence[object]) -> str:
    """``ge``/``gt``/``le``/``lt``/``min_length`` como texto corto (``"lo..hi"``)."""
    ge = next((m.ge for m in meta if isinstance(m, Ge)), None)
    le = next((m.le for m in meta if isinstance(m, Le)), None)
    if ge is not None and le is not None:
        return f"{ge}..{le}"
    parts: list[str] = []
    for item in meta:
        if isinstance(item, Ge):
            parts.append(f">= {item.ge}")
        elif isinstance(item, Gt):
            parts.append(f"> {item.gt}")
        elif isinstance(item, Le):
            parts.append(f"<= {item.le}")
        elif isinstance(item, Lt):
            parts.append(f"< {item.lt}")
        elif isinstance(item, MinLen):
            parts.append(f"len >= {item.min_length}")
    return ", ".join(parts)


def _scalar_kind(tp: object) -> tuple[Kind, tuple[str, ...]]:
    origin = get_origin(tp)
    if origin is Literal:
        return "choice", tuple(str(a) for a in get_args(tp))
    if origin in (list, tuple):
        return "str_list", ()
    if origin is dict:
        return "str_map", ()
    if tp is bool:
        return "bool", ()
    if tp is int:
        return "int", ()
    if tp is float:
        return "float", ()
    if isinstance(tp, type) and issubclass(tp, Path):
        return "path", ()
    return "str", ()


def _connection_kind(resolved: _Resolved, owner: type[BaseModel]) -> str | None:
    """``"as400"``/``"mssql"`` si el campo es una conexión (inline o alias); ``None`` si no."""
    for arg in resolved.union_args:
        if _is_model(arg) and arg in _CONNECTION_MODELS:
            return _CONNECTION_MODELS[arg]
    if resolved.tp is str and "kind" in owner.model_fields:
        return _kind_of(owner, "kind")
    return None


# ------------------------------------------------------------ walker


def build_form(model: type[BaseModel], data: object, *, path: YamlPath = ()) -> list[Node]:
    """Un nodo por campo de ``model``, con ``data`` (YAML plano) decidiendo
    variantes e items. ``data`` que no es mapping se trata como vacío."""
    values = data if isinstance(data, Mapping) else {}
    return [
        _node_for_field(model, name, info, values.get(name), (*path, name))
        for name, info in model.model_fields.items()
    ]


def _node_for_field(
    owner: type[BaseModel], name: str, info: FieldInfo, value: object, path: YamlPath
) -> Node:
    description = info.description or ""
    if path == _CONNECTIONS_PATH:
        return Section(path, name, _CONNECTIONS_HINT, [], readonly=True)
    resolved = _resolve(info.annotation, info.metadata)
    discriminator = info.discriminator or resolved.discriminator
    if discriminator and resolved.union_args:
        models = [a for a in resolved.union_args if _is_model(a)]
        children = _discriminated_children(models, value, path, str(discriminator))
        return Section(path, name, description, children)
    if _is_model(resolved.tp):
        return Section(path, name, description, build_form(resolved.tp, value, path=path))
    container = _container_node(resolved, name, value, path)
    if container is not None:
        return container
    return _field_spec(owner, name, info, resolved, path, description)


def _field_spec(
    owner: type[BaseModel],
    name: str,
    info: FieldInfo,
    resolved: _Resolved,
    path: YamlPath,
    description: str,
) -> FieldSpec:
    connection = _connection_kind(resolved, owner) if name.endswith("connection") else None
    if connection is not None:
        kind: Kind = "connection"
        choices: tuple[str, ...] = (connection,)
    else:
        kind, choices = _scalar_kind(resolved.tp)
    default = None if info.default is PydanticUndefined else info.default
    return FieldSpec(
        path,
        name,
        kind,
        choices=choices,
        default=default,
        required=info.is_required(),
        description=description,
        nullable=resolved.nullable,
        constraints=_constraints(resolved.meta),
    )


def _container_node(resolved: _Resolved, name: str, value: object, path: YamlPath) -> Node | None:
    """``list``/``tuple``/``dict``: sección de items si el elemento es un
    modelo (o unión discriminada); campo ``str_list``/``str_map`` si no."""
    origin = get_origin(resolved.tp)
    args = get_args(resolved.tp)
    if origin in (list, tuple) and args:
        item = _resolve(args[0])
        models = _models_of(item)
        if not models:
            return None
        return _list_section(name, item, models, value, path)
    if origin is dict and len(args) == 2:
        models = _models_of(_resolve(args[1]))
        if not models:
            return None
        entries = value.items() if isinstance(value, Mapping) else ()
        items = [_item_section(models[0], sub, (*path, str(key)), str(key)) for key, sub in entries]
        return DictSection(path, name, models[0], items)
    return None


def _models_of(resolved: _Resolved) -> list[type[BaseModel]]:
    if _is_model(resolved.tp):
        return [resolved.tp]
    return [a for a in resolved.union_args if _is_model(a)]


def _list_section(
    name: str, item: _Resolved, models: list[type[BaseModel]], value: object, path: YamlPath
) -> ListSection:
    entries = list(value) if isinstance(value, Sequence) and not isinstance(value, str) else []
    discriminator = item.discriminator
    choices = tuple(_kind_of(m, discriminator) for m in models) if discriminator else ()
    items = [
        _item_section(models, e, (*path, i), f"[{i}]", discriminator) for i, e in enumerate(entries)
    ]
    return ListSection(path, name, models[0], discriminator, choices, items)


def _item_section(
    models: type[BaseModel] | Sequence[type[BaseModel]],
    data: object,
    path: YamlPath,
    label: str,
    discriminator: str | None = None,
) -> Section:
    """Un item de lista / entrada de dict: sección sin descripción con sus campos."""
    if isinstance(models, type):
        return Section(path, label, "", build_form(models, data, path=path))
    if discriminator:
        return Section(path, label, "", _discriminated_children(models, data, path, discriminator))
    return Section(path, label, "", build_form(models[0], data, path=path))


def _discriminated_children(
    models: Sequence[type[BaseModel]], data: object, path: YamlPath, discriminator: str
) -> list[Node]:
    """El ``kind`` como choice con TODAS las variantes + los campos de la elegida."""
    variant = _variant(models, data, discriminator)
    kind_path = (*path, discriminator)
    kind_info = variant.model_fields[discriminator]
    head = FieldSpec(
        kind_path,
        discriminator,
        "choice",
        choices=tuple(_kind_of(m, discriminator) for m in models),
        required=True,
        description=kind_info.description or "",
        discriminator=True,
    )
    rest = [node for node in build_form(variant, data, path=path) if node.path != kind_path]
    return [head, *rest]


# ------------------------------------------------------------ coerce


def coerce(spec: FieldSpec, raw: str) -> object:
    """Texto del widget → valor para ``working``.

    Vacío → ``MISSING`` (se borra la clave y aplica el default) o ``None``
    si el campo admite ``None``. Lo que no parsea vuelve como texto para
    que pydantic lo rechace con su mensaje al validar.
    """
    text = raw.strip()
    if not text:
        return None if spec.nullable else MISSING
    if spec.kind == "int":
        return _parse(int, text)
    if spec.kind == "float":
        return _parse(float, text)
    if spec.kind == "bool":
        return _BOOL_WORDS.get(text.lower(), text)
    if spec.kind == "str_list":
        return [part.strip() for part in text.split(",") if part.strip()]
    if spec.kind == "str_map":
        return _parse_map(text)
    return text


def _parse(cast: type[int] | type[float], text: str) -> object:
    try:
        return cast(text)
    except ValueError:
        return text


def _parse_map(text: str) -> object:
    """Líneas ``clave: valor``; una línea sin ``:`` invalida el bloque entero."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            return text
        out[key.strip()] = value.strip()
    return out


# ------------------------------------------------------------ diff


def diff_edits(
    original: Mapping[str, object], working: Mapping[str, object], *, path: YamlPath = ()
) -> list[Edit]:
    """Los ``Edit`` mínimos que llevan ``original`` a ``working``.

    Mappings recursivos; escalares con tipo (``True`` ≠ ``1``); listas de
    mappings por índice (un item cambiado se reemplaza entero, los que
    sobran se borran del índice más alto hacia abajo); listas escalares
    como un todo.
    """
    edits: list[Edit] = []
    for key, value in working.items():
        step = (*path, key)
        if key not in original:
            edits.append(Edit(step, deepcopy(value)))
        else:
            edits.extend(_diff_value(original[key], value, step))
    edits.extend(Edit((*path, key), DELETE) for key in original if key not in working)
    return edits


def _diff_value(old: object, new: object, path: YamlPath) -> list[Edit]:
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        return diff_edits(old, new, path=path)
    if isinstance(old, list) and isinstance(new, list):
        return _diff_list(old, new, path)
    return [] if _equal(old, new) else [Edit(path, deepcopy(new))]


def _diff_list(old: list[object], new: list[object], path: YamlPath) -> list[Edit]:
    if not any(isinstance(item, Mapping) for item in (*old, *new)):
        return [] if _equal(old, new) else [Edit(path, deepcopy(new))]
    edits = [
        Edit((*path, i), deepcopy(item))
        for i, item in enumerate(new)
        if i >= len(old) or not _equal(old[i], item)
    ]
    edits.extend(Edit((*path, i), DELETE) for i in range(len(old) - 1, len(new) - 1, -1))
    return edits


def _equal(a: object, b: object) -> bool:
    """Igualdad profunda que distingue tipos (``True`` vs ``1``, ``1`` vs ``1.0``)."""
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b
