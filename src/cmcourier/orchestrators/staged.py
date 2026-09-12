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
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
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
    IdentityResolutionError,
    IDRViNotMappedError,
    IndexingError,
    PDFAssemblyFailedError,
    RetriesExhaustedError,
    RVABREPNotFoundError,
    SourceFailedError,
    SourceFileMissingError,
)
from cmcourier.domain.models import (
    ClientTrigger,
    CMMapping,
    ExcludedTrigger,
    MigrationRecord,
    ReasonCode,
    ResolvedMetadata,
    RVABREPDocument,
    StagedFile,
    StageStatus,
    Trigger,
    trigger_system_id,
)
from cmcourier.domain.ports import ITrackingStore, S0Strategy
from cmcourier.observability.error_classification import ErrorCategory, classify_failure
from cmcourier.observability.metrics import MetricsRecorder, StageTimer
from cmcourier.observability.system_metrics import SystemMetricsSampler
from cmcourier.services.auto_tune import AutoTuneController
from cmcourier.services.cancellation import CancellationToken
from cmcourier.services.document_cache import DocumentCacheService
from cmcourier.services.eligibility import EligibilityService, EligibilitySnapshot
from cmcourier.services.identity import IdentityResolver, ResolvedIdentity
from cmcourier.services.indexing import EnrichOutcome, IndexingService
from cmcourier.services.lane_controller import LaneController
from cmcourier.services.lane_splitter import Lane
from cmcourier.services.lane_splitter import split as split_lanes
from cmcourier.services.mapping import MappingService
from cmcourier.services.metadata import MetadataService
from cmcourier.services.reconciler import stop_reconciler_visibly
from cmcourier.services.worker_pool_stats import ResizableSemaphore, WorkerPoolStats

_log = logging.getLogger(__name__)

# 148 REQ-002: los cuatro ``CM_*`` salen de ``classify_failure`` (104), que YA
# produce exactamente este enum y hasta ahora sólo alimentaba métricas. Se
# conecta a la base, no se reinventa. ``app_error`` no tiene código propio en
# la taxonomía: es, por definición, la excepción no contemplada.
_CM_REASONS: Mapping[ErrorCategory, ReasonCode] = MappingProxyType(
    {
        "timeout": ReasonCode.CM_TIMEOUT,
        "http_4xx": ReasonCode.CM_REJECTED_4XX,
        "http_5xx": ReasonCode.CM_ERROR_5XX,
        "transport": ReasonCode.CM_TRANSPORT,
        "app_error": ReasonCode.CRASHED,
    }
)


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


def _census_record(excluded: ExcludedTrigger, batch_id: str) -> MigrationRecord:
    """148 REQ-004: la fila de ``migration_log`` de un documento que no sigue.

    Un solo lugar la arma para todos los caminos del censo. ``id_rvi`` va
    siempre: sin él el censo no se puede agrupar por código RVI, que es
    exactamente la pregunta del operador.
    """
    return MigrationRecord(
        trigger_shortname=excluded.shortname or "",
        trigger_cif=excluded.cif or "",
        trigger_system_id=excluded.system_id or "",
        rvabrep_txn_num=excluded.txn_num,
        rvabrep_file_name=excluded.file_name,
        batch_id=batch_id,
        status=StageStatus.S1_PENDING,
        created_at=datetime.now(),  # noqa: DTZ005 — wall-clock para auditoría humana
        reason_code=excluded.reason_code,
        id_rvi=excluded.id_rvi,
    )


