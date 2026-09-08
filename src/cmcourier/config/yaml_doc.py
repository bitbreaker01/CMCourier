"""Edición round-trip del YAML de configuración con ``ruamel.yaml`` (137).

Un único escritor para todo lo que toca el YAML desde la consola (135,
138, 139). ``ruamel`` en modo round-trip conserva comentarios, comillas,
orden y anchors; acá sólo agregamos lo que el proyecto necesita y ruamel
no da solo: EOL del archivo, indentación detectada del archivo, línea en
blanco antes de un bloque nuevo de primer nivel, y una escritura atómica
con verificación previa + backup. La verificación semántica es del
llamador (``write(verify=...)``): este módulo no conoce el schema.
"""

from __future__ import annotations

__all__ = [
    "DELETE",
    "MISSING",
    "Edit",
    "WriteResult",
    "YamlDocument",
    "YamlDocumentError",
    "YamlWriteError",
    "apply_edits",
    "to_plain",
]

import io
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Generic, TypeVar

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarbool import ScalarBoolean
from ruamel.yaml.scalarfloat import ScalarFloat
from ruamel.yaml.scalarint import ScalarInt
from ruamel.yaml.scalarstring import ScalarString

T = TypeVar("T")

# Ruta dentro del documento: ``str`` indexa mappings, ``int`` secuencias.
YamlPath = tuple[str | int, ...]


class _Sentinel:
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


MISSING: Final = _Sentinel("MISSING")
DELETE: Final = _Sentinel("DELETE")


class YamlDocumentError(ValueError):
    """El YAML no se pudo cargar o la edición pedida no tiene sentido sobre él."""


class YamlWriteError(RuntimeError):
    """La verificación previa a escribir falló — el archivo original quedó intacto."""


@dataclass(frozen=True, slots=True)
class WriteResult(Generic[T]):
    value: T
    backup_path: Path
    path: Path


@dataclass(frozen=True, slots=True)
class Edit:
    """Una edición pendiente: ``value`` es ``DELETE`` para borrar la clave."""

    path: YamlPath
    value: object


