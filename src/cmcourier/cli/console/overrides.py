"""Overrides de sesión con modelo draft/applied (124, C4 del informe UX).

El operador edita un *draft* en la pantalla CONFIG; `a` lo valida y lo
promueve a *applied*. El launcher y el resumen efectivo leen SOLO
applied — un draft sin promover jamás llega a una corrida. Nada de esto
toca el YAML.
"""

from __future__ import annotations

__all__ = ["OverrideError", "SessionOverrides", "apply_overrides"]

import json
from dataclasses import asdict, dataclass, replace

from cmcourier.config.schema import PipelineConfig


class OverrideError(ValueError):
    """Un valor de override fuera de rango — con mensaje accionable."""


@dataclass(frozen=True, slots=True)
class SessionOverrides:
    """Cada campo en ``None`` significa "usar el YAML"."""

    mode: str | None = None  # batched | streaming
    prep_workers: int | None = None
    bucket_size: int | None = None
    workers: int | None = None
    auto_tune_enabled: bool | None = None
    max_bandwidth_mbps: float | None = None
    unmask_pii: bool | None = None

    def is_empty(self) -> bool:
        return all(v is None for v in asdict(self).values())

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in asdict(self).items() if v is not None]
        return ", ".join(parts) if parts else "ninguno"

    def to_json(self) -> str:
        return json.dumps(
            {k: v for k, v in asdict(self).items() if v is not None}, ensure_ascii=False
        )

    def validated(self) -> SessionOverrides:
        """Valida rangos (los mismos del schema) y devuelve self."""
        if self.mode is not None and self.mode not in ("batched", "streaming"):
            raise OverrideError("mode debe ser batched o streaming")
        if self.prep_workers is not None and not 1 <= self.prep_workers <= 32:
            raise OverrideError("prep_workers debe estar entre 1 y 32")
        if self.bucket_size is not None and not 10 <= self.bucket_size <= 1000:
            raise OverrideError("bucket_size debe estar entre 10 y 1000")
        if self.workers is not None and not 1 <= self.workers <= 64:
            raise OverrideError("cmis.workers debe estar entre 1 y 64")
        if self.max_bandwidth_mbps is not None and not 0 <= self.max_bandwidth_mbps <= 1000:
            raise OverrideError("max_bandwidth_mbps debe estar entre 0 y 1000")
        return self

    def cleared(self) -> SessionOverrides:
        return replace(
            self,
            mode=None,
            prep_workers=None,
            bucket_size=None,
            workers=None,
            auto_tune_enabled=None,
            max_bandwidth_mbps=None,
            unmask_pii=None,
        )


def apply_overrides(config: PipelineConfig, ov: SessionOverrides) -> PipelineConfig:
    """Config efectivo = YAML + applied, vía ``model_copy`` anidado.

    Los modelos son frozen: cada bloque tocado se re-crea con
    ``model_copy(update=…)`` — el YAML original queda intacto.
    """
    updates: dict[str, object] = {}

    proc_updates: dict[str, object] = {}
    if ov.mode is not None:
        proc_updates["mode"] = ov.mode
    if ov.prep_workers is not None:
        proc_updates["prep_workers"] = ov.prep_workers
    if ov.bucket_size is not None:
        proc_updates["streaming"] = config.processing.streaming.model_copy(
            update={"bucket_size": ov.bucket_size}
        )
    if proc_updates:
        updates["processing"] = config.processing.model_copy(update=proc_updates)

    cmis_updates: dict[str, object] = {}
    if ov.workers is not None:
        cmis_updates["workers"] = ov.workers
    if ov.auto_tune_enabled is not None:
        cmis_updates["auto_tune"] = config.cmis.auto_tune.model_copy(
            update={"enabled": ov.auto_tune_enabled}
        )
    if ov.max_bandwidth_mbps is not None:
        cmis_updates["max_bandwidth_mbps"] = ov.max_bandwidth_mbps
    if cmis_updates:
        updates["cmis"] = config.cmis.model_copy(update=cmis_updates)

    if ov.unmask_pii is not None:
        updates["observability"] = config.observability.model_copy(
            update={"unmask_pii": ov.unmask_pii}
        )

    return config.model_copy(update=updates) if updates else config
