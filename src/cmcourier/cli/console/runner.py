"""ConsoleRunManager — el motor de lanzamiento de la consola (124).

Replica la plomería de ``_run_with_optional_tui`` (selección de
orchestrator por modo, semántica de resume, watchdog de deadline) pero
con el ciclo de vida de la consola: lock de config sostenido durante la
corrida, auditoría persistida por batch (C3), y notificación a la app
al terminar. El pipeline se construye SIEMPRE sin auto-doctor — el
gate es de la consola (C2).
"""

from __future__ import annotations

__all__ = ["ConsoleRunManager", "LaunchSpec", "build_data_provider"]

import getpass
import hashlib
import logging
import platform
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cmcourier.cli.commands._lock import acquire_config_lock
from cmcourier.cli.console.overrides import apply_overrides
from cmcourier.config.schema import CsvTriggerConfig, PipelineConfig
from cmcourier.config.wiring import build_pipeline
from cmcourier.observability.setup import configure as configure_observability
from cmcourier.orchestrators.multi_batch import MultiBatchOrchestrator, MultiBatchRunReport
from cmcourier.orchestrators.staged import StagedPipeline
from cmcourier.orchestrators.streaming import StreamingOrchestrator
from cmcourier.services.deadline import DeadlineWatchdog
from cmcourier.services.triggers import SingleDocTriggerStrategy
from cmcourier.tui import TUIDataProvider