@dataclass(slots=True)
class _StageItem:
    """Estado mutable por-doc que se hilvana a través de los `stage`s S1..S5."""

    trigger: Trigger
    document: RVABREPDocument
    mapping: CMMapping | None = None
    metadata: ResolvedMetadata | None = None
    staged_file: StagedFile | None = None
    cm_object_id: str | None = None
    # 147 REQ-003: la identidad del cliente, resuelta al INICIO de S2 y
    # arrastrada hasta las tres escrituras (REQ-004). ``None`` hasta que S2
    # corra, y para siempre cuando no hay resolver cableado (pre-147).
    identity: ResolvedIdentity | None = None
    # 147 REQ-003: los campos que costó resolver la identidad, indexados por
    # nombre canónico. S3 los usa como semilla para no repetir la cadena.
    identity_fields: Mapping[str, str] = field(default_factory=dict)


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
        identity_resolver: IdentityResolver | None = None,
        eligibility_service: EligibilityService | None = None,
    ) -> None:
        self._trigger_strategy = trigger_strategy
        # 150 REQ-001/003: sin servicio (la perilla apagada) S2 se comporta
        # byte-idéntico al pre-150 y la lista de activos NO SE ABRE ni una
        # vez. El servicio comparte la instancia de ``metadata_service``, así
        # que las búsquedas contra la lista se memoizan por corrida igual que
        # los saltos de la cadena de identidad.
        self._eligibility = eligibility_service
        self._eligibility_snapshot: EligibilitySnapshot | None = None
        # 147 REQ-003: sin resolver, S2 se comporta byte-idéntico al pre-147.
        # El resolver comparte la instancia de ``metadata_service`` (el memo
        # de la cadena vive adentro), así que lo que S2 consulta S3 no lo
        # vuelve a pagar ni siquiera cuando la semilla no alcanza.
        self._identity_resolver = identity_resolver
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
        # 132: re-autenticación CMIS en caliente. Con handler registrado,
        # un 401 en S5 pausa el token, avisa UNA vez por episodio y
        # reintenta el doc cuando alguien reanuda. Sin handler (headless,
        # TUI clásica) el 401 falla como siempre. ``_cred_generation``
        # distingue un 401 de un request que ya viajaba con la sesión
        # vieja cuando las credenciales se refrescaron: ése reintenta sin
        # abrir episodio nuevo.
        self._auth_expired_handler: Callable[[], None] | None = None
        self._reauth_lock = threading.Lock()
        self._cred_generation = 0
        # Episodio abierto (número) o None. NO se gatea en ``is_paused()``:
        # una pausa manual previa al 401 dejaría al handler sin disparar.
        self._reauth_episode: int | None = None
        self._reauth_episodes_opened = 0
        # 133: techo manual de `worker`s. El AIMD escribe ``_aimd_total`` y
        # el operador ``_user_cap``; el pool ve ``min`` de ambos, acotado a
        # ``[1, _pool_ceiling()]``. Un solo helper (``_apply_worker_budget``)
        # aplica el resultado para que nunca se pisen.
        self._aimd_total = self._workers
        self._user_cap: int | None = None
        self._budget_lock = threading.Lock()
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

    # ------------------------------------------- 132: re-auth CMIS en caliente

    def set_auth_expired_handler(self, handler: Callable[[], None] | None) -> None:
        """Registra el aviso de "sesión CMIS rechazada". Se invoca desde un
        worker thread, UNA vez por episodio de pausa; el receptor debe
        despachar a su propio thread (p. ej. ``app.call_from_thread``)."""
        self._auth_expired_handler = handler

    def set_cmis_credentials(self, username: str, password: str) -> None:
        """Empuja credenciales nuevas al uploader (próximo POST re-hace el
        warmup) y sube la generación para que los 401 en vuelo reintenten
        sin abrir otro episodio."""
        with self._reauth_lock:
            self._uploader.set_credentials(username, password)
            self._cred_generation += 1

    def _await_reauth(self, generation: int) -> bool:
        """Tras un 401: si las credenciales ya cambiaron desde que salió el
        request, reintenta directo; si no, pausa, avisa (una vez por
        episodio) y espera en la compuerta. Devuelve False si la corrida
        se canceló mientras esperaba."""
        with self._reauth_lock:
            if self._cancel_token.is_cancelled():
                return False
            if self._cred_generation != generation:
                return True
            if self._reauth_episode is None:
                self._reauth_episodes_opened += 1
                self._reauth_episode = self._reauth_episodes_opened
                fire = True
            else:
                fire = False
            episode = self._reauth_episode
            self._cancel_token.pause()  # idempotente: puede estar pausada a mano
        if fire and self._auth_expired_handler is not None:
            try:
                self._auth_expired_handler()
            except Exception:  # noqa: BLE001 — el TUI no puede matar al worker
                _log.exception(
                    "el handler de re-autenticación falló; la corrida queda "
                    "pausada — reanudá con credenciales nuevas desde la consola"
                )
        proceed = self._cancel_token.checkpoint()
        with self._reauth_lock:
            # Cerrar SOLO nuestro episodio: si otro worker ya abrió el
            # siguiente (401 con las credenciales nuevas), no lo pisamos.
            if self._reauth_episode == episode:
                self._reauth_episode = None
        return proceed

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
        """Devuelve el budget TOTAL actual de `worker`s para ambos modos (036).

        133: es el presupuesto EFECTIVO (ya recortado por el techo
        manual). Con un techo bajo el AIMD "cree" que el pool es chico y
        propone ``cap+1``; cuando el techo se levanta, el pool sube a eso
        y el AIMD sigue creciendo desde ahí. Aceptable y acotado.
        """
        if self._lane_controller is not None:
            return self._lane_controller.snapshot().total_budget
        return self._concurrency_limit.capacity

    def _on_pool_resize(self, new_total: int) -> None:
        """Hook AIMD para resize del pool. Despacha por modo (036).

        133: no escribe al pool directo — actualiza ``_aimd_total`` y
        re-aplica el ``min`` con el techo manual.
        """
        with self._budget_lock:
            self._aimd_total = max(1, int(new_total))
            self._apply_worker_budget()

    def _effective_worker_budget(self) -> int:
        """``min(aimd_total, user_cap)`` acotado a ``[1, _pool_ceiling()]`` (133)."""
        budget = self._aimd_total
        if self._user_cap is not None:
            budget = min(budget, self._user_cap)
        # Dual-lane: cada lane retiene al menos un slot → piso 2.
        floor = 2 if self._lane_controller is not None else 1
        return max(floor, min(budget, self._pool_ceiling()))

    def _apply_worker_budget(self) -> int:
        """Único punto que escribe la capacidad al semáforo / lane controller (133).

        Llamar con ``_budget_lock`` tomado. Devuelve el presupuesto efectivo.
        """
        budget = self._effective_worker_budget()
        if self._lane_controller is not None:
            self._lane_controller.set_total_budget(budget)
        else:
            self._concurrency_limit.set_capacity(budget)
        return budget

    @property
    def worker_cap(self) -> int | None:
        """Techo manual de `worker`s vigente, o None si no hay (133)."""
        return self._user_cap

    @property
    def effective_workers(self) -> int:
        """Presupuesto efectivo del pool en cualquiera de los dos modos (133)."""
        return self._current_total_workers()

    @property
    def pool_ceiling(self) -> int:
        """Máximo que el pool puede llegar a tener (``cmis.workers`` o ``max_threads``)."""
        return self._pool_ceiling()

    def set_worker_cap(self, cap: int | None) -> int:
        """Fija (o quita con None) el techo manual; devuelve el presupuesto efectivo (133)."""
        with self._budget_lock:
            self._user_cap = None if cap is None else max(1, int(cap))
            budget = self._apply_worker_budget()
        _log.info(
            "workers: techo manual %s → presupuesto efectivo %d",
            "quitado" if cap is None else cap,
            budget,
        )
        return budget

    def adjust_worker_cap(self, delta: int) -> int:
        """Mueve el techo manual ``delta`` pasos partiendo del efectivo actual (133).

        Parte del EFECTIVO (no del cap anterior): si el AIMD bajó a 3 y el
        operador aprieta ``-``, espera 2, no ``cap-1``. El resultado queda
        acotado a ``[1, _pool_ceiling()]``.

        Con ``+`` también empuja el presupuesto del AIMD hasta el techo
        nuevo: el AIMD lee el efectivo y converge a ``cap+1``, así que
        subir sólo el techo era un no-op después del primer paso (el
        ``min`` seguía atado al AIMD). El operador pide "más" y obtiene
        más; el AIMD retoma desde ahí si S5 se degrada.
        """
        with self._budget_lock:
            target = self._effective_worker_budget() + int(delta)
            self._user_cap = max(1, min(target, self._pool_ceiling()))
            if delta > 0:
                self._aimd_total = max(self._aimd_total, self._user_cap)
            return self._apply_worker_budget()

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
        # 150 REQ-002: la lista de activos se verifica ANTES de que exista el
        # `batch`. Si está rota, la corrida no arranca — ni siquiera deja una
        # fila de batch a medio empezar.
        self.preflight()
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
            resume_scope = self._resume_scope(resolved_batch_id) if from_stage > 1 else None

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
            # estado terminal esté visible para la pasada final. 144:
            # la pasada se publica como fase de cierre en pool_stats.
            if self._periodic_reconciler is not None:
                stop_reconciler_visibly(self._periodic_reconciler, self._pool_stats)

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

    def _resume_scope(self, batch_id: str) -> set[str]:
        """Los ``txn_num`` que un resume DEBE volver a mirar.

        148 REQ-004: es todo el `batch` MENOS las filas que una corrida
        anterior escribió como ``OUT_OF_SCOPE_RESUME``. Sin esa resta, el
        segundo resume del mismo `batch` vería esas filas dentro del
        alcance y adoptaría documentos que nunca fueron parte de él —
        justo lo que el primer resume decidió no tocar.
        """
        return {
            doc.txn_num
            for doc in self._tracking_store.list_docs_for_batch(batch_id)
            if doc.reason_code != ReasonCode.OUT_OF_SCOPE_RESUME.value
        }

    def preflight(self) -> EligibilitySnapshot | None:
        """150 REQ-002: verifica la lista de activos antes del primer documento.

        Levanta :class:`~cmcourier.domain.exceptions.EligibilityListError` —y
        aborta la corrida— cuando la fuente no abre, le falta una columna
        declarada o tiene cero filas. NO se falla documento por documento: no
        se arranca. Una lista rota no significa "nadie está activo", significa
        que no podemos responder la pregunta; seguir produciría un censo
        impecable y completamente falso.

        Con la perilla apagada es un no-op que devuelve ``None`` y no toca
        ninguna fuente. Idempotente: la verificación corre una sola vez por
        instancia de pipeline.
        """
        if self._eligibility is None or self._eligibility_snapshot is not None:
            return self._eligibility_snapshot
        self._eligibility_snapshot = self._eligibility.verify_list()
        return self._eligibility_snapshot

    def record_eligibility_audit(self, batch_id: str) -> None:
        """150 REQ-005: deja en ``migration_batch`` QUÉ lista se usó.

        El CSV de activos es una foto de un momento. Dentro de seis meses
        alguien va a leer el censo y preguntar *"¿activo según qué lista?"* —
        sin la ruta, la fecha y el conteo de filas no hay forma de contestar.

        ``getattr`` defensivo con el precedente de 124: los dobles de test y
        los stores que sólo implementan el port no tienen por qué exponer el
        método de auditoría.
        """
        snapshot = self._eligibility_snapshot
        if snapshot is None:
            return
        record = getattr(self._tracking_store, "record_eligibility_audit", None)
        if record is None:
            return
        record(
            batch_id,
            source_path=snapshot.path or snapshot.source,
            modified_at=snapshot.modified_at,
            row_count=snapshot.row_count,
        )

    def _resolve_batch_id(self, batch_id: str | None, from_stage: int, batch_size: int) -> str:
        if batch_id is not None:
            self.record_eligibility_audit(batch_id)
            return batch_id
        new_batch_id = self._tracking_store.start_batch(total_records=batch_size)
        self.record_eligibility_audit(new_batch_id)
        # 148 REQ-005: ``batch_size`` es una perilla de memoria, NO un conteo
        # del origen. Se siembra en 0 y S1 lo arma documento por documento —
        # si no, el denominador arranca mintiendo y el censo nunca cierra.
        self._tracking_store.set_source_total(new_batch_id, 0)
        return new_batch_id

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
        resume_scope = self._resume_scope(batch_id) if from_stage > 1 else None
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

    def record_prep_crash(self, trigger: Trigger, batch_id: str, exc: BaseException) -> None:
        """148 REQ-004: una excepción no contemplada en S1..S4 deja su fila.

        Hasta acá ese camino sólo incrementaba un tally en memoria: el
        documento desaparecía del `batch` sin estado terminal y sin razón.

        Si S1 alcanzó a marcarlo ``S1_DONE`` ya está contado en el
        denominador; si reventó antes, se cuenta acá — el censo tiene que
        cerrar en los dos casos.
        """
        txn = self._indexing_service.txn_num_of(trigger)
        excluded = self._exclusion_for(trigger, ReasonCode.CRASHED)
        counted = bool(txn) and self._tracking_store.is_stage_done(
            txn, batch_id, StageStatus.S1_DONE
        )
        self._record_crash(excluded, batch_id, StageStatus.S1_FAILED, exc, count_source=not counted)

    def record_upload_crash(self, item: _StageItem, batch_id: str, exc: BaseException) -> None:
        """148 REQ-004: el crash de S5 — el bug de los ~200 uploads perdidos.

        La excepción no-CMIS no persistía nada, y como
        ``mark_stage_pending`` es ``INSERT OR IGNORE`` el documento se
        quedaba en ``S4_DONE``: subido a los ojos del reporte, nunca
        subido de verdad. Acá termina en ``S5_FAILED`` + ``CRASHED``.

        El documento ya fue contado por S1, así que el denominador no se
        toca.
        """
        self._record_crash(
            self._exclusion_for(item.trigger, ReasonCode.CRASHED, item.document),
            batch_id,
            StageStatus.S5_FAILED,
            exc,
            count_source=False,
        )

    def _record_crash(
        self,
        excluded: ExcludedTrigger,
        batch_id: str,
        stage: StageStatus,
        exc: BaseException,
        *,
        count_source: bool,
    ) -> None:
        """Escribe la fila del crash. Best-effort: el tracking nunca mata al worker."""
        try:
            self._tracking_store.mark_stage_pending(
                _census_record(excluded, batch_id), StageStatus.S1_PENDING
            )
            if count_source:
                self._tracking_store.increment_source_total(batch_id, 1)
            self._tracking_store.mark_stage_terminal(
                excluded.txn_num,
                batch_id,
                stage,
                f"crashed: {type(exc).__name__}",
                reason_code=ReasonCode.CRASHED,
            )
        except Exception:  # noqa: BLE001 — S6 nunca bloquea el pipeline
            _log.exception(
                "pipeline: could not record crash",
                extra={"batch_id": batch_id, "txn_num": excluded.txn_num},
            )

    def _build_record(
        self,
        item: _StageItem,
        batch_id: str,
        stage: StageStatus,
    ) -> MigrationRecord:
        # 147 REQ-004: UN SOLO ORIGEN para las tres escrituras. La identidad
        # que S2 resolvió es la que va a `migration_log`, la que va a
        # CTECIF/CTENUM de RVIMGLOG (el adaptador la lee de ESTE record) y la
        # que armó la clave del mapeo. Pre-147 el tracking guardaba el CIF
        # curado y NIARVILOG el crudo: dos tablas discrepando sobre el mismo
        # documento.
        # 046: hasta que S2 corra (S1_PENDING / S1_SKIPPED) todavía no hay
        # identidad; ahí se cae a la proyección best-effort del trigger, que
        # es exactamente el comportamiento pre-147.
        audit = item.trigger.audit_row()
        identity = item.identity
        return MigrationRecord(
            trigger_shortname=(
                identity.shortname if identity is not None else audit.get("shortname") or ""
            ),
            trigger_cif=(identity.cif if identity is not None else audit.get("cif") or ""),
            trigger_system_id=(
                identity.system_id if identity is not None else audit.get("system_id") or ""
            ),
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
            # 148 REQ-004: el código RVI se persiste SIEMPRE, no sólo en las
            # exclusiones. Hoy no vive en ninguna otra parte de la base — sin
            # él el censo no se puede agrupar por código, que es exactamente
            # la pregunta del operador.
            id_rvi=item.document.index7,
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
        """S0→S1. 148 REQ-004: ningún documento del origen sale de acá sin fila.

        Cuatro salidas posibles por documento, y las cuatro dejan rastro:
        sigue viaje (``S1_DONE``), ya se había subido (``S1_SKIPPED`` +
        ``ALREADY_UPLOADED``), el origen o el operador lo excluyeron
        (``S1_FILTERED`` + su razón), o el escaneo se rompió
        (``S1_FAILED`` + su razón). Pre-148 tres de esos caminos eran un
        ``continue`` pelado.
        """
        rec = recorder or self._metrics
        items: list[_StageItem] = []
        skipped_cross_batch = 0
        # 051: un trigger filtrado es un resultado de primera clase, NO una
        # falla y NO un descarte silencioso.
        filtered = 0
        for trigger in triggers:
            # 097: cancelación cooperativa — dejamos de tomar triggers
            # nuevos; los ya convertidos a items siguen su curso. 148: el
            # trigger que teníamos en la mano deja su fila CANCELLED.
            if not self._cancel_token.checkpoint():
                self._record_cancelled(trigger, batch_id)
                break
            # 148 REQ-001: S0 ya clasificó esta fila (código fuera del
            # allow-list, o fila sin identidad). No se enriquece: se
            # registra y no llega a S2.
            if isinstance(trigger, ExcludedTrigger):
                filtered += 1
                self._record_exclusion(trigger, batch_id, StageStatus.S1_FILTERED)
                continue
            outcome = self._s1_enrich(trigger, batch_id, rec)
            if outcome is None:
                continue
            for excluded in outcome.excluded:
                filtered += 1
                self._record_exclusion(excluded, batch_id, StageStatus.S1_FILTERED)
            for doc in outcome.documents:
                item, skipped_delta = self._s1_admit(trigger, doc, batch_id, resume_scope)
                skipped_cross_batch += skipped_delta
                if item is not None:
                    items.append(item)
        return items, skipped_cross_batch, filtered

    def _record_cancelled(self, trigger: Trigger, batch_id: str) -> None:
        """148: el trigger que estaba en la mano cuando se canceló la corrida."""
        self._record_exclusion(
            self._exclusion_for(trigger, ReasonCode.CANCELLED),
            batch_id,
            StageStatus.S1_FAILED,
            failure=True,
        )

    def _s1_enrich(
        self,
        trigger: Trigger,
        batch_id: str,
        rec: MetricsRecorder,
    ) -> EnrichOutcome | None:
        """Enriquece un trigger. ``None`` = el escaneo falló y ya dejó su fila.

        148 REQ-004: ``RVABREPNotFoundError`` e ``IndexingError`` eran dos
        ``continue`` pelados — el documento se evaporaba sin contador, sin
        fila y con un log que nadie correlaciona.
        """
        audit_shortname = trigger.audit_row().get("shortname") or "<unknown>"
        with StageTimer(
            rec,
            pipeline=self._pipeline_name,
            stage="S1",
            batch_id=batch_id,
            txn_num=audit_shortname,
        ) as timer:
            try:
                return self._indexing_service.enrich_census(trigger)
            except RVABREPNotFoundError:
                timer.mark_failed()
                _log.warning(
                    "pipeline: trigger has no rvabrep rows",
                    extra={"batch_id": batch_id, "shortname": audit_shortname},
                )
                reason = ReasonCode.SOURCE_ROW_NOT_FOUND
            except IndexingError:
                timer.mark_failed()
                _log.exception(
                    "pipeline: indexing failed",
                    extra={"batch_id": batch_id, "shortname": audit_shortname},
                )
                reason = ReasonCode.INDEXING_FAILED
        self._record_exclusion(
            self._exclusion_for(trigger, reason), batch_id, StageStatus.S1_FAILED, failure=True
        )
        return None

    def _s1_admit(
        self,
        trigger: Trigger,
        doc: RVABREPDocument,
        batch_id: str,
        resume_scope: set[str] | None,
    ) -> tuple[_StageItem | None, int]:
        """Decide si *doc* entra al `batch`. Devuelve ``(item, skipped_delta)``."""
        if resume_scope is not None and doc.txn_num not in resume_scope:
            self._record_exclusion(
                self._exclusion_for(trigger, ReasonCode.OUT_OF_SCOPE_RESUME, doc),
                batch_id,
                StageStatus.S1_FILTERED,
            )
            return None, 0
        already_in_batch = self._tracking_store.is_stage_done(
            doc.txn_num, batch_id, StageStatus.S1_DONE
        )
        if not already_in_batch and self._tracking_store.is_uploaded(doc.txn_num):
            # 062: persiste una fila ``S1_SKIPPED`` para que el tab DETAIL
            # + analyzer + `batch show` puedan ver qué docs fueron
            # salteados cross-batch.
            self._record_exclusion(
                self._exclusion_for(trigger, ReasonCode.ALREADY_UPLOADED, doc),
                batch_id,
                StageStatus.S1_SKIPPED,
            )
            return None, 1
        item = _StageItem(trigger=trigger, document=doc)
        if not already_in_batch:
            record = self._build_record(item, batch_id, StageStatus.S1_PENDING)
            self._tracking_store.mark_stage_pending(record, StageStatus.S1_PENDING)
            self._tracking_store.mark_stage_done(doc.txn_num, batch_id, StageStatus.S1_DONE)
            # 148 REQ-005: el denominador se arma a medida que S1 ve los
            # documentos — no se conoce de antemano.
            self._tracking_store.increment_source_total(batch_id, 1)
        return item, 0

    # ------------------------------------------------- 148 REQ-004: el censo

    def _exclusion_for(
        self,
        trigger: Trigger,
        reason: ReasonCode,
        doc: RVABREPDocument | None = None,
    ) -> ExcludedTrigger:
        """Proyecta ``(trigger[, doc])`` al ítem clasificado que se registra.

        Con documento en mano la clave es su ``txn_num`` REAL. Sin él —el
        trigger no matcheó ninguna fila, o el escaneo explotó antes— se
        usa el txn que el propio trigger conozca y, recién en último
        lugar, una clave sintética por identidad: no hay ninguna otra
        cosa que usar, y sin fila el documento vuelve a ser invisible.
        """
        audit = trigger.audit_row()
        if doc is not None:
            txn, id_rvi, file_name = doc.txn_num, doc.index7, doc.file_name
        elif isinstance(trigger, ExcludedTrigger):
            txn, id_rvi, file_name = trigger.txn_num, trigger.id_rvi, trigger.file_name
        else:
            txn = self._indexing_service.txn_num_of(trigger)
            id_rvi, file_name = "", ""
        if not txn:
            txn = f"{reason.value}__{audit.get('shortname') or ''}__{audit.get('system_id') or ''}"
        return ExcludedTrigger(
            reason_code=reason,
            txn_num=txn,
            id_rvi=id_rvi,
            file_name=file_name,
            shortname=audit.get("shortname"),
            cif=audit.get("cif"),
            system_id=audit.get("system_id"),
        )

    def _record_exclusion(
        self,
        excluded: ExcludedTrigger,
        batch_id: str,
        stage: StageStatus,
        *,
        failure: bool = False,
    ) -> None:
        """148 REQ-004: escribe LA fila del censo para un documento que no sigue.

        Siempre tres cosas: la fila (``INSERT OR IGNORE``, con ``id_rvi``
        para que el censo se pueda agrupar por código), el denominador, y
        el estado terminal con su ``reason_code``. ``failure`` elige entre
        ``mark_stage_failed`` (cuenta como reintento) y
        ``mark_stage_terminal`` (terminó su recorrido, no falló).
        """
        reason = excluded.reason_code.value.lower()
        self._tracking_store.mark_stage_pending(
            _census_record(excluded, batch_id), StageStatus.S1_PENDING
        )
        self._tracking_store.increment_source_total(batch_id, 1)
        # 148: UN log por exclusión, con la razón legible por máquina — la
        # misma que va a la columna. Pre-148 cada camino inventaba su propio
        # string (o no logueaba nada).
        _log.info(
            "pipeline: doc excluded at S1",
            extra={"batch_id": batch_id, "txn_num": excluded.txn_num, "reason": reason},
        )
        write = (
            self._tracking_store.mark_stage_failed
            if failure
            else self._tracking_store.mark_stage_terminal
        )
        write(excluded.txn_num, batch_id, stage, reason, reason_code=excluded.reason_code)

    def _record_terminal_reason(
        self,
        item: _StageItem,
        batch_id: str,
        stage: StageStatus,
        reason: ReasonCode,
    ) -> None:
        """148 REQ-004: cierra un documento que YA tiene fila en el `batch`.

        Para los caminos que abortan con el documento a mitad del
        `pipeline` (cancelación, claim perdido, crash): la fila existe
        desde S1, así que no hace falta insertarla ni volver a contar el
        denominador — sólo que deje de estar en un estado progresivo.
        """
        self._tracking_store.mark_stage_terminal(
            item.document.txn_num,
            batch_id,
            stage,
            reason.value.lower(),
            reason_code=reason,
        )

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
        """S2 para un item: PRIMERO la identidad, después el mapeo.

        147 REQ-003: la resolución de identidad se cuelga del arranque de S2
        en vez de tener etapa propia (ver "Fuera de alcance" del spec: una
        S1.5 costaba migrar ``migration_log``, estados nuevos en toda la
        máquina de recovery, consola y docs). El precio es que **S2 pasa a
        poder hacer red**; el beneficio, que la identidad ya está disponible
        para la clave del mapeo y para las tres escrituras.

        Devuelve ``(survivor_or_None, counted_failure)`` — una falla ya
        marcada como done en una corrida previa se descarta sin contar.
        """
        # 097: cancelación cooperativa — el item se saltea sin contar
        # como falla; queda pendiente para un resume. 148 REQ-004: pero
        # deja de quedar en un estado progresivo sin explicación.
        if not self._cancel_token.checkpoint():
            self._record_terminal_reason(
                item, batch_id, StageStatus.S2_FAILED, ReasonCode.CANCELLED
            )
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
                self._resolve_identity(item)
                # 150 REQ-003: la elegibilidad va INMEDIATAMENTE después de la
                # identidad y ANTES del mapeo — para saber si el cliente está
                # activo hay que saber primero quién es el cliente. Que
                # ``_resolve_identity`` levante antes es la precedencia de
                # REQ-004: un documento cuya identidad no resuelve nunca llega
                # a evaluarse contra la lista.
                if not self._client_is_active(item):
                    self._record_terminal_reason(
                        item, batch_id, StageStatus.S2_FAILED, ReasonCode.CLIENT_NOT_ACTIVE
                    )
                    return None, False
                # 145 REQ-001: la clave del mapping es ``(sistema, IDRVI)``.
                mapping = self._mapping_service.get_mapping(
                    item.document.index7, self._s2_system_id(item)
                )
            # 147 REQ-003: ``IdentityResolutionError`` desciende de
            # ``MappingError`` justo para esto — una identidad que no resolvió
            # es un ``S2_FAILED`` con su motivo, no un estado nuevo.
            except (IDRViNotMappedError, IdentityResolutionError) as exc:
                timer.mark_failed()
                if not self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S2_DONE):
                    record = self._build_record(item, batch_id, StageStatus.S2_PENDING)
                    self._tracking_store.mark_stage_pending(record, StageStatus.S2_PENDING)
                    self._tracking_store.mark_stage_failed(
                        txn,
                        batch_id,
                        StageStatus.S2_FAILED,
                        str(exc),
                        reason_code=self._s2_reason(exc),
                    )
                    return None, True
                return None, False
        if not self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S2_DONE):
            record = self._build_record(item, batch_id, StageStatus.S2_PENDING)
            self._tracking_store.mark_stage_pending(record, StageStatus.S2_PENDING)
            self._tracking_store.mark_stage_done(txn, batch_id, StageStatus.S2_DONE)
        item.mapping = mapping
        return item, False

    def _s2_reason(self, exc: IDRViNotMappedError | IdentityResolutionError) -> ReasonCode:
        """148 REQ-002: las tres razones que hoy colapsan en ``S2_FAILED``.

        Las tres son del balde ``BLOQUEADO`` —las arregla el operador—
        pero editando archivos DISTINTOS: el YAML de identidad,
        ``MapeoRVI_CM.csv``, o el manifest de tipos del server. Un
        ``S2_FAILED`` con texto libre no permite separarlas.

        El ``is True`` es deliberado: ``missing_from_manifest`` puede no
        existir (modos consolidado/split, dobles de test), y un atributo
        cualquiera no debe valer como "sí".
        """
        if isinstance(exc, IdentityResolutionError):
            return ReasonCode.IDENTITY_UNRESOLVED
        missing = getattr(self._mapping_service, "missing_from_manifest", None)
        if missing is not None and missing(exc.id_rvi) is True:
            return ReasonCode.TYPE_NOT_IN_MANIFEST
        return ReasonCode.CODE_NOT_MAPPED

    def _resolve_identity(self, item: _StageItem) -> None:
        """147 REQ-003: resuelve la identidad y la cuelga del item.

        No-op sin resolver cableado. Levanta
        :class:`~cmcourier.domain.exceptions.IdentityResolutionError` cuando
        un slot con ``on_missing: fail`` no resolvió; el caller la traduce a
        ``S2_FAILED``.
        """
        if self._identity_resolver is None:
            return
        outcome = self._identity_resolver.resolve_outcome(item.trigger, item.document)
        item.identity = outcome.identity
        item.identity_fields = outcome.fields

    def _client_is_active(self, item: _StageItem) -> bool:
        """150 REQ-003: ¿el cliente de este documento tiene producto activo?

        Sin servicio cableado (la perilla apagada) devuelve SIEMPRE ``True``
        sin consultar nada: byte-equivalente al pre-150.

        Se le pasan los campos que la resolución de identidad ya resolvió
        (147 REQ-003) como semilla; lo que un ``match_any`` pida de más lo
        resuelve el mismo motor de ``field_sources``, memoizado por corrida.
        """
        if self._eligibility is None:
            return True
        return self._eligibility.is_active(item.trigger, item.document, item.identity_fields)

    def _s2_system_id(self, item: _StageItem) -> str | None:
        """La primera mitad de la clave del mapeo.

        147 REQ-003: sale de la identidad SÓLO cuando el YAML declaró
        ``identity.system_id``. Sin declarar, sigue saliendo de
        ``trigger_system_id`` — un slot ausente se comporta exactamente como
        antes del 147. El ``or None`` conserva la semántica de "vacío ⇒
        comodín" que ``get_mapping`` espera.
        """
        resolver = self._identity_resolver
        if item.identity is not None and resolver is not None and resolver.declares("system_id"):
            return item.identity.system_id or None
        return trigger_system_id(item.trigger)

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
        # 148 REQ-004: con fila terminal, no colgado en un estado progresivo.
        if not self._cancel_token.checkpoint():
            self._record_terminal_reason(
                item, batch_id, StageStatus.S3_FAILED, ReasonCode.CANCELLED
            )
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
                    # 147 REQ-003: lo que S2 ya resolvió entra como semilla —
                    # un BAC_CIF que costó tres saltos no se vuelve a
                    # consultar acá, ni sus eslabones intermedios.
                    resolution = self._metadata_service.resolve(
                        item.trigger,
                        item.document,
                        item.mapping,
                        seed=item.identity_fields,
                    )
                # 149 REQ-001: `DefaultValidationFailedError` quedó sin uso en
                # el runtime (el `default_value` ya no se valida). Se deja en
                # el `except` un ciclo mientras la excepción sigue deprecada;
                # sacarla no cambia comportamiento.
                except (SourceFailedError, DefaultValidationFailedError) as exc:
                    timer.mark_failed()
                    if not self._tracking_store.is_stage_done(txn, batch_id, StageStatus.S3_DONE):
                        record = self._build_record(item, batch_id, StageStatus.S3_PENDING)
                        self._tracking_store.mark_stage_pending(record, StageStatus.S3_PENDING)
                        self._tracking_store.mark_stage_failed(
                            txn,
                            batch_id,
                            StageStatus.S3_FAILED,
                            str(exc),
                            reason_code=ReasonCode.METADATA_UNRESOLVED,
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
        # 148 REQ-004: con fila terminal, no colgado en un estado progresivo.
        if not self._cancel_token.checkpoint():
            self._record_terminal_reason(
                item, batch_id, StageStatus.S4_FAILED, ReasonCode.CANCELLED
            )
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
                        txn,
                        batch_id,
                        StageStatus.S4_FAILED,
                        str(exc),
                        reason_code=(
                            ReasonCode.SOURCE_FILE_MISSING
                            if isinstance(exc, SourceFileMissingError)
                            else ReasonCode.ASSEMBLY_FAILED
                        ),
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
            # 148 REQ-004: sin esto el doc quedaba en ``S5_PENDING`` para
            # siempre — la fila recién insertada arriba, y nadie que la
            # cierre. Terminal + ``CLAIM_LOST``: se ve en el censo y el
            # operador sabe que otro proceso se lo llevó.
            self._record_terminal_reason(
                item, batch_id, StageStatus.S5_FAILED, ReasonCode.CLAIM_LOST
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
        # 148 REQ-004: el doc está en ``S4_DONE``; sin fila terminal el censo
        # lo cuenta como "preparado" y nadie sabe que nunca se intentó subir.
        if not self._cancel_token.checkpoint():
            self._record_terminal_reason(
                item, batch_id, StageStatus.S5_FAILED, ReasonCode.CANCELLED
            )
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
                    cm_object_id = self._upload_with_reauth(
                        item, batch_id, txn, folder_path, object_type_id, timer=timer
                    )
                except (CMISClientError, CMISServerError, RetriesExhaustedError) as exc:
                    timer.mark_failed()
                    # 104: clasifica la falla y la contabiliza por tipo +
                    # status HTTP. Se hace acá — con la excepción en mano —
                    # porque el outcome string que ve el loop consumidor ya
                    # perdió el tipo.
                    category, status_code = classify_failure(exc)
                    (recorder or self._metrics).record_upload_failed(category, status_code)
                    # 148 REQ-002: el MISMO clasificador que alimenta las
                    # métricas alimenta ahora la base. Un solo criterio.
                    reason = _CM_REASONS[category]
                    if self._coordinator is not None:
                        self._coordinator.mark_failed(
                            record=record,
                            document=item.document,
                            mapping=item.mapping,
                            trigger=item.trigger,
                            stage=StageStatus.S5_FAILED,
                            error=str(exc),
                            reason_code=reason,
                        )
                    else:
                        self._tracking_store.mark_stage_failed(
                            txn, batch_id, StageStatus.S5_FAILED, str(exc), reason_code=reason
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

    _MAX_REAUTH_EPISODES = 2

    def _upload_with_reauth(
        self,
        item: _StageItem,
        batch_id: str,
        txn: str,
        folder_path: str,
        object_type_id: str,
        *,
        timer: StageTimer | None = None,
    ) -> str:
        """132: el upload de S5 con hasta dos episodios de re-autenticación.

        Sin handler registrado es una sola llamada al uploader (headless).
        Con handler, un 401 pausa la corrida y espera credenciales nuevas;
        si llegan, reintenta el MISMO doc dentro del mismo slot y timer.
        La espera se le descuenta al ``timer`` — es tiempo del operador,
        no del upload, y el p95 de S5 alimenta al AIMD.
        Al tercer 401 consecutivo (o si cancelan durante la espera) la
        excepción sube y el doc se marca ``S5_FAILED`` como siempre."""
        assert item.staged_file is not None
        assert item.metadata is not None
        episodes = 0
        while True:
            generation = self._cred_generation
            try:
                return self._uploader.upload(
                    file=item.staged_file,
                    folder_path=folder_path,
                    object_type_id=object_type_id,
                    document_name=f"{txn}.pdf",
                    mime_type="application/pdf",
                    properties=dict(item.metadata.properties),
                    batch_id=batch_id,
                )
            except CMISClientError as exc:
                if (
                    exc.status_code != 401
                    or self._auth_expired_handler is None
                    or episodes >= self._MAX_REAUTH_EPISODES
                ):
                    raise
                episodes += 1
                _log.warning(
                    "S5 txn=%s: sesión CMIS rechazada (401) — corrida pausada, "
                    "esperando credenciales nuevas (episodio %d/%d)",
                    txn,
                    episodes,
                    self._MAX_REAUTH_EPISODES,
                )
                waited_from = time.monotonic()
                try:
                    if not self._await_reauth(generation):
                        raise
                finally:
                    if timer is not None:
                        timer.exclude(time.monotonic() - waited_from)

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
