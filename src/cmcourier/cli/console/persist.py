"""Escribir los overrides de sesión al YAML de configuración (135, 137).

Siete escalares conocidos van al YAML vía :class:`YamlDocument` (ruamel
round-trip: comentarios, comillas y orden intactos; lo que no se toca
queda byte-idéntico). Y como un editor puede equivocarse igual, antes de
tocar el archivo cargamos el resultado con ``load_config`` y exigimos
que sea EXACTAMENTE ``apply_overrides(config, ov)``. Si no: no se escribe.
"""

from __future__ import annotations

__all__ = ["PersistError", "PersistResult", "persist_overrides"]

from dataclasses import dataclass, replace
from pathlib import Path

from cmcourier.cli.console.overrides import SessionOverrides, apply_overrides
from cmcourier.config.loader import load_config
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.yaml_doc import YamlDocument, YamlDocumentError, YamlWriteError

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

    # El trigger (127) es una elección del launcher, no del YAML.
    expected = apply_overrides(config, replace(ov, trigger=None))

    def verify(tmp: Path) -> PipelineConfig:
        try:
            loaded = load_config(tmp)
        except Exception as exc:  # noqa: BLE001 — cualquier fallo de carga cierra
            raise PersistError(f"el YAML editado no valida: {exc}") from exc
        if loaded != expected:
            raise PersistError(
                "el YAML editado no coincide con los overrides (¿anchors, merge keys?) "
                "— editá el archivo a mano"
            )
        return loaded

    try:
        doc = YamlDocument.load(config_path)
        for field, path in _PATHS:
            value = getattr(ov, field)
            if value is not None:
                doc.set(path, value)
        result = doc.write(verify=verify)
    # M2: leer, escribir el tmp o reemplazar puede fallar por permisos / disco
    # lleno; la [3] sólo atrapa PersistError y el OSError crudo salía como traceback.
    except (YamlDocumentError, YamlWriteError, OSError) as exc:
        raise PersistError(str(exc)) from exc
    return PersistResult(config=result.value, backup_path=result.backup_path, changed=changed)