if TYPE_CHECKING:
    from cmcourier.cli.console.app import ConsoleApp

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Lo que el operador eligió en [5] CORRER."""

    total: int | None = None
    max_duration_s: float | None = None
    resume_batch_id: str | None = None
    shortname: str | None = None  # single_doc
    system_id: str | None = None
    cif: str | None = None


def build_data_provider(
    pipeline: StagedPipeline,
    orchestrator: MultiBatchOrchestrator | StreamingOrchestrator,
    config: PipelineConfig,
    *,
    planned_total: int | None = None,
) -> TUIDataProvider:
    """El mismo cableado que arma ``cli/app.py`` para la TUI clásica."""
    bucket_provider = (
        orchestrator.streaming_snapshot if isinstance(orchestrator, StreamingOrchestrator) else None
    )
    return TUIDataProvider(
        pipeline_name=pipeline.pipeline_name,
        metrics_recorder=pipeline.metrics_recorder,
        pool_stats=pipeline.pool_stats,
        concurrency_limit=pipeline.concurrency_limit,
        cmis_config=config.cmis,
        uploader=pipeline.uploader,
        auto_tune=pipeline.auto_tune_controller,
        recorder_provider=orchestrator.active_recorder,
        upload_recorder_provider=orchestrator.upload_recorder,
        chunks_provider=orchestrator.chunks_snapshot,
        lane_controller=pipeline.lane_controller,
        tracking_store=pipeline.tracking_store,
        mode=config.processing.mode,
        bucket_provider=bucket_provider,
        planned_total=planned_total,
    )


class ConsoleRunManager:
    """Una corrida por vez. La app consulta ``active`` y recibe
    ``on_run_finished(manager)`` en el thread de la UI al terminar."""

    def __init__(self, app: ConsoleApp) -> None:
        self.app = app
        self.orchestrator: MultiBatchOrchestrator | StreamingOrchestrator | None = None
        self.provider: TUIDataProvider | None = None
        self.pipeline: StagedPipeline | None = None
        self.report: MultiBatchRunReport | None = None
        self.exception: BaseException | None = None
        self.effective: PipelineConfig | None = None
        self.spec: LaunchSpec | None = None
        self._lock_cm: Any = None
        self._thread: threading.Thread | None = None
        self._watchdog: DeadlineWatchdog | None = None
        # 132: True entre el aviso de "sesión CMIS rechazada" y el resume
        # que empuja la credencial nueva al pipeline.
        self.reauth_pending = False
        self._audit_config_hash = ""
        self._audit_overrides_json = "{}"

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        """132: la compuerta del token está cerrada (corrida en pausa)."""
        orch = self.orchestrator
        return orch is not None and orch.cancel_token.is_paused()

    def outcome(self) -> str:
        if self.exception is not None:
            return "failed"
        orch = self.orchestrator
        if orch is not None and orch.cancel_token.is_cancelled():
            return "cancelled"
        return "completed"

    # ------------------------------------------------------------ launch

    def launch(self, spec: LaunchSpec) -> None:
        """Adquiere el lock, construye todo y arranca el worker.

        Propaga ``LockHeldError`` / ``ConfigurationError`` — la app las
        traduce a modales. Si algo falla después del lock, se libera.
        """
        app = self.app
        self.spec = spec
        effective = apply_overrides(app.config, app.state.overrides)
        self.effective = effective
        # M7: unmask_pii es de proceso — se re-aplica al lanzar.
        configure_observability(effective.observability, app.log_level, tui_active=True)
        self._lock_cm = acquire_config_lock(app.config_path)
        self._lock_cm.__enter__()
        try:
            pipeline, kwargs = self._build(effective, spec)
        except BaseException:
            self._release_lock()
            raise
        self.pipeline = pipeline
        self.reauth_pending = False
        # I4: la auditoría describe el config que la corrida USÓ. Si el
        # operador escribe el YAML con `w` (135) mientras corre, el hash y
        # los overrides al cierre ya no serían los de esta corrida.
        self._audit_config_hash = hashlib.sha256(app.config_path.read_bytes()).hexdigest()[:12]
        self._audit_overrides_json = app.state.overrides.to_json()
        # 132: un 401 en S5 pausa la corrida y avisa acá (worker thread).
        pipeline.set_auth_expired_handler(self._on_auth_expired)
        self.provider = build_data_provider(
            pipeline,
            self.orchestrator,  # type: ignore[arg-type]
            effective,
            planned_total=spec.total,  # 134: la ETA de corrida necesita el total
        )
        if spec.max_duration_s is not None:
            assert self.orchestrator is not None
            self._watchdog = DeadlineWatchdog(self.orchestrator.cancel_token, spec.max_duration_s)
            self._watchdog.start()
        self._thread = threading.Thread(
            target=self._worker, args=(kwargs,), name="cmcourier-console-run", daemon=False
        )
        self._thread.start()

    def _build(
        self, effective: PipelineConfig, spec: LaunchSpec
    ) -> tuple[StagedPipeline, dict[str, Any]]:
        kind = getattr(effective.trigger, "kind", "csv")
        strategy = None
        pipeline_name = f"{kind}-trigger"
        if kind == "single_doc":
            strategy = SingleDocTriggerStrategy(
                shortname=spec.shortname or "",
                system_id=spec.system_id or "",
                cif=spec.cif or None,
            )
            pipeline_name = "single-doc"
        pipeline = build_pipeline(
            effective,
            self.app.state.creds.to_secrets(),
            trigger_strategy_override=strategy,
            pipeline_name=pipeline_name,
        )
        if effective.processing.mode == "streaming":
            self.orchestrator = StreamingOrchestrator(
                pipeline=pipeline, config=effective, log_dir=effective.observability.log_dir
            )
        else:
            self.orchestrator = MultiBatchOrchestrator(
                pipeline=pipeline, config=effective, log_dir=effective.observability.log_dir
            )
        source_descriptor = (
            str(effective.trigger.csv_path)
            if isinstance(effective.trigger, CsvTriggerConfig)
            else ""
        )
        kwargs: dict[str, Any] = {
            "source_descriptor": source_descriptor,
            "batch_size": effective.batch_size,
            # resume y batch nombrado son inherentemente single-batch.
            "batches_in_flight": 1
            if spec.resume_batch_id
            else effective.processing.batches_in_flight,
            "from_stage": 1,
            "resume_batch_id": spec.resume_batch_id,
            "total": spec.total,
        }
        return pipeline, kwargs

    # ------------------------------------------------------------ worker

    def _worker(self, kwargs: dict[str, Any]) -> None:
        assert self.orchestrator is not None and self.provider is not None
        self.provider.mark_batch_started(batch_id=kwargs.get("resume_batch_id") or "")
        try:
            self.report = self.orchestrator.run(**kwargs)
        except BaseException as exc:  # noqa: BLE001 — la app lo muestra, no revienta
            self.exception = exc
            _log.exception("console: la corrida terminó con excepción")
        finally:
            self.provider.mark_batch_complete()
            self._audit()
            self._release_lock()
            self.app.call_from_thread(self.app.on_run_finished, self)

    def _audit(self) -> None:
        """C3: auditoría por batch — nunca aborta el cierre de la corrida."""
        try:
            store = self.pipeline.tracking_store if self.pipeline else None
            if store is None or self.effective is None:
                return
            record_audit = getattr(store, "record_batch_audit", None)
            set_outcome = getattr(store, "set_batch_outcome", None)
            if record_audit is None or set_outcome is None:
                return
            outcome = self.outcome()
            for batch_id in self._batch_ids():
                record_audit(
                    batch_id,
                    operator=getpass.getuser(),
                    station=platform.node(),
                    pipeline_kind=str(getattr(self.effective.trigger, "kind", "?")),
                    environment=self.effective.environment,
                    config_hash=self._audit_config_hash,
                    overrides_json=self._audit_overrides_json,
                    doctor_verdict=self.app.state.doctor_verdict(),
                )
                set_outcome(batch_id, outcome)
        except Exception:  # noqa: BLE001
            _log.exception("console: fallo al persistir la auditoría del batch")

    def _batch_ids(self) -> list[str]:
        if self.report is None:
            return []
        return [r.batch_id for r in self.report.chunks if r.batch_id]

    def _release_lock(self) -> None:
        if self._lock_cm is None:
            return
        cm, self._lock_cm = self._lock_cm, None
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            _log.exception("console: fallo al liberar el lock de config")

    # ------------------------------------------------------------ control

    def cancel(self) -> None:
        if self.orchestrator is not None:
            self.orchestrator.cancel_token.cancel()

    # 132: pausa cooperativa + re-auth en caliente. ``pause``/``resume``
    # se llaman desde el thread de la UI; ``_on_auth_expired`` llega desde
    # un worker del pipeline y sólo despacha a la UI.

    def pause(self) -> None:
        if self.orchestrator is not None:
            self.orchestrator.cancel_token.pause()

    def resume(self) -> None:
        """Abre la compuerta. Si hay una re-auth pendiente, ANTES empuja la
        credencial CMIS de [2] al pipeline — los workers reintentan con la
        sesión nueva, no con la rechazada."""
        if self.orchestrator is None:
            return
        if self.reauth_pending and self.pipeline is not None:
            cred = self.app.state.creds.get("cmis")
            self.pipeline.set_cmis_credentials(cred.username, cred.password)
            self.reauth_pending = False
        self.orchestrator.cancel_token.resume()

    # 133: techo manual de workers — el clamp vive en el pipeline.

    @property
    def worker_cap(self) -> int | None:
        return None if self.pipeline is None else self.pipeline.worker_cap

    @property
    def effective_workers(self) -> int | None:
        return None if self.pipeline is None else self.pipeline.effective_workers

    @property
    def pool_ceiling(self) -> int | None:
        return None if self.pipeline is None else self.pipeline.pool_ceiling

    def adjust_workers(self, delta: int) -> int | None:
        """Mueve el techo ``delta`` pasos; None si no hay pipeline (corrida idle)."""
        if self.pipeline is None:
            return None
        return self.pipeline.adjust_worker_cap(delta)

    def _on_auth_expired(self) -> None:
        self.reauth_pending = True
        self.app.call_from_thread(self.app.on_auth_expired, self)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
