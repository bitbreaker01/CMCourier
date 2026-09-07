"""Overrides de sesión con modelo draft/applied (124, C4 del informe UX).

El operador edita un *draft* en la pantalla CONFIG; `a` lo valida y lo
promueve a *applied*. El launcher y el resumen efectivo leen SOLO
applied — un draft sin promover jamás llega a una corrida. Nada de esto
toca el YAML.
"""

from __future__ import annotations

__all__ = ["OverrideError", "SessionOverrides", "TriggerOverride", "apply_overrides"]

import json
from dataclasses import dataclass, fields, replace

from cmcourier.config.schema import (
    CsvTriggerConfig,
    LocalScanTriggerConfig,
    PipelineConfig,
    RvabrepTriggerConfig,
    SingleDocTriggerConfig,
)

TriggerOverride = (
    CsvTriggerConfig | RvabrepTriggerConfig | LocalScanTriggerConfig | SingleDocTriggerConfig
)


class OverrideError(ValueError):
    """Un valor de override fuera de rango — con mensaje accionable."""


@dataclass(frozen=True, slots=True)
class SessionOverrides:
    """Cada campo en ``None`` significa "usar el YAML".

    127: ``trigger`` es el pipeline elegido en [5] CORRER — un modelo
    pydantic ya validado (el CSV / la carpeta existen). Se elige y se
    aplica en el launcher, no en [3]: por eso ``cleared()`` no lo toca.
    """

    mode: str | None = None  # batched | streaming
    prep_workers: int | None = None
    bucket_size: int | None = None
    workers: int | None = None
    auto_tune_enabled: bool | None = None
    max_bandwidth_mbps: float | None = None
    unmask_pii: bool | None = None
    trigger: TriggerOverride | None = None

    def _scalar_items(self) -> list[tuple[str, object]]:
        # No usamos ``asdict``: deep-copiaría el modelo pydantic del trigger.
        return [
            (f.name, getattr(self, f.name))
            for f in fields(self)
            if f.name != "trigger" and getattr(self, f.name) is not None
        ]

    def is_empty(self) -> bool:
        return self.trigger is None and not self._scalar_items()

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in self._scalar_items()]
        if self.trigger is not None:
            parts.append(f"pipeline={self.trigger.kind}")
        return ", ".join(parts) if parts else "ninguno"

    def to_json(self) -> str:
        payload: dict[str, object] = dict(self._scalar_items())
        if self.trigger is not None:
            payload["trigger"] = self.trigger.model_dump(mode="json")
        return json.dumps(payload, ensure_ascii=False)

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

    if ov.trigger is not None:
        # 127: build_pipeline despacha S0 solo por config.trigger — con
        # esto alcanza para que toda la corrida use el pipeline elegido.
        updates["trigger"] = ov.trigger

    return config.model_copy(update=updates) if updates else config