class YamlDocument:
    """Un YAML cargado en modo round-trip, editable por rutas y escribible con verificación."""

    def __init__(self, path: Path, root: CommentedMap, yaml: YAML, eol: str) -> None:
        self._path = path
        self._root = root
        self._yaml = yaml
        self._eol = eol

    @property
    def path(self) -> Path:
        return self._path

    @property
    def root(self) -> CommentedMap:
        return self._root

    @classmethod
    def load(cls, path: Path) -> YamlDocument:
        """Lee ``path`` (resolviendo symlinks) y lo parsea conservando todo.

        M6: el EOL es del ARCHIVO, no de cada línea — con un solo ``\\r\\n``
        adentro el archivo "es CRLF" y se vuelve a escribir entero con CRLF
        (un archivo de EOL mixto ya está roto para git y para el editor).
        """
        # B2 de 135: si el YAML es un symlink, el reemplazo atómico pisaría
        # el link y el archivo real no cambiaría. Trabajamos sobre el real.
        path = path.resolve()
        raw = path.read_bytes().decode("utf-8")
        # I5: conservar el terminador de línea del archivo (CRLF de Windows).
        # ruamel mezcla EOLs si le damos CRLF: normalizamos y reponemos al dumpear.
        eol = "\r\n" if "\r\n" in raw else "\n"
        text = raw.replace("\r\n", "\n")
        mapping, sequence, offset = _detect_indent(text)
        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        yaml.width = 4096
        yaml.indent(mapping=mapping, sequence=sequence, offset=offset)
        yaml.Representer = _NullAsNullRepresenter
        try:
            root = yaml.load(text)
        except DuplicateKeyError as exc:
            raise YamlDocumentError(f"clave duplicada en {path.name}: {exc.problem}") from exc
        except YAMLError as exc:
            raise YamlDocumentError(f"YAML inválido en {path.name}: {exc}") from exc
        if not isinstance(root, CommentedMap):
            raise YamlDocumentError(
                f"la raíz de {path.name} debe ser un mapping, no {type(root).__name__}"
            )
        return cls(path, root, yaml, eol)

    # -- lectura -------------------------------------------------------------

    def get(self, path: YamlPath) -> object:
        """El nodo en ``path`` o ``MISSING`` (también si el camino no cuadra con el tipo)."""
        node: object = self._root
        for step in path:
            in_map = isinstance(step, str) and isinstance(node, CommentedMap) and step in node
            in_seq = (
                isinstance(step, int) and isinstance(node, CommentedSeq) and 0 <= step < len(node)
            )
            if not (in_map or in_seq):
                return MISSING
            node = node[step]  # type: ignore[index]
        return node

    def has(self, path: YamlPath) -> bool:
        return self.get(path) is not MISSING

    # -- edición -------------------------------------------------------------

    def set(self, path: YamlPath, value: object) -> None:
        """Asigna ``value`` en ``path`` creando los mappings intermedios que falten."""
        if not path:
            raise YamlDocumentError("la ruta no puede estar vacía")
        parent = self._ensure_parent(path)
        step = path[-1]
        self._require_not_alias(parent, path[:-1])
        if isinstance(step, int):
            _set_index(parent, step, _to_node(value))
            return
        _require_map(parent, path)
        if parent is self._root and step not in self._root:
            self._add_top_level(step, _to_node(value))
        else:
            parent[step] = _to_node(value)

    def delete(self, path: YamlPath) -> None:
        """Borra ``path``; una clave inexistente es no-op, un índice fuera de rango es error."""
        if not path:
            raise YamlDocumentError("la ruta no puede estar vacía")
        parent = self.get(path[:-1])
        step = path[-1]
        if isinstance(step, int):
            if not isinstance(parent, CommentedSeq):
                raise YamlDocumentError(f"{_fmt(path[:-1])} no es una secuencia")
            if not 0 <= step < len(parent):
                raise YamlDocumentError(f"índice {step} fuera de rango en {_fmt(path[:-1])}")
            self._require_not_alias(parent, path[:-1])
            del parent[step]
            return
        if isinstance(parent, CommentedMap) and step in parent:
            # I1: la clave está pero la pone un ``<<: *base`` — ``del`` no borra
            # nada y el operador se queda pensando que la vació.
            if step not in dict(parent.non_merged_items()):
                raise YamlDocumentError(
                    f"{_fmt(path)} viene de una merge key (<<:) — editalo a mano"
                )
            self._require_not_alias(parent, path[:-1])
            del parent[step]

    def append(self, path: YamlPath, value: object) -> None:
        """Agrega ``value`` al final de la secuencia en ``path`` (la crea si no existe)."""
        if not path:
            raise YamlDocumentError("la ruta no puede estar vacía")
        current = self.get(path)
        if current is MISSING:
            self.set(path, [value])
            return
        if not isinstance(current, CommentedSeq):
            raise YamlDocumentError(f"{_fmt(path)} no es una secuencia")
        self._require_not_alias(current, path)
        current.append(_to_node(value))

    # -- salida --------------------------------------------------------------

    def text(self) -> str:
        """El documento dumpeado con el EOL original."""
        return self._dump().replace("\n", self._eol)

    def write(self, *, verify: Callable[[Path], T]) -> WriteResult[T]:
        """Escribe a un tmp del mismo directorio, verifica, hace backup y reemplaza atómico.

        Si ``verify`` levanta, el original queda intacto y NO hay backup.
        """
        path = self._path
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(self.text().encode("utf-8"))
            try:
                value = verify(tmp)
            except Exception as exc:  # noqa: BLE001 — cualquier fallo de verificación cierra
                raise YamlWriteError(str(exc)) from exc
            backup = _backup_name(path)
            shutil.copy2(path, backup)
            shutil.copymode(path, tmp)  # B3: un YAML 0600 no sale 0664
            tmp.replace(path)  # atómico: mismo directorio
        finally:
            tmp.unlink(missing_ok=True)
        return WriteResult(value=value, backup_path=backup, path=path)

    # -- internos ------------------------------------------------------------

    def _dump(self) -> str:
        buf = io.StringIO()
        self._yaml.dump(self._root, buf)
        return buf.getvalue()

    def _ensure_parent(self, path: YamlPath) -> CommentedMap | CommentedSeq:
        """El contenedor padre de ``path``, creando mappings block-style en el camino."""
        node: CommentedMap | CommentedSeq = self._root
        for depth, step in enumerate(path[:-1]):
            child: object
            if isinstance(step, int):
                if not isinstance(node, CommentedSeq):
                    raise YamlDocumentError(f"{_fmt(path[:depth])} no es una secuencia")
                if not 0 <= step < len(node):
                    raise YamlDocumentError(f"índice {step} fuera de rango en {_fmt(path[:depth])}")
                child = node[step]
            else:
                if not isinstance(node, CommentedMap):
                    raise YamlDocumentError(f"{_fmt(path[:depth])} no es un mapping")
                child = node.get(step, MISSING)
                if child is MISSING:
                    self._require_not_alias(node, path[:depth])
                    child = _block_map()
                    if node is self._root:
                        self._add_top_level(step, child)
                    else:
                        node[step] = child
            if not isinstance(child, CommentedMap | CommentedSeq):
                raise YamlDocumentError(f"{_fmt(path[: depth + 1])} es un escalar, no un bloque")
            node = child
        return node

    def _require_not_alias(self, container: object, path: YamlPath) -> None:
        """B1: un contenedor que aparece dos veces en el árbol es un ancla.

        Con ``rvi: &base {…}`` / ``otro: *base`` ruamel devuelve el MISMO
        objeto en los dos lugares: tocar ``connections.rvi.host`` cambiaría
        también ``connections.otro`` y el operador nunca lo pidió. No se
        edita: el archivo queda intacto y se le dice que lo haga a mano.
        """
        if _shared(self._root, container):
            raise YamlDocumentError(
                f"{_fmt(path) or '<raíz>'} es un alias YAML (&/*) — editalo a mano"
            )

    def _add_top_level(self, key: str, node: object) -> None:
        """Agrega una clave NUEVA de primer nivel con línea en blanco antes.

        Los bloques del proyecto van separados por línea en blanco. ruamel
        guarda ese ``\\n`` como comentario previo a la clave, así que no se
        duplica en re-dumps. Si el archivo ya terminaba en línea en blanco
        (ruamel la deja pegada a la clave anterior), no agregamos otra.
        """
        needs_blank = len(self._root) > 0 and not self._dump().endswith("\n\n")
        self._root[key] = node
        if needs_blank:
            self._root.yaml_set_comment_before_after_key(key, before="\n")


