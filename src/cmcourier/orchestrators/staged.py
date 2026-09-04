"""Orchestrator de las etapas S0..S6 para el ``csv-trigger-pipeline``.

Conecta los siete colaboradores (estrategia de triggers S0 + servicios /
adaptadores S1..S5 + tracking store S6) en un único `pipeline` ejecutable.
El orchestrator no contiene lógica de negocio — sólo coordinación, manejo
de errores y conteo (Principio III de la Constitución).

Dos comportamientos de alto nivel:

* **`Idempotency` cross-batch**: los docs cuyo ``txn_num`` ya está en
  ``S5_DONE`` en cualquier `batch` previo se saltean — no se re-suben,
  pero 062 revirtió el contrato previo de "salto silencioso" y el
  `batch` actual ahora escribe una fila en ``migration_log`` con
  ``status=S1_SKIPPED`` para que el tab DETAIL + analyzer +
  ``batch show`` puedan identificar qué docs específicos cayeron en
  este `bucket`.
* **Resume `stage`-por-`stage`**: ``run(batch_id=..., from_stage=N)``
  reutiliza un `batch` existente y ACOTA la corrida a su conjunto
  previo de ``txn_num``s. Dentro de cada `stage`, ``is_stage_done``
  por-doc cortocircuita la re-ejecución del trabajo ya exitoso — así,
  re-correr con ``from_stage=1`` contra un `batch` completado realiza
  cero uploads.

Disciplina de logging (Constitución VIII): cada record lleva ``batch_id``
en ``extra``; los records por-doc agregan ``txn_num``; los records
por-`stage` agregan ``stage``. Los valores resueltos de propiedades
(CIF, Nombre_Cliente, …) NUNCA aparecen en los records de log.
"""

from __future__ import annotations

__all__ = ["StagedPipeline", "RunReport"]

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from cmcourier.services.idempotency import IdempotencyCoordinator
    from cmcourier.services.reconciler import PeriodicReconciler

from cmcourier.adapters.assembly import PdfAssembler
from cmcourier.adapters.assembly.pdf_assembler import AssemblyTimings
from cmcourier.adapters.assembly.pool import _pool_assemble_traced
from cmcourier.adapters.upload.cmis_uploader import CmisUploader
from cmcourier.config.schema import AutoTuneConfig, HeavyLightLanesConfig
from cmcourier.domain.exceptions import (
    CMISClientError,
    CMISServerError,
    DefaultValidationFailedError,
    IDRViNotMappedError,
    IndexingError,
    PDFAssemblyFailedError,
    RetriesExhaustedError,
    RVABREPDeletedError,
    RVABREPNotFoundError,
    SourceFailedError,
    SourceFileMissingError,
)
from cmcourier.domain.models import (
    ClientTrigger,
    CMMapping,
    MigrationRecord,
    ResolvedMetadata,
    RVABREPDocument,
    StagedFile,
    StageStatus,
    Trigger,
)
from cmcourier.domain.ports import ITrackingStore, S0Strategy
from cmcourier.observability.error_classification import classify_failure
from cmcourier.observability.metrics import MetricsRecorder, StageTimer
from cmcourier.observability.system_metrics import SystemMetricsSampler
from cmcourier.services.auto_tune import AutoTuneController
from cmcourier.services.cancellation import CancellationToken
from cmcourier.services.document_cache import DocumentCacheService
from cmcourier.services.indexing import IndexingService
from cmcourier.services.lane_controller import LaneController
from cmcourier.services.lane_splitter import Lane
from cmcourier.services.lane_splitter import split as split_lanes
from cmcourier.services.mapping import MappingService
from cmcourier.services.metadata import MetadataService
from cmcourier.services.worker_pool_stats import ResizableSemaphore, WorkerPoolStats

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses públicas
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunReport:
    """Resumen del resultado devuelto por :meth:`StagedPipeline.run`."""

    batch_id: str
    total_triggers: int
    total_docs: int
    s1_done: int
    s1_skipped_cross_batch: int
    s1_filtered: int
    s2_done: int
    s2_failed: int
    s3_done: int
    s3_failed: int
    s4_done: int
    s4_failed: int
    s5_done: int
    s5_failed: int
    elapsed_seconds: float


# ---------------------------------------------------------------------------
# Estado interno de `stage`
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _StageItem:
    """Estado mutable por-doc que se hilvana a través de los `stage`s S1..S5."""

    trigger: Trigger
    document: RVABREPDocument
    mapping: CMMapping | None = None
    metadata: ResolvedMetadata | None = None
    staged_file: StagedFile | None = None
    cm_object_id: str | None = None


