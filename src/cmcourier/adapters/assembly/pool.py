"""Wrapper de `process pool` sobre :class:`PdfAssembler` (066).

La etapa S4 (ensamblado de PDF vía ``img2pdf`` + ``PIL`` + ``PyPDF2``) es
CPU-bound y está dominada por trabajo de extensiones en C que no siempre
libera el `GIL`. Ejecutarla dentro de un `thread` `producer` la serializa
sin importar cuántos `threads` haya — la pestaña BUCKET muestra N `threads`
"en vuelo" pero el `throughput` agregado es equivalente al de un solo
`worker`.

Este módulo expone un :class:`concurrent.futures.ProcessPoolExecutor` que
ejecuta ``PdfAssembler.assemble`` en N procesos `worker`, cada uno con su
propio intérprete Python. El `thread` `producer` envía la tarea y se
bloquea en ``.result()``; ese bloqueo libera el `GIL` para que otros
`producers` avancen con trabajo de S1/S2/S3. Los `workers` ejecutan
``assemble()`` con paralelismo real a nivel de SO.

Contrato de picklability:

* ``RVABREPDocument`` — dataclass frozen, picklable.
* ``StagedFile`` — dataclass frozen, picklable.
* ``AssemblerConfig`` — dataclass frozen, picklable.

Las funciones auxiliares :func:`_pool_init` y :func:`_pool_assemble` viven
a nivel de módulo (no anidadas) para que ``ProcessPoolExecutor`` pueda
picklearlas por nombre.
"""

from __future__ import annotations

__all__ = ["build_s4_process_pool", "_pool_assemble", "_pool_assemble_traced", "_pool_init"]

import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor

from cmcourier.adapters.assembly.pdf_assembler import (
    AssemblerConfig,
    AssemblyTimings,
    PdfAssembler,
)
from cmcourier.domain.models import RVABREPDocument, StagedFile

_log = logging.getLogger(__name__)

# Singleton por `worker`. El callable ``initializer`` del pool corre una
# vez por proceso `worker` y construye el assembler acá; las llamadas
# posteriores a ``_pool_assemble`` lo reutilizan.
_worker_assembler: PdfAssembler | None = None


def _pool_init(config: AssemblerConfig) -> None:
    """``initializer`` de ProcessPoolExecutor — corre una vez por `worker`.

    Construye un :class:`PdfAssembler` por proceso y lo deja en el global
    de módulo ``_worker_assembler``. El constructor del assembler crea el
    directorio temporal (idempotente), así que los `workers` pueden
    competir sobre él sin problema.
    """
    global _worker_assembler  # noqa: PLW0603 — singleton de módulo intencional
    _worker_assembler = PdfAssembler(config)


def _pool_assemble(document: RVABREPDocument) -> StagedFile:
    """Punto de entrada del `worker` — ensambla un documento y devuelve el
    :class:`StagedFile`. Propaga lo que :meth:`PdfAssembler.assemble`
    levante; el ``Future.result()`` del caller re-lanza la excepción en el
    proceso principal.
    """
    if _worker_assembler is None:  # pragma: no cover — el initializer lo garantiza
        raise RuntimeError("066: _pool_assemble called before _pool_init configured the worker")
    return _worker_assembler.assemble(document)


def _pool_assemble_traced(
    document: RVABREPDocument,
) -> tuple[StagedFile, AssemblyTimings]:
    """093: igual que :func:`_pool_assemble` pero devuelve además los
    timings sub-stage. El orquestador los loguea como ``S4.<sub>`` en
    el ``MetricsRecorder`` para que aparezcan en el ``batch_summary``."""
    if _worker_assembler is None:  # pragma: no cover — el initializer lo garantiza
        raise RuntimeError(
            "093: _pool_assemble_traced called before _pool_init configured the worker"
        )
    return _worker_assembler.assemble_traced(document)


def build_s4_process_pool(
    config: AssemblerConfig,
    max_workers: int | None = None,
) -> ProcessPoolExecutor:
    """Construye un `process pool` dimensionado a ``max_workers`` (por
    defecto ``os.cpu_count()``). Cada `worker` ejecuta :func:`_pool_init`
    una vez con el :class:`AssemblerConfig` provisto.

    El caller es dueño del ciclo de vida — debe llamar a
    ``pool.shutdown(wait=True)`` cuando la corrida del `pipeline` termine.
    """
    workers = max_workers if max_workers is not None else (os.cpu_count() or 1)
    workers = max(1, int(workers))
    # 066: forzamos ``spawn`` (no ``fork``) porque el proceso padre corre
    # muchos `threads` (producers, S5 pool, controlador AIMD, sampler).
    # ``fork`` en un padre multi-threaded puede dejar al hijo con `locks`
    # en estado inconsistente (Python 3.12 emite un DeprecationWarning
    # por esto); ``spawn`` arma un intérprete fresco y vuelve a correr
    # los helpers del pool a nivel de módulo vía ``initializer``.
    spawn_ctx = multiprocessing.get_context("spawn")
    pool = ProcessPoolExecutor(
        max_workers=workers,
        initializer=_pool_init,
        initargs=(config,),
        mp_context=spawn_ctx,
    )
    _log.info(
        "066: S4 process pool started",
        extra={"max_workers": workers, "source_root": str(config.source_root)},
    )
    return pool