def apply_edits(doc: YamlDocument, edits: Iterable[Edit]) -> None:
    """Aplica ``edits`` en orden: ``DELETE`` borra, cualquier otro valor asigna."""
    for edit in edits:
        if edit.value is DELETE:
            doc.delete(edit.path)
        else:
            doc.set(edit.path, edit.value)


def to_plain(node: object) -> object:
    """``CommentedMap`` / ``CommentedSeq`` → ``dict`` / ``list``, y los escalares
    de ruamel a builtins (M4).

    ``DoubleQuotedScalarString`` es un ``str`` y ``ScalarFloat`` un ``float``,
    pero NO son ``str`` / ``float``: la igualdad por tipo de ``diff_edits``
    (139) los ve distintos de lo que tipea el operador y el formulario
    reporta cambios fantasma. Acá se desenvuelven una sola vez.
    """
    if isinstance(node, CommentedMap):
        return {key: to_plain(value) for key, value in node.items()}
    if isinstance(node, CommentedSeq):
        return [to_plain(value) for value in node]
    return _plain_scalar(node)


def _plain_scalar(node: object) -> object:
    """El escalar ruamel como builtin (``ScalarBoolean`` antes que ``ScalarInt``: es un ``int``)."""
    if isinstance(node, ScalarString):
        return str(node)
    if isinstance(node, ScalarBoolean):
        return bool(node)
    if isinstance(node, ScalarInt):
        return int(node)
    if isinstance(node, ScalarFloat):
        return float(node)
    if isinstance(node, TaggedScalar):
        return str(node.value)
    return node