def _size_of_stage_item(item: _StageItem) -> int:
    """Accessor de tamaño para el lane splitter (036). 0 cuando falta staged_file."""
    return item.staged_file.size_bytes if item.staged_file is not None else 0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class StagedPipeline:
    """Orchestrator del ``csv-trigger-pipeline`` (S0..S6)."""

    def __init__(
        self,
        *,
        trigger_strategy: S0Strategy,
        indexing_service: IndexingService,
        mapping_service: MappingService,
        metadata_service: MetadataService,
        assembler: PdfAssembler,
        uploader: CmisUploader,
        tracking_store: ITrackingStore,
        metrics_recorder: MetricsRecorder | None = None,
        pipeline_name: str = "csv-trigger",
        workers: int = 1,
        prep_workers: int = 1,
        pool_stats: WorkerPoolStats | None = None,
        auto_tune: AutoTuneConfig | None = None,
        sampler: SystemMetricsSampler | None = None,
        coordinator: IdempotencyCoordinator | None = None,
        periodic_reconciler: PeriodicReconciler | None = None,
        heavy_light_lanes: HeavyLightLanesConfig | None = None,
        document_cache: DocumentCacheService | None = None,
        s4_process_pool: ProcessPoolExecutor | None = None,
        keep_staged_files: bool = False,
        s4_smart_routing: bool = False,
    ) -> None:
        self._trigger_strategy = trigger_strategy
        self._indexing_service = indexing_service
        self._mapping_service = mapping_service
        self._metadata_service = metadata_service
        self._assembler = assembler
        self._uploader = uploader
        self._tracking_store = tracking_store
        self._metrics = metrics_recorder or MetricsRecorder(
            log_dir=Path("./logs"),
            slow_op_threshold_ms=5000.0,
            slow_op_top_n=20,
            enabled=False,
            pipeline_metrics_enabled=False,
        )
        self._pipeline_name = pipeline_name
        self._workers = max(1, int(workers))
        # 056: `thread pool` de tamaño fijo para los `stage`s de prep
        # S2/S3/S4. 1 == serial (byte-idéntico al pre-056). S0/S1 se
        # mantienen seriales.
        self._prep_workers = max(1, int(prep_workers))
        self._pool_stats = pool_stats or WorkerPoolStats()
        # 025 fase 2: `soft-cap` del límite de concurrencia. El
        # auto-tune lo ajusta.
        self._auto_tune_cfg = auto_tune
        self._concurrency_limit = ResizableSemaphore(self._workers)
        # 025 fase 3: construye el controller `eagerly` para que el TUI
        # pueda referenciarlo antes de que arranque run(). El controller
        # se queda idle (sin `thread`) hasta que se llama ``start()``
        # dentro de _stage_s5.
        self._auto_tune_controller: AutoTuneController | None = self._build_auto_tune_controller()
        # 026: `sampler` de métricas del sistema tier-5. La factory
        # devuelve None cuando está deshabilitado en config; hacemos
        # late-bind de las pool stats para que un `sampler` construido
        # por la capa de wiring pueda reportar active_workers.
        self._sampler = sampler
        if self._sampler is not None:
            self._sampler.attach_pool_stats(self._pool_stats)
        # 034 fase 3: coordinador de `idempotency` distribuida. Cuando
        # es None, is_uploaded / mark_uploaded / mark_failed van
        # directo al tracking_store (comportamiento pre-034). Cuando
        # está seteado, el coordinador agrega encima el path de AS400
        # NIARVILOG.
        self._coordinator = coordinator
        # 096: reconciliador periódico AS400. None salvo cuando
        # ``tracking.as400_sync.mode == "periodic"`` — la capa de wiring
        # lo construye y este orchestrator solo lo arranca/para en run().
        self._periodic_reconciler = periodic_reconciler
        # 097: token de cancelación cooperativa. El TUI lo prende cuando
        # el operador confirma "q"; los métodos per-doc lo chequean al
        # entrar para frenar ordenadamente (drain). Headless = nunca se
        # prende.
        self._cancel_token = CancellationToken()
        # 037: cache de metadata cross-batch. None cuando está
        # deshabilitado (default) — S3 siempre invoca
        # MetadataService.resolve (comportamiento pre-037).
        self._document_cache = document_cache
        # 066: `process pool` opcional para S4 (ensamblado de PDF).
        # Cuando está seteado, ``_s4_one`` hace `submit` al pool en
        # lugar de llamar al assembler directamente — bypasea el `GIL`
        # para el trabajo CPU-bound. ``None`` corre S4 inline
        # (comportamiento pre-066, byte-idéntico).
        self._s4_process_pool = s4_process_pool
        # 085: post-S5_DONE borra el ``StagedFile`` de ``temp_dir`` por
        # default. Flag opt-out para debug (operador necesita
        # inspeccionar el archivo ensamblado después del upload).
        self._keep_staged_files = keep_staged_files
        # 094: cuando True Y el process pool está activo, los PDF
        # nativos van inline (thread del prep_workers) en lugar del
        # process pool. Evita ~30s/doc de overhead en Windows para
        # docs cuyo trabajo útil (shutil.copy2) NO es CPU bound.
        self._s4_smart_routing = s4_smart_routing
        # 036: coordinador de `lane`s heavy/light. None cuando el modo
        # dual está apagado (el default) — S5 mantiene el path legacy
        # de pool único.
        self._lanes_config = heavy_light_lanes
        self._lane_controller: LaneController | None = None
        if heavy_light_lanes is not None and heavy_light_lanes.enabled:
            self._lane_controller = LaneController(
                total_budget=self._workers,
                heavy_initial_ratio=heavy_light_lanes.heavy_initial_ratio,
                rebalance_interval_s=heavy_light_lanes.rebalance_interval_s,
                idle_threshold_s=heavy_light_lanes.idle_threshold_s,
            )
        # 119: pools de threads de vida larga — lazy, reutilizados por
        # todos los chunks/stages de la corrida. Pre-119 cada stage de
        # prep y cada chunk de S5 creaba y destruía su executor
        # (~80 000 ciclos de spawn/join en una corrida de 20M docs, y la
        # causa raíz de la fuga de conexiones ODBC que 106 mitigó desde
        # el adapter). ``shutdown_worker_pools`` los cierra al final.
        self._pools_lock = threading.Lock()
        self._prep_pool: ThreadPoolExecutor | None = None
        self._s5_pool: ThreadPoolExecutor | None = None
        self._s5_heavy_pool: ThreadPoolExecutor | None = None
        self._s5_light_pool: ThreadPoolExecutor | None = None

    # ------------------------------------------------- Accessors del TUI

    @property
    def metrics_recorder(self) -> MetricsRecorder:
        return self._metrics

    @property
    def pool_stats(self) -> WorkerPoolStats:
        return self._pool_stats

    @property
    def concurrency_limit(self) -> ResizableSemaphore:
        return self._concurrency_limit

    @property
    def uploader(self) -> CmisUploader:
        return self._uploader

    @property
    def pipeline_name(self) -> str:
        return self._pipeline_name

    @property
    def auto_tune_controller(self) -> AutoTuneController | None:
        return self._auto_tune_controller

    @property
    def sampler(self) -> SystemMetricsSampler | None:
        return self._sampler

    @property
    def lane_controller(self) -> LaneController | None:
        """036: handle de sólo lectura para el TUI / tests.

        ``None`` cuando el modo dual está apagado.
        """
        return self._lane_controller

    @property
    def tracking_store(self) -> ITrackingStore:
        """052: handle de sólo lectura para el drill-down por `chunk` del TUI."""
        return self._tracking_store

    @property
    def cancel_token(self) -> CancellationToken:
        """097: token de cancelación cooperativa compartido con el TUI."""
        return self._cancel_token

    # --------------------------------------------------- wiring del auto-tune

    def _build_auto_tune_controller(self) -> AutoTuneController | None:
        """Devuelve un controller sii ``cmis.auto_tune.enabled``; si no, None.

        En modo dual-lane (036), AIMD pilotea el budget TOTAL de
        `worker`s; el lane controller es dueño del split por `lane`.
        ``on_pool_resize`` despacha a cualquiera de los dos
        controllers que esté activo.
        """
        if self._auto_tune_cfg is None or not self._auto_tune_cfg.enabled:
            return None
        return AutoTuneController(
            config=self._auto_tune_cfg,
            p95_provider=lambda: self._metrics.current_stage_p95_with_count("S5"),
            current_workers_provider=self._current_total_workers,
            current_timeout_provider=lambda: self._uploader._timeout_s,
            on_pool_resize=self._on_pool_resize,
            on_timeout_change=self._set_upload_timeout,
        )

    def _current_total_workers(self) -> int:
        """Devuelve el budget TOTAL actual de `worker`s para ambos modos (036)."""
        if self._lane_controller is not None:
            return self._lane_controller.snapshot().total_budget
        return self._concurrency_limit.capacity

    def _on_pool_resize(self, new_total: int) -> None:
        """Hook AIMD para resize del pool. Despacha por modo (036)."""
        if self._lane_controller is not None:
            self._lane_controller.set_total_budget(new_total)
        else:
            self._concurrency_limit.set_capacity(new_total)

    def _pool_ceiling(self) -> int:
        """057: la cantidad máxima de `thread`s que S5 alguna vez podría necesitar.

        El ``ThreadPoolExecutor`` de S5 debe dimensionarse a este valor —
        NO al ``cmis.workers`` inicial. AIMD redimensiona el
        ``ResizableSemaphore`` / ``LaneController`` hasta
        ``auto_tune.max_threads``; si el pool sólo tiene
        ``cmis.workers`` `thread`s, esos slots extra del `semaphore`
        no tienen ningún `thread` para correrlos y ``pool_in_use`` se
        queda clavado en el conteo inicial. Con AIMD deshabilitado
        nada redimensiona el `semaphore`, así que ``cmis.workers`` ya
        es el techo correcto.
        """
        if self._auto_tune_cfg is not None and self._auto_tune_cfg.enabled:
            return max(self._workers, self._auto_tune_cfg.max_threads)
        return self._workers

    def _set_upload_timeout(self, new_timeout_s: float) -> None:
        """AIMD empuja un nuevo timeout; el uploader lo toma en la próxima llamada."""
        self._uploader._timeout_s = float(new_timeout_s)

    # ----------------------------------------------------------- API pública

    def run(
        self,
        *,
        source_descriptor: str,
        batch_size: int = 1000,
        batch_id: str | None = None,
        from_stage: int = 1,
        total: int | None = None,
    ) -> RunReport:
        """Corre el `pipeline` csv-trigger end-to-end.

        ``total`` (033) acota la cantidad de triggers procesados después
        del acquire de S0 — útil para validar una config contra un
        subconjunto chico antes de lanzar la migración completa.
        """
        start = time.monotonic()
        self._validate_parameters(batch_size, from_stage, batch_id)
        resolved_batch_id = self._resolve_batch_id(batch_id, from_stage, batch_size)
        self._metrics.start_batch(pipeline=self._pipeline_name, batch_id=resolved_batch_id)

        if self._sampler is not None:
            self._sampler.start()
        # 096: el reconciliador de fondo corre durante toda la corrida;
        # su pasada FINAL (en stop()) garantiza que nada quede sin
        # sincronizar a AS400.
        if self._periodic_reconciler is not None:
            self._periodic_reconciler.start()
        try:
            s0_start = time.monotonic()
            triggers = list(self._trigger_strategy.acquire(source_descriptor))
            if total is not None:
                triggers = triggers[: max(0, total)]
            self._metrics.record_stage(
                stage="S0", duration_ms=(time.monotonic() - s0_start) * 1000.0
            )
            resume_scope = (
                self._tracking_store.list_txn_nums_for_batch(resolved_batch_id)
                if from_stage > 1
                else None
            )

            items, skipped, s1_filtered = self._stage_s0_s1(
                triggers, resolved_batch_id, resume_scope
            )
            s1_done = len(items)
            items, s2_failed = self._stage_s2(items, resolved_batch_id)
            s2_done = len(items)
            items, s3_failed = self._stage_s3(items, resolved_batch_id)
            s3_done = len(items)
            items, s4_failed = self._stage_s4(items, resolved_batch_id)
            s4_done = len(items)
            controller = self._auto_tune_controller
            try:
                if controller is not None:
                    controller.start()
                # 038: pre-abre el `connection pool` TCP+`TLS`+`JSESSIONID`
                # de S5 para que los primeros uploads no paguen cada uno
                # el handshake en su critical path. 122: se calienta al
                # techo AIMD (paridad con streaming) — relevante con
                # ``http2: false``, donde cada worker abre su socket.
                self._uploader.warm_connection_pool(self._pool_ceiling())
                s5_done, s5_failed = self._stage_s5(items, resolved_batch_id)
            finally:
                if controller is not None:
                    controller.stop(timeout=2.0)

            self._tracking_store.flush()
            self._tracking_store.complete_batch(resolved_batch_id)
        finally:
            # 119: cierra los pools persistentes de la corrida.
            self.shutdown_worker_pools()
            if self._sampler is not None:
                self._sampler.stop()
            # 096: para el daemon y corre la pasada de reconciliación
            # final. Después del flush de SQLite para que el último
            # estado terminal esté visible para la pasada final.
            if self._periodic_reconciler is not None:
                self._periodic_reconciler.stop()

        elapsed = time.monotonic() - start
        total_docs = s1_done + skipped
        self._metrics.close_batch(
            pipeline=self._pipeline_name,
            batch_id=resolved_batch_id,
            total_docs=total_docs,
            elapsed_s=elapsed,
        )

        return RunReport(
            batch_id=resolved_batch_id,
            total_triggers=len(triggers),
            total_docs=total_docs,
            s1_done=s1_done,
            s1_skipped_cross_batch=skipped,
            s1_filtered=s1_filtered,
            s2_done=s2_done,
            s2_failed=s2_failed,
            s3_done=s3_done,
            s3_failed=s3_failed,
            s4_done=s4_done,
            s4_failed=s4_failed,
            s5_done=s5_done,
            s5_failed=s5_failed,
            elapsed_seconds=elapsed,
        )

    # ----------------------------------------------------------- helpers (auxiliares)

    @staticmethod
    def _validate_parameters(batch_size: int, from_stage: int, batch_id: str | None) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        if not 1 <= from_stage <= 5:
            raise ValueError(f"from_stage must be in [1, 5], got {from_stage}")
        if from_stage > 1 and batch_id is None:
            raise ValueError("from_stage > 1 requires batch_id")

    def _resolve_batch_id(self, batch_id: str | None, from_stage: int, batch_size: int) -> str:
        if batch_id is not None:
            return batch_id
        return self._tracking_store.start_batch(total_records=batch_size)

    # ----------------------------------------------------- puntos de entrada multi-batch
    # 028: prep_chunk / upload_chunk permiten que el MultiBatchOrchestrator
    # maneje cada `chunk` con su propio MetricsRecorder mientras comparten
    # el resto del estado del `pipeline` (`worker pool` de S5, tracking,
    # servicios, controller AIMD).

    def prep_chunk(
        self,
        *,
        triggers: list[Trigger],
        batch_id: str,
        recorder: MetricsRecorder,
        from_stage: int = 1,
    ) -> tuple[list[_StageItem], int, int, int, int, int, int]:
        """Corre S0..S4 sobre un `chunk` de triggers ya adquirido.

        Devuelve ``(items, skipped, s1_done, s1_filtered, s2_failed,
        s3_failed, s4_failed)``. La adquisición de triggers es
        responsabilidad del orchestrator — este método toma la lista
        directamente.
        """
        resume_scope = (
            self._tracking_store.list_txn_nums_for_batch(batch_id) if from_stage > 1 else None
        )
        items, skipped, s1_filtered = self._stage_s0_s1(
            triggers, batch_id, resume_scope, recorder=recorder
        )
        s1_done = len(items)
        items, s2_failed = self._stage_s2(items, batch_id, recorder=recorder)
        items, s3_failed = self._stage_s3(items, batch_id, recorder=recorder)
        items, s4_failed = self._stage_s4(items, batch_id, recorder=recorder)
        return items, skipped, s1_done, s1_filtered, s2_failed, s3_failed, s4_failed

    def upload_chunk(
        self,
        *,
        items: list[_StageItem],
        batch_id: str,
        recorder: MetricsRecorder,
    ) -> tuple[int, int]:
        """Corre S5 sobre un `chunk` preparado. Devuelve ``(s5_done, s5_failed)``."""
        return self._stage_s5(items, batch_id, recorder=recorder)

    # ----------------------------------------------------- streaming (063)

    def streaming_prep_one(
        self,
        trigger: Trigger,
        batch_id: str,
        recorder: MetricsRecorder,
    ) -> tuple[_StageItem | None, int, int]:
        """063: corre S1→S4 sobre un único trigger y devuelve el sobreviviente.

        Usado por los `producer`s de :class:`StreamingOrchestrator`.
        Devuelve ``(survivor, skipped_cross_batch, s1_filtered)``:

        * ``survivor`` es el único ``_StageItem`` sobreviviente o
          ``None`` (filtrado / saltado cross-batch / fallado en S2-S4).
        * ``skipped_cross_batch`` es 1 cuando el doc RVABREP del
          trigger ya había sido subido en un `batch` previo (062
          ``S1_SKIPPED``).
        * ``s1_filtered`` es 1 cuando la fila RVABREP venía con código
          de baja (062 ``S1_FILTERED``).

        La persistencia de falla / filtrado / salto la hacen los
        helpers internos por-`stage` — este método no agrega ningún
        comportamiento propio más allá del secuenciamiento.
        """
        items, skipped, filtered = self._stage_s0_s1(
            [trigger], batch_id, resume_scope=None, recorder=recorder
        )
        if not items:
            return None, skipped, filtered
        survivor, _ = self._s2_one(items[0], batch_id, recorder)
        if survivor is None:
            return None, skipped, filtered
        survivor, _ = self._s3_one(survivor, batch_id, recorder)
        if survivor is None:
            return None, skipped, filtered
        survivor, _ = self._s4_one(survivor, batch_id, recorder)
        return survivor, skipped, filtered

    def streaming_upload_one(
        self,
        item: _StageItem,
        batch_id: str,
        recorder: MetricsRecorder,
        lane: Lane | None = None,
    ) -> Literal["done", "failed", "skipped"]:
        """063: corre S5 sobre un único item preparado.

        ``lane`` (065) selecciona entre el `semaphore` de pool único +
        worker-pool-stats (``None``) y el `semaphore` por-`lane` dentro
        del :class:`LaneController` (``"heavy"`` / ``"light"``). El
        ``_upload_one`` existente maneja ambos paths de manera
        uniforme — esto es un wrapper público delgado.
        """
        return self._upload_one(item, batch_id, recorder, lane)

    def warm_upload_pool(self, workers: int) -> None:
        """063: pre-abre el `connection pool` de S5 a ``workers`` `socket`s."""
        self._uploader.warm_connection_pool(workers)

    def _build_record(
        self,
        item: _StageItem,
        batch_id: str,
        stage: StageStatus,
    ) -> MigrationRecord:
        # 046: los triggers son polimórficos; audit_row() devuelve la
        # proyección best-effort para las columnas trigger_* de
        # migration_log.
        audit = item.trigger.audit_row()
        return MigrationRecord(
            trigger_shortname=audit.get("shortname") or "",
            trigger_cif=audit.get("cif") or "",
            trigger_system_id=audit.get("system_id") or "",
            rvabrep_txn_num=item.document.txn_num,
            rvabrep_file_name=item.document.file_name,
            batch_id=batch_id,
            status=stage,
            created_at=datetime.now(),  # noqa: DTZ005 — wall-clock para auditoría humana-legible
            cm_folder=item.mapping.cm_folder if item.mapping else None,
            cm_object_type=item.mapping.cm_object_type if item.mapping else None,
            source_file_path=str(item.staged_file.path) if item.staged_file else None,
            page_count=item.staged_file.page_count if item.staged_file else None,
            file_size_bytes=item.staged_file.size_bytes if item.staged_file else None,
        )

    # ----------------------------------------------------------- `stage`s

    def _stage_s0_s1(
        self,
        triggers: list[Trigger],
        batch_id: str,
        resume_scope: set[str] | None,
        *,
        recorder: MetricsRecorder | None = None,
    ) -> tuple[list[_StageItem], int, int]:
        rec = recorder or self._metrics
        items: list[_StageItem] = []
        skipped_cross_batch = 0
        # 051: un trigger cuya fila RVABREP viene con código de baja es
        # *filtrado* — un resultado de primera clase, NO una falla y
        # NO un descarte silencioso.
        filtered = 0
        for trigger in triggers:
            # 097: cancelación cooperativa — dejamos de tomar triggers
            # nuevos; los ya convertidos a items siguen su curso.
            if self._cancel_token.is_cancelled():
                break
            audit = trigger.audit_row()
            audit_shortname = audit.get("shortname") or "<unknown>"
            docs: list[RVABREPDocument] = []
            with StageTimer(
                rec,
                pipeline=self._pipeline_name,
                stage="S1",
                batch_id=batch_id,
                txn_num=audit_shortname,
            ) as timer:
                try:
                    docs = self._indexing_service.enrich(trigger)
                except RVABREPNotFoundError:
                    timer.mark_failed()
                    _log.warning(
                        "pipeline: trigger has no rvabrep rows",
                        extra={"batch_id": batch_id, "shortname": audit_shortname},
                    )
                    continue
                except RVABREPDeletedError as exc:
                    # 051: borrado-en-origen NO es una falla del `pipeline`
                    # — el doc se excluye correctamente. Lo contamos, lo
                    # logueamos y seguimos.
                    # 062: persiste una fila `S1_FILTERED` en
                    # migration_log para que el tab DETAIL + analyzer +
                    # `batch show` puedan ver QUÉ triggers fueron
                    # filtrados y por qué. La excepción se dispara antes
                    # de que se derive ningún txn_num, así que usamos
                    # una clave sintética indexada por la identidad del
                    # trigger — las re-corridas colisionan
                    # idempotentemente vía INSERT OR IGNORE.
                    filtered += 1
                    audit_system_id = audit.get("system_id") or ""
                    synthetic_txn = f"FILTERED__{audit_shortname}__{audit_system_id}"
                    filtered_record = MigrationRecord(
                        trigger_shortname=audit_shortname,
                        trigger_cif=audit.get("cif") or "",
                        trigger_system_id=audit_system_id,
                        rvabrep_txn_num=synthetic_txn,
                        rvabrep_file_name="",
                        batch_id=batch_id,
                        status=StageStatus.S1_PENDING,
                        created_at=datetime.now(),  # noqa: DTZ005
                    )
                    self._tracking_store.mark_stage_pending(filtered_record, StageStatus.S1_PENDING)
                    self._tracking_store.mark_stage_terminal(
                        synthetic_txn,
                        batch_id,
                        StageStatus.S1_FILTERED,
                        f"deleted_at_source; deleted_count={exc.deleted_count}",
                    )
                    _log.info(
                        "pipeline: doc filtered at S1",
                        extra={
                            "batch_id": batch_id,
                            "shortname": audit_shortname,
                            "reason": "deleted_at_source",
                        },
                    )
                    continue
                except IndexingError:
                    timer.mark_failed()
                    _log.exception(
                        "pipeline: indexing failed",
                        extra={"batch_id": batch_id, "shortname": audit_shortname},
                    )
                    continue
            for doc in docs:
                if resume_scope is not None and doc.txn_num not in resume_scope:
                    _log.info(
                        "pipeline: doc out of resume scope",
                        extra={
                            "batch_id": batch_id,
                            "txn_num": doc.txn_num,
                            "reason": "resume_out_of_scope",
                        },
                    )
                    continue
                already_in_batch = self._tracking_store.is_stage_done(
                    doc.txn_num, batch_id, StageStatus.S1_DONE
                )
                if not already_in_batch and self._tracking_store.is_uploaded(doc.txn_num):
                    # 062: persiste una fila ``S1_SKIPPED`` para que el
                    # tab DETAIL + analyzer + `batch show` puedan ver
                    # qué docs fueron salteados cross-batch (el
                    # contrato previo de "salteado silenciosamente"
                    # se revierte intencionalmente por trazabilidad).
                    skipped_cross_batch += 1
                    skip_item = _StageItem(trigger=trigger, document=doc)
                    skip_record = self._build_record(skip_item, batch_id, StageStatus.S1_PENDING)
                    self._tracking_store.mark_stage_pending(skip_record, StageStatus.S1_PENDING)
                    self._tracking_store.mark_stage_terminal(
                        doc.txn_num,
                        batch_id,
                        StageStatus.S1_SKIPPED,
                        "cross_batch_uploaded",
                    )
                    _log.info(
                        "pipeline: doc already uploaded in prior batch",
                        extra={
                            "batch_id": batch_id,
                            "txn_num": doc.txn_num,
                            "reason": "cross_batch_uploaded",
                        },
                    )
                    continue
                item = _StageItem(trigger=trigger, document=doc)
                if not already_in_batch:
                    record = self._build_record(item, batch_id, StageStatus.S1_PENDING)
                    self._tracking_store.mark_stage_pending(record, StageStatus.S1_PENDING)
                    self._tracking_store.mark_stage_done(doc.txn_num, batch_id, StageStatus.S1_DONE)
                items.append(item)
        return items, skipped_cross_batch, filtered

    def _run_prep_stage(
        self,
        items: list[_StageItem],
        worker: Callable[[_StageItem], tuple[_StageItem | None, bool]],
    ) -> tuple[list[_StageItem], int]:
        """056: despacha el `worker` por-item de un `stage` de prep.

        ``prep_workers == 1`` corre en serial — byte-idéntico al loop
        pre-056. Arriba de 1, un ``ThreadPoolExecutor`` fijo corre el
        `worker`; ``pool.map`` preserva el orden de entrada, así que
        ``survivors`` queda determinístico sin importar el orden de
        completación. Cada `worker` devuelve
        ``(survivor_or_None, counted_failure)``.
        """
        if self._prep_workers == 1:
            results = [worker(item) for item in items]
        else:
            # 119: pool persistente — pre-119 se creaba y destruía un
            # executor por stage por chunk.
            results = list(self._get_prep_pool().map(worker, items))
        survivors = [item for item, _ in results if item is not None]
        failed = sum(1 for _, counted in results if counted)
        return survivors, failed

    # ------------------------------------------------- pools persistentes (119)

    def _get_prep_pool(self) -> ThreadPoolExecutor:
        with self._pools_lock:
            if self._prep_pool is None:
                self._prep_pool = ThreadPoolExecutor(
                    max_workers=self._prep_workers,
                    thread_name_prefix="cmcourier-prep",
                )
            return self._prep_pool

    def _get_s5_pool(self) -> ThreadPoolExecutor:
        with self._pools_lock:
            if self._s5_pool is None:
                self._s5_pool = ThreadPoolExecutor(
                    max_workers=self._pool_ceiling(),
                    thread_name_prefix="cmcourier-s5",
                )
            return self._s5_pool

    def _get_s5_lane_pools(self) -> tuple[ThreadPoolExecutor, ThreadPoolExecutor]:
        with self._pools_lock:
            if self._s5_heavy_pool is None:
                ceiling = self._pool_ceiling()
                self._s5_heavy_pool = ThreadPoolExecutor(
                    max_workers=ceiling,
                    thread_name_prefix="cmcourier-s5-heavy",
                )
                self._s5_light_pool = ThreadPoolExecutor(
                    max_workers=ceiling,
                    thread_name_prefix="cmcourier-s5-light",
                )
            assert self._s5_light_pool is not None
            return self._s5_heavy_pool, self._s5_light_pool

    def shutdown_worker_pools(self) -> None:
        """119: cierra (wait) y limpia los pools persistentes.

        Idempotente; los orchestrators lo llaman en su ``finally``. Un
        uso posterior recrea los pools — el pipeline sigue siendo
        reutilizable."""
        with self._pools_lock:
            pools = [
                p
                for p in (
                    self._prep_pool,
                    self._s5_pool,
                    self._s5_heavy_pool,
                    self._s5_light_pool,
                )
                if p is not None
            ]
            self._prep_pool = None
            self._s5_pool = None
            self._s5_heavy_pool = None
            self._s5_light_pool = None
        for pool in pools:
            pool.shutdown(wait=True)

    def _stage_s2(
        self,
        items: list[_StageItem],
        batch_id: str,
        *,
        recorder: MetricsRecorder | None = None,
    ) -> tuple[list[_StageItem], int]:
        rec = recorder or self._metrics
        return self._run_prep_stage(items, lambda item: self._s2_one(item, batch_id, rec))

    def _s2_one(
        self, item: _StageItem, batch_id: str, rec: MetricsRecorder
    ) -> tuple[_StageItem | None, bool]:
        """S2 mapping para un item. Devuelve ``(survivor_or_None,
        counted_failure)`` — una falla ya marcada como done en una
        corrida previa se descarta sin contar."""
        # 097: cancelación cooperativa — el item se saltea sin contar
        # como falla; queda pendiente para un resume.
        if self._cancel_token.is_cancelled():
            return None, False
        txn = item.document.txn_num
        with StageTimer(
            rec,
            pipeline=self._pipeline_name,
            stage="S2",
            batch_id=batch_id,
            txn_num=txn,
        ) as timer:
            try:
                mapping = self._mapping_service.get_mapping(item.document.index7)
            except IDRViNotMappedError as exc:
                timer.mark_failed()
                if not self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S2_DONE):
                    record = self._build_record(item, batch_id, StageStatus.S2_PENDING)
                    self._tracking_store.mark_stage_pending(record, StageStatus.S2_PENDING)
                    self._tracking_store.mark_stage_failed(
                        txn, batch_id, StageStatus.S2_FAILED, str(exc)
                    )
                    return None, True
                return None, False
        if not self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S2_DONE):
            record = self._build_record(item, batch_id, StageStatus.S2_PENDING)
            self._tracking_store.mark_stage_pending(record, StageStatus.S2_PENDING)
            self._tracking_store.mark_stage_done(txn, batch_id, StageStatus.S2_DONE)
        item.mapping = mapping
        return item, False

    def _stage_s3(
        self,
        items: list[_StageItem],
        batch_id: str,
        *,
        recorder: MetricsRecorder | None = None,
    ) -> tuple[list[_StageItem], int]:
        rec = recorder or self._metrics
        return self._run_prep_stage(items, lambda item: self._s3_one(item, batch_id, rec))

    def _s3_one(
        self, item: _StageItem, batch_id: str, rec: MetricsRecorder
    ) -> tuple[_StageItem | None, bool]:
        """Resolución de metadata S3 para un item. Devuelve
        ``(survivor_or_None, counted_failure)``."""
        # 097: cancelación cooperativa — saltea sin contar como falla.
        if self._cancel_token.is_cancelled():
            return None, False
        assert item.mapping is not None
        txn = item.document.txn_num
        fields = item.mapping.required_metadata_fields
        with StageTimer(
            rec,
            pipeline=self._pipeline_name,
            stage="S3",
            batch_id=batch_id,
            txn_num=txn,
        ) as timer:
            cached = (
                self._document_cache.try_get(txn_num=txn, fields=fields)
                if self._document_cache is not None
                else None
            )
            if cached is not None:
                # 037: cache hit — cortocircuita el MetadataService.
                # 046: los triggers son polimórficos. Para un
                # ClientTrigger reconstruimos con el CIF cacheado para
                # que el código downstream que lee ``.cif``
                # directamente quede consistente; para los triggers
                # basados en filas conservamos el original (la fila es
                # inmutable, y el cif cacheado vive de todos modos en
                # la propiedad BAC_CIF del `bag` de metadata).
                metadata = ResolvedMetadata.from_dict(dict(cached.properties))
                healed_trigger: Trigger
                if isinstance(item.trigger, ClientTrigger):
                    healed_trigger = ClientTrigger(
                        shortname=item.trigger.shortname,
                        cif=cached.trigger_cif,
                        system_id=item.trigger.system_id,
                    )
                else:
                    healed_trigger = item.trigger
                healed_cif: str | None = cached.trigger_cif
            else:
                try:
                    resolution = self._metadata_service.resolve(
                        item.trigger, item.document, item.mapping
                    )
                except (SourceFailedError, DefaultValidationFailedError) as exc:
                    timer.mark_failed()
                    if not self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S3_DONE):
                        record = self._build_record(item, batch_id, StageStatus.S3_PENDING)
                        self._tracking_store.mark_stage_pending(record, StageStatus.S3_PENDING)
                        self._tracking_store.mark_stage_failed(
                            txn, batch_id, StageStatus.S3_FAILED, str(exc)
                        )
                        return None, True
                    return None, False
                metadata = resolution.metadata
                healed_trigger = resolution.healed_trigger
                healed_cif = resolution.healed_cif
                if self._document_cache is not None:
                    self._document_cache.put(
                        txn_num=txn,
                        fields=fields,
                        metadata=metadata,
                        trigger_cif=healed_cif,
                    )
        if not self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S3_DONE):
            record = self._build_record(item, batch_id, StageStatus.S3_PENDING)
            self._tracking_store.mark_stage_pending(record, StageStatus.S3_PENDING)
            self._tracking_store.mark_stage_done(txn, batch_id, StageStatus.S3_DONE)
        item.metadata = metadata
        item.trigger = healed_trigger
        return item, False

    def _stage_s4(
        self,
        items: list[_StageItem],
        batch_id: str,
        *,
        recorder: MetricsRecorder | None = None,
    ) -> tuple[list[_StageItem], int]:
        rec = recorder or self._metrics
        return self._run_prep_stage(items, lambda item: self._s4_one(item, batch_id, rec))

    def _s4_one(
        self, item: _StageItem, batch_id: str, rec: MetricsRecorder
    ) -> tuple[_StageItem | None, bool]:
        """Ensamblado de PDF de S4 para un item. Devuelve
        ``(survivor_or_None, counted_failure)``.

        066: cuando ``_s4_process_pool`` está seteado, despacha vía
        ``pool.submit(_pool_assemble, ...).result()`` para que el
        trabajo CPU-bound corra en un proceso separado — bypaseando
        el `GIL`. El `thread` `producer` bloquea esperando la future
        pero libera el `GIL`, dejando que otros `producer`s corran
        trabajo de S1-S3.
        """
        # 097: cancelación cooperativa — saltea sin contar como falla.
        if self._cancel_token.is_cancelled():
            return None, False
        txn = item.document.txn_num
        with StageTimer(
            rec,
            pipeline=self._pipeline_name,
            stage="S4",
            batch_id=batch_id,
            txn_num=txn,
        ) as timer:
            try:
                # 094: smart routing — los PDF nativos corren inline aun
                # cuando el process pool está activo. ``shutil.copy2``
                # libera el GIL durante I/O; el thread del prep_workers
                # da paralelismo real sin el overhead pickle/IPC/spawn
                # del process pool (~30s/doc en Windows). Los paginados
                # TIFF/JPEG siguen yendo al process pool porque
                # ``img2pdf`` es CPU bound.
                route_inline = self._s4_process_pool is None or (
                    self._s4_smart_routing and item.document.is_pdf
                )
                if route_inline:
                    staged, timings = self._assembler.assemble_traced(item.document)
                else:
                    assert self._s4_process_pool is not None
                    staged, timings = self._s4_process_pool.submit(
                        _pool_assemble_traced, item.document
                    ).result()
                # 093: sub-stage metrics — aparecen en ``batch_summary``
                # como buckets propios ("S4.copy_native", "S4.encode_pdf",
                # etc.) que ``cmcourier diagnose`` muestra junto con S4.
                self._record_s4_substages(rec, timings)
            except (SourceFileMissingError, PDFAssemblyFailedError) as exc:
                timer.mark_failed()
                if not self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S4_DONE):
                    record = self._build_record(item, batch_id, StageStatus.S4_PENDING)
                    self._tracking_store.mark_stage_pending(record, StageStatus.S4_PENDING)
                    self._tracking_store.mark_stage_failed(
                        txn, batch_id, StageStatus.S4_FAILED, str(exc)
                    )
                    return None, True
                return None, False
        if not self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S4_DONE):
            record = self._build_record(item, batch_id, StageStatus.S4_PENDING)
            self._tracking_store.mark_stage_pending(record, StageStatus.S4_PENDING)
            self._tracking_store.mark_stage_done(txn, batch_id, StageStatus.S4_DONE)
        item.staged_file = staged
        # 058: la fila se hizo INSERT-OR-IGNORE en S1 con metadata
        # NULL (item.staged_file era None en ese momento). Ahora que
        # el assembler produjo los valores reales, los persistimos
        # para que el tab DETAIL y ``cmcourier batch show`` vean
        # efectivamente el tamaño + cantidad de páginas + path del
        # archivo. Fuera del guard de is_stage_done para que las
        # corridas de resume también rellenen cualquier fila pre-058.
        self._tracking_store.record_staged_file_metadata(
            txn,
            batch_id,
            source_file_path=str(staged.path),
            page_count=staged.page_count,
            file_size_bytes=staged.size_bytes,
        )
        return item, False

    def _stage_s5(
        self,
        items: list[_StageItem],
        batch_id: str,
        *,
        recorder: MetricsRecorder | None = None,
    ) -> tuple[int, int]:
        """Uploads de S5, paralelizados sobre ``self._workers`` `thread`s (025).

        Las llamadas al tracking-store siguen serializadas por la
        `queue` del writer; `CmisUploader` es thread-safe según 025
        R011/R012; las métricas por-`stage` usan un `lock` por
        debajo. Los resultados se totalizan en el `thread` principal
        a partir de los resultados de ``as_completed``.

        028: ``recorder`` permite que el orchestrator multi-batch
        rutee los timings de S5 al recorder por-`chunk`.
        """
        rec = recorder or self._metrics
        # 036: cuando el modo dual-lane está configurado Y el splitter
        # dice que vale la pena, despacha cada item con su tag de
        # `lane`. Si no, el path legacy de pool único corre
        # byte-idéntico al pre-036.
        assignment = self._partition_for_lanes(items)
        if assignment is None:
            return self._stage_5_single(items, batch_id, rec)
        return self._stage_5_dual(assignment, batch_id, rec)

    def _stage_5_single(
        self,
        items: list[_StageItem],
        batch_id: str,
        rec: MetricsRecorder,
    ) -> tuple[int, int]:
        """S5 legacy de pool único (pre-036). Byte-idéntico a 025.

        057: el pool se dimensiona a ``_pool_ceiling()`` (el máximo
        AIMD), no al ``cmis.workers`` inicial — si no, el
        ``ResizableSemaphore`` redimensionado por AIMD no tiene
        `thread`s para honrar sus slots extra.
        """
        ceiling = self._pool_ceiling()
        self._pool_stats.set_pool_size(ceiling)
        self._pool_stats.set_queue_depth(len(items))
        s5_done = 0
        failed = 0
        # 119: pool persistente — el `as_completed` sobre el dict
        # completo de futures espera a todos los items del chunk, así
        # que el `with` (shutdown por chunk) no aportaba nada más que
        # churn de threads.
        pool = self._get_s5_pool()
        futures = {pool.submit(self._upload_one, item, batch_id, rec): item for item in items}
        for fut in as_completed(futures):
            outcome = fut.result()
            if outcome == "done":
                s5_done += 1
                rec.record_upload_done()
            elif outcome == "failed":
                # 104: el conteo de fallas (total + tipo + status) ya lo
                # hizo ``_upload_one`` con la excepción en mano.
                failed += 1
            elif outcome == "skipped":
                rec.record_upload_skipped()
            self._pool_stats.decrement_queue_depth()
        return s5_done, failed

    def _partition_for_lanes(
        self, items: list[_StageItem]
    ) -> tuple[tuple[_StageItem, ...], tuple[_StageItem, ...]] | None:
        """Devuelve items ``(heavy, light)`` cuando aplica el modo dual; si no, ``None``."""
        if self._lane_controller is None or self._lanes_config is None:
            return None
        if not self._lanes_config.enabled:
            return None
        assignment = split_lanes(
            items,
            threshold_bytes=self._lanes_config.heavy_threshold_bytes,
            min_batch=self._lanes_config.heavy_lane_min_batch,
            size_of=_size_of_stage_item,
        )
        if assignment.is_single_lane:
            return None
        return assignment.heavy, assignment.light

    def _stage_5_dual(
        self,
        assignment: tuple[tuple[_StageItem, ...], tuple[_StageItem, ...]],
        batch_id: str,
        rec: MetricsRecorder,
    ) -> tuple[int, int]:
        """036: `dispatch` dual heavy/light vía dos `executor`s cooperantes.

        Cada `lane` obtiene su propio ``ThreadPoolExecutor``
        dimensionado al techo del budget TOTAL de `worker`s (057:
        ``_pool_ceiling()``, el máximo AIMD — no el ``cmis.workers``
        inicial); el `semaphore` por-`lane` dentro del
        ``LaneController`` acota la concurrencia real. Dos
        `executor`s evitan la inanición que ocurriría si los `worker`s
        de un único `executor` agarrasen primero los heavies y luego
        se bloqueasen en el `semaphore` heavy — dejando los items
        light encolados sin un `thread` que los corra.
        """
        assert self._lane_controller is not None
        heavy_items, light_items = assignment
        depths: dict[Lane, int] = {"heavy": len(heavy_items), "light": len(light_items)}
        self._lane_controller.set_queue_depth("heavy", depths["heavy"])
        self._lane_controller.set_queue_depth("light", depths["light"])
        self._lane_controller.start()
        s5_done = 0
        failed = 0
        try:
            # 119: pools persistentes por lane — pre-119 se creaban y
            # destruían dos executors de `ceiling` threads por chunk.
            heavy_pool, light_pool = self._get_s5_lane_pools()
            futures: dict[Future[Literal["done", "failed", "skipped"]], Lane] = {}
            for item in heavy_items:
                futures[heavy_pool.submit(self._upload_one, item, batch_id, rec, "heavy")] = "heavy"
            for item in light_items:
                futures[light_pool.submit(self._upload_one, item, batch_id, rec, "light")] = "light"
            for fut in as_completed(futures):
                lane = futures[fut]
                outcome = fut.result()
                if outcome == "done":
                    s5_done += 1
                    rec.record_upload_done()
                elif outcome == "failed":
                    # 104: ``_upload_one`` ya contabilizó la falla por
                    # tipo + status con la excepción en mano.
                    failed += 1
                elif outcome == "skipped":
                    rec.record_upload_skipped()
                depths[lane] = max(0, depths[lane] - 1)
                self._lane_controller.set_queue_depth(lane, depths[lane])
        finally:
            self._lane_controller.stop()
        return s5_done, failed

    def _s5_preflight(
        self,
        item: _StageItem,
        batch_id: str,
        txn: str,
    ) -> MigrationRecord | Literal["done", "skipped"]:
        """109: pre-flight de idempotencia de S5, SIN slot del semáforo.

        Devuelve el :class:`MigrationRecord` listo para el upload, o el
        outcome terminal (``"done"`` si el doc ya está en ``S5_DONE``,
        ``"skipped"`` si otro proceso ganó el claim distribuido).
        """
        assert item.mapping is not None
        if self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S5_DONE):
            return "done"
        record = self._build_record(item, batch_id, StageStatus.S5_PENDING)
        self._tracking_store.mark_stage_pending(record, StageStatus.S5_PENDING)
        # 034 fase 3: claim distribuido. Cuando el coordinador es
        # None (path legacy), try_claim siempre es True. Cuando
        # está activo, el coordinador va a AS400 NIARVILOG; si
        # otro proceso ya es dueño de la fila (un competidor en
        # Java u otra instancia de CMCourier), salteamos.
        if self._coordinator is not None and not self._coordinator.try_claim(
            record=record,
            document=item.document,
            mapping=item.mapping,
            trigger=item.trigger,
        ):
            _log.info(
                "pipeline: doc claimed by another process",
                extra={
                    "batch_id": batch_id,
                    "txn_num": txn,
                    "reason": "as400_claim_lost",
                },
            )
            return "skipped"
        return record

    def _upload_one(
        self,
        item: _StageItem,
        batch_id: str,
        recorder: MetricsRecorder | None = None,
        lane: Lane | None = None,
    ) -> Literal["done", "failed", "skipped"]:
        """Trabajo S5 por-doc ejecutado dentro de un `worker` `thread` (025 + 036).

        Cuando ``lane`` es None, corre el path legacy del `semaphore`
        de pool único + stats (pre-036). Cuando está seteado, en su
        lugar se usan el `semaphore` por-`lane` y los counters del
        :class:`LaneController`.
        """
        assert item.mapping is not None
        assert item.metadata is not None
        assert item.staged_file is not None
        # 097: cancelación cooperativa — un doc que aún no arrancó el
        # upload se saltea limpio (queda pendiente para un resume). Los
        # docs ya en vuelo pasaron este chequeo y terminan — eso es el
        # drain. Chequeado antes de tomar el slot del semaphore.
        if self._cancel_token.is_cancelled():
            return "skipped"
        txn = item.document.txn_num
        # 109: el pre-flight de idempotencia (query SQLite + claim AS400)
        # corre ANTES de tomar el slot — el semáforo se reserva para el
        # upload real. Un doc ya subido o un claim perdido retornan sin
        # consumir presupuesto de concurrencia.
        preflight = self._s5_preflight(item, batch_id, txn)
        if not isinstance(preflight, MigrationRecord):
            self._mark_completed(lane)
            return preflight
        record = preflight
        worker_name = threading.current_thread().name

        # 025 fase 2: respeta el cap del `semaphore` del auto-tune
        # antes de consumir efectivamente un slot de `worker`.
        if lane is None:
            self._concurrency_limit.acquire()
            self._pool_stats.mark_busy(worker_name)
        else:
            assert self._lane_controller is not None
            self._lane_controller.acquire(lane)
        try:
            with StageTimer(
                recorder or self._metrics,
                pipeline=self._pipeline_name,
                stage="S5",
                batch_id=batch_id,
                txn_num=txn,
            ) as timer:
                try:
                    # 039: ``cmis_type`` (MapeoRVI_CM.CMISType, 035)
                    # sobrescribe el ``cm_object_type`` derivado cuando
                    # está seteado.
                    # 038: ``cmis_folder`` (MapeoRVI_CM.CMISFolder)
                    # sobrescribe el ``cm_folder`` derivado cuando está
                    # seteado. Ambos permiten que `repo`sitorios no
                    # IBM-CM (Alfresco staging, o tipos de banco
                    # futuros que no sigan el patrón
                    # ``$t!-N_BAC_…v-1``) funcionen sin cambio de
                    # código.
                    object_type_id = item.mapping.cmis_type or item.mapping.cm_object_type
                    folder_path = item.mapping.cmis_folder or item.mapping.cm_folder
                    cm_object_id = self._uploader.upload(
                        file=item.staged_file,
                        folder_path=folder_path,
                        object_type_id=object_type_id,
                        document_name=f"{txn}.pdf",
                        mime_type="application/pdf",
                        properties=dict(item.metadata.properties),
                        batch_id=batch_id,
                    )
                except (CMISClientError, CMISServerError, RetriesExhaustedError) as exc:
                    timer.mark_failed()
                    # 104: clasifica la falla y la contabiliza por tipo +
                    # status HTTP. Se hace acá — con la excepción en mano —
                    # porque el outcome string que ve el loop consumidor ya
                    # perdió el tipo.
                    category, status_code = classify_failure(exc)
                    (recorder or self._metrics).record_upload_failed(category, status_code)
                    if self._coordinator is not None:
                        self._coordinator.mark_failed(
                            record=record,
                            document=item.document,
                            mapping=item.mapping,
                            trigger=item.trigger,
                            stage=StageStatus.S5_FAILED,
                            error=str(exc),
                        )
                    else:
                        self._tracking_store.mark_stage_failed(
                            txn, batch_id, StageStatus.S5_FAILED, str(exc)
                        )
                    self._mark_failed(lane)
                    return "failed"
            if self._coordinator is not None:
                self._coordinator.mark_uploaded(
                    record=record,
                    document=item.document,
                    mapping=item.mapping,
                    trigger=item.trigger,
                    cm_object_id=cm_object_id,
                )
            else:
                self._tracking_store.mark_stage_done(
                    txn, batch_id, StageStatus.S5_DONE, cm_object_id=cm_object_id
                )
            item.cm_object_id = cm_object_id
            self._cleanup_staged_file(item.staged_file)
            self._mark_completed(lane)
            return "done"
        finally:
            if lane is None:
                self._pool_stats.mark_idle(worker_name)
                self._concurrency_limit.release()
            else:
                assert self._lane_controller is not None
                self._lane_controller.release(lane)

    @staticmethod
    def _record_s4_substages(
        rec: MetricsRecorder,
        timings: AssemblyTimings,
    ) -> None:
        """093: emite cada sub-stage de S4 al ``MetricsRecorder``.

        Cada substage > 0 se registra como ``S4.<name>``. Los valores
        en 0 se skipean (camino no tomado: ``copy_native_ms == 0``
        en el path paginado, ``encode_pdf_ms == 0`` en native_pdf).
        Aparecen en ``batch_summary`` y ``cmcourier diagnose`` los
        muestra junto con S4.
        """
        if timings.source_stat_ms > 0:
            rec.record_stage(stage="S4.source_stat", duration_ms=timings.source_stat_ms)
        if timings.copy_native_ms > 0:
            rec.record_stage(stage="S4.copy_native", duration_ms=timings.copy_native_ms)
        if timings.discover_pages_ms > 0:
            rec.record_stage(stage="S4.discover_pages", duration_ms=timings.discover_pages_ms)
        if timings.encode_pdf_ms > 0:
            rec.record_stage(stage="S4.encode_pdf", duration_ms=timings.encode_pdf_ms)
        if timings.dst_stat_ms > 0:
            rec.record_stage(stage="S4.dst_stat", duration_ms=timings.dst_stat_ms)

    def _cleanup_staged_file(self, staged: StagedFile) -> None:
        """085: borra el archivo ensamblado de ``temp_dir`` post-S5_DONE.

        No-op si ``keep_staged_files=True``. Idempotente: ``missing_ok``
        cubre el caso de que algún otro proceso ya lo haya borrado o que
        el path nunca haya existido. Cualquier fallo se loguea pero no
        propaga — el upload ya completó y mark_uploaded ya persistió;
        un cleanup roto no debe revertir un S5_DONE legítimo.
        """
        if self._keep_staged_files:
            return
        try:
            staged.path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning(
                "failed to cleanup staged file path=%s err=%s",
                staged.path,
                exc,
            )

    def _mark_completed(self, lane: Lane | None) -> None:
        if lane is None:
            self._pool_stats.mark_completed()
        else:
            assert self._lane_controller is not None
            self._lane_controller.mark_completed(lane)

    def _mark_failed(self, lane: Lane | None) -> None:
        if lane is None:
            self._pool_stats.mark_failed()
        else:
            assert self._lane_controller is not None
            self._lane_controller.mark_failed(lane)
