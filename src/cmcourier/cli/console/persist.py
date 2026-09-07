"""Escribir los overrides de sesión al YAML de configuración (135).

PyYAML no hace round-trip (pierde comentarios) y no queremos una
dependencia nueva sólo para esto. Así que parcheamos el TEXTO: siete
escalares conocidos, línea a línea, conservando comentarios inline y
todo lo demás byte-idéntico. Y como un parche textual puede
equivocarse (flow style, anchors, claves duplicadas), antes de tocar
el archivo cargamos el resultado con ``load_config`` y exigimos que sea
EXACTAMENTE ``apply_overrides(config, ov)``. Si no: no se escribe.
"""

from __future__ import annotations

__all__ = ["PersistError", "PersistResult", "persist_overrides"]

import os
import re
import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from cmcourier.cli.console.overrides import SessionOverrides, apply_overrides
from cmcourier.config.loader import load_config
from cmcourier.config.schema import PipelineConfig

# (campo del override, ruta de claves en el YAML)
_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mode", ("processing", "mode")),
    ("prep_workers", ("processing", "prep_workers")),
    ("bucket_size", ("processing", "streaming", "bucket_size")),
    ("workers", ("cmis", "workers")),
    ("auto_tune_enabled", ("cmis", "auto_tune", "enabled")),
    ("max_bandwidth_mbps", ("cmis", "max_bandwidth_mbps")),
    ("unmask_pii", ("observability", "unmask_pii")),
)

_KEY_RE = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z0-9_.-]+):(?P<rest>.*)$")


class PersistError(RuntimeError):
    """No se escribió el YAML — el mensaje dice por qué."""


@dataclass(frozen=True, slots=True)
class PersistResult:
    config: PipelineConfig
    backup_path: Path
    changed: dict[str, object]


def persist_overrides(
    config_path: Path, config: PipelineConfig, ov: SessionOverrides
) -> PersistResult:
    """Escribe los escalares de ``ov`` en ``config_path``; devuelve la config recargada."""
    changed = {
        ".".join(path): getattr(ov, field)
        for field, path in _PATHS
        if getattr(ov, field) is not None
    }
    if not changed:
        raise PersistError("no hay nada que escribir: los overrides aplicados están vacíos")

    original = config_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for field, path in _PATHS:
        value = getattr(ov, field)
        if value is not None:
            lines = _patch(lines, path, _render(value))
    patched = "".join(lines)

    # El trigger (127) es una elección del launcher, no del YAML.
    expected = apply_overrides(config, replace(ov, trigger=None))
    tmp = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(patched, encoding="utf-8")
        try:
            loaded = load_config(tmp)
        except Exception as exc:  # noqa: BLE001 — cualquier fallo de carga cierra
            raise PersistError(f"el YAML parcheado no valida: {exc}") from exc
        if loaded != expected:
            raise PersistError(
                "el YAML parcheado no coincide con los overrides (¿flow style, anchors, "
                "claves duplicadas?) — editá el archivo a mano"
            )
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = config_path.with_name(f"{config_path.name}.bak-{stamp}")
        shutil.copy2(config_path, backup)
        tmp.replace(config_path)  # atómico: mismo directorio
    finally:
        tmp.unlink(missing_ok=True)
    return PersistResult(config=loaded, backup_path=backup, changed=changed)


def _render(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)  # 10.0 → "10.0": PyYAML lo lee como float, igual que el schema
    return str(value)


def _patch(lines: list[str], path: tuple[str, ...], value: str) -> list[str]:
    """Reemplaza / inserta ``path`` en ``lines``. Best effort: la verificación decide."""
    start, end, indent = 0, len(lines), 0
    for depth, key in enumerate(path):
        hit = _find_key(lines, start, end, indent, key)
        last = depth == len(path) - 1
        if hit is None:
            # Falta desde acá: insertar el sub-árbol al final del bloque padre.
            insert_at = _block_end(lines, start, end)
            missing = path[depth:]
            new = [f"{' ' * (indent + 2 * i)}{k}:\n" for i, k in enumerate(missing[:-1])]
            new.append(f"{' ' * (indent + 2 * (len(missing) - 1))}{missing[-1]}: {value}\n")
            return lines[:insert_at] + new + lines[insert_at:]
        idx, rest = hit
        if last:
            comment = ""
            m = re.search(r"\s+#.*$", rest)
            if m:
                comment = m.group(0)
            lines[idx] = f"{' ' * indent}{key}: {value}{comment}\n"
            return lines
        # Bloque intermedio: sus hijos van hasta la próxima clave con indent <= actual.
        start = idx + 1
        end = _block_end(lines, start, end, parent_indent=indent)
        child = _child_indent(lines, start, end, indent)
        if child is None:
            # Bloque vacío o flow style: forzamos +2 y dejamos que la verificación juzgue.
            child = indent + 2
        indent = child
    return lines


def _find_key(
    lines: list[str], start: int, end: int, indent: int, key: str
) -> tuple[int, str] | None:
    for i in range(start, end):
        m = _KEY_RE.match(lines[i].rstrip("\n"))
        if m and len(m.group("indent")) == indent and m.group("key") == key:
            return i, m.group("rest")
    return None


def _block_end(lines: list[str], start: int, end: int, *, parent_indent: int = -1) -> int:
    """Índice de la primera línea (≥ start) que ya no pertenece al bloque."""
    last_content = start
    for i in range(start, end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        ind = len(lines[i]) - len(lines[i].lstrip(" "))
        if ind <= parent_indent:
            break
        last_content = i + 1
    return last_content


def _child_indent(lines: list[str], start: int, end: int, parent: int) -> int | None:
    for i in range(start, end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        ind = len(lines[i]) - len(lines[i].lstrip(" "))
        return ind if ind > parent else None
    return None