def _shared(root: object, target: object) -> bool:
    """``target`` aparece más de una vez (por identidad) colgando de ``root``."""
    seen = 0
    visited: set[int] = set()
    stack: list[object] = [root]
    while stack:
        node = stack.pop()
        if node is target:
            seen += 1
            if seen > 1:
                return True
        if not isinstance(node, CommentedMap | CommentedSeq) or id(node) in visited:
            continue
        visited.add(id(node))
        stack.extend(node.values() if isinstance(node, CommentedMap) else node)
    return False


class _NullAsNullRepresenter(RoundTripRepresenter):
    """ruamel emite ``None`` como valor vacío (``a:``); el proyecto escribe ``null``.

    Subclase (y no ``add_representer`` sobre la clase base) para no mutar
    el representer global de ruamel desde acá.
    """

    def represent_none(self, data: None) -> ScalarNode:
        node: ScalarNode = self.represent_scalar("tag:yaml.org,2002:null", "null")
        return node


_NullAsNullRepresenter.add_representer(type(None), _NullAsNullRepresenter.represent_none)


def _block_map() -> CommentedMap:
    node = CommentedMap()
    node.fa.set_block_style()
    return node


def _to_node(value: object) -> object:
    """dict / list → contenedores ruamel en block style (recursivo); el resto tal cual."""
    if isinstance(value, CommentedMap | CommentedSeq):
        return value
    if isinstance(value, dict):
        node = _block_map()
        for key, item in value.items():
            node[key] = _to_node(item)
        return node
    if isinstance(value, list | tuple):
        seq = CommentedSeq(_to_node(item) for item in value)
        seq.fa.set_block_style()
        return seq
    return value


def _set_index(parent: CommentedMap | CommentedSeq, index: int, value: object) -> None:
    """``index == len`` agrega al final (139: ``diff_edits`` emite items nuevos así)."""
    if not isinstance(parent, CommentedSeq):
        raise YamlDocumentError("un índice numérico sólo se aplica a una secuencia")
    if index == len(parent):
        parent.append(value)
        return
    if not 0 <= index < len(parent):
        raise YamlDocumentError(f"índice {index} fuera de rango (usá append para agregar)")
    parent[index] = value


def _require_map(node: object, path: YamlPath) -> None:
    if not isinstance(node, CommentedMap):
        raise YamlDocumentError(f"{_fmt(path[:-1]) or '<raíz>'} no es un mapping")


def _fmt(path: YamlPath) -> str:
    return ".".join(str(step) for step in path)


def _backup_name(path: Path) -> Path:
    """``<name>.bak-YYYYmmdd-HHMMSS``; sufijo ``-N`` si dos escrituras caen en el mismo segundo."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    return backup


def _detect_indent(text: str) -> tuple[int, int, int]:
    """(mapping, sequence, offset) del archivo — defaults (2, 4, 2), el estilo del proyecto.

    Mapping: primera línea indentada cuyo padre es una clave (no un ``- ``).
    Secuencia: primer ``- `` bajo una clave: ``offset = dash - padre``,
    ``sequence = offset + 2`` (ruamel exige ``sequence >= offset + 2``).
    """
    mapping: int | None = None
    sequence: int | None = None
    offset: int | None = None
    stack: list[tuple[int, bool]] = []  # (indent, es_item_de_lista)
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        is_item = stripped.startswith("- ") or stripped == "-"
        # Un ``- `` al MISMO indent que su clave (offset 0) sigue siendo hijo de ella.
        while stack and (
            stack[-1][0] > indent or (stack[-1][0] == indent and (stack[-1][1] or not is_item))
        ):
            stack.pop()
        parent = stack[-1] if stack else None
        if parent is not None and not parent[1]:
            if is_item and offset is None:
                offset = indent - parent[0]
                sequence = offset + 2
            elif not is_item and mapping is None and ":" in stripped:
                mapping = indent - parent[0]
        if mapping is not None and offset is not None:
            break
        stack.append((indent, is_item))
    return (mapping or 2, sequence or 4, offset if offset is not None else 2)
