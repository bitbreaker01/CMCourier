"""Capa de configuración: schema Pydantic + loader YAML + secretos por env-var.

Único lector de variables de entorno del proceso: ``CMIS_USERNAME`` /
``CMIS_PASSWORD`` más, por cada alias del registro ``connections:`` (129),
``<ALIAS>_USERNAME`` / ``<ALIAS>_PASSWORD`` (alias en mayúsculas; las
conexiones inline usan el alias implícito ``as400``). Ver
:func:`cmcourier.config.schema.credential_env_vars`. Principio V de la
Constitución.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cmcourier.config.loader import Secrets, load_config, load_secrets
from cmcourier.config.schema import PipelineConfig

if TYPE_CHECKING:
    from cmcourier.config.wiring import build_pipeline


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """Importa :func:`build_pipeline` de forma `lazy` para romper una dependencia circular.

    ``config.wiring`` arrastra ``orchestrators.staged`` que a su vez
    importa ``observability.setup`` que importa ``config.schema``.
    Un import eager top-level haría ciclo. Re-exportar vía ``__getattr__``
    mantiene la API pública (``from cmcourier.config import build_pipeline``)
    funcionando sin el ciclo.
    """
    if name == "build_pipeline":
        from cmcourier.config.wiring import build_pipeline as _bp  # noqa: PLC0415

        return _bp
    raise AttributeError(f"module 'cmcourier.config' has no attribute {name!r}")


__all__ = [
    "PipelineConfig",
    "Secrets",
    "build_pipeline",
    "load_config",
    "load_secrets",
]
