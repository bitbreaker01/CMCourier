"""Orchestrator multi-batch producer-consumer (028 — POST-MVP §7).

Envuelve un :class:`StagedPipeline` para correr múltiples `chunk`s del
origen de triggers con hasta ``batches_in_flight`` `chunk`s en vuelo
simultáneamente. Para ``batches_in_flight == 1`` el orchestrator
delega directamente a ``pipeline.run(...)`` (sin overhead). Para
``N == 2`` lanza un `thread` de prep (S0..S4) y un `thread` de upload
(S5) que se comunican a través de una `queue` acotada chica.

Semántica por `chunk`:
    * Cada `chunk` obtiene su **propio** ``batch_id`` del
      `tracking store`.
    * Cada `chunk` obtiene su **propio** :class:`MetricsRecorder`
      para que los archivos `slow-ops` por `chunk` + los eventos
      `batch_summary` por `chunk` queden aislados. Los handlers de
      `slow-op` de los recorders filtran por ``record.batch_id``
      (ver 028 fase 2).
    * El `worker pool` de S5 (`semaphore` + ThreadPoolExecutor) se
      **comparte** entre `chunk`s — la concurrencia total de upload
      se mantiene en ``cmis.workers``.

Aislamiento de fallas: una excepción en el prep o upload de un
`chunk` se loguea en ERROR y el `chunk` se agrega a
``failed_chunks``. Los `chunk`s restantes continúan. El exit code
del reporte agregado refleja si algún `chunk` reportó fallas en
s5 o si crasheó directamente.
"""

from __future__ import annotations

__all__ = ["ChunkState", "MultiBatchOrchestrator", "MultiBatchRunReport"]

import itertools
import logging
import queue
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cmcourier.config.schema import PipelineConfig
from cmcourier.domain.models import (  # noqa: F401 — TriggerRecord re-exportado
    Trigger,
    TriggerRecord,
)
from cmcourier.observability.metrics import MetricsRecorder
from cmcourier.orchestrators.chunked import chunked
from cmcourier.orchestrators.staged import RunReport, StagedPipeline, _StageItem
from cmcourier.services.cancellation import CancellationToken
from cmcourier.services.reconciler import stop_reconciler_visibly

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChunkState:
    """Una fila en la máquina de estados de `chunk`s del orchestrator (030, binding del TUI).

    Estados: ``QUEUED``, ``PREP``, ``UPLOAD``, ``DONE``, ``FAILED``.

    041 agrega el desglose por `stage` que alimenta la tabla del tab CHUNKS y
    el display por `chunk` de MB/timer/ETA del tab UPLOAD. Los campos
    ``*_monotonic`` se setean cuando el `chunk` transiciona al `stage`; los
    campos ``*_elapsed_s`` se congelan cuando lo deja. Mientras el `chunk`
    está vivo en un `stage`, el `consumer` deriva elapsed = ``now - started``.
    """

    chunk_idx: int
    batch_id: str
    status: str
    s5_done: int = 0
    s5_failed: int = 0
    # 041 — plan por `chunk` + estadísticas por `stage`
    doc_count: int = 0
    total_bytes: int = 0
    prep_done: int = 0
    prep_skipped: int = 0
    prep_failed: int = 0
    # 051 — docs filtrados en S1 (filas RVABREP con código de baja)
    prep_filtered: int = 0
    upload_skipped: int = 0
    prep_started_monotonic: float | None = None
    prep_elapsed_s: float = 0.0
    upload_started_monotonic: float | None = None
    upload_elapsed_s: float = 0.0


@dataclass(frozen=True, slots=True)
class MultiBatchRunReport:
    """Resultado agregado de una corrida multi-batch."""

    chunks: list[RunReport]
    failed_chunks: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_triggers(self) -> int:
        return sum(r.total_triggers for r in self.chunks)

    @property
    def total_docs(self) -> int:
        return sum(r.total_docs for r in self.chunks)

    @property
    def s5_done(self) -> int:
        return sum(r.s5_done for r in self.chunks)

    @property
    def s5_failed(self) -> int:
        return sum(r.s5_failed for r in self.chunks)

    @property
    def s1_filtered(self) -> int:
        """051 — docs filtrados en S1 (filas RVABREP con código de baja)."""
        return sum(r.s1_filtered for r in self.chunks)

    @property
    def elapsed_seconds(self) -> float:
        return sum(r.elapsed_seconds for r in self.chunks)


# ---------------------------------------------------------------------------
# Forma interna del handoff
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PreparedChunk:
    """Handoff desde el `thread` de prep al `thread` de upload."""

    batch_id: str
    chunk_idx: int
    triggers: list[Trigger]
    items: list[_StageItem]
    skipped: int
    s1_done: int
    s1_filtered: int
    s2_failed: int
    s3_failed: int
    s4_failed: int
    recorder: MetricsRecorder
    started_at: float
    prep_failure: BaseException | None = None


# `Sentinel` que el `thread` de prep deja en la `queue` de upload para
# señalizar "no hay más `chunk`s". El `thread` de upload drena y sale.
_PREP_DONE = object()


def _items_total_bytes(items: Sequence[object]) -> int:
    """Suma ``staged_file.size_bytes`` defensivamente sobre los items de un `chunk`.

    Los items de producción siempre llevan un ``staged_file`` después de S4.
    Los `stub`s de tests unitarios (``SimpleNamespace``, etc.) a veces no
    — se cae a 0 para cualquier item que no exponga la cadena.
    """
    total = 0
    for it in items:
        staged = getattr(it, "staged_file", None)
        if staged is None:
            continue
        size = getattr(staged, "size_bytes", None)
        if isinstance(size, int):
            total += size
    return total


class MultiBatchOrchestrator:
    """Corre un ``StagedPipeline`` con overlap producer-consumer."""

    def __init__(
        self,
        *,
        pipeline: StagedPipeline,
        config: PipelineConfig,
        log_dir: Path,
    ) -> None:
        self._pipeline = pipeline
        self._config = config
        self._log_dir = log_dir
        # 030: máquina de estados de `chunk`s para el tab CHUNKS del TUI.
        # Indexada por chunk_idx para que los `thread`s de prep / upload
        # puedan actualizar sin lookups. El recorder activo alimenta los
        # bindings en vivo de los tabs PREP/UPLOAD.
        self._chunks_state: dict[int, ChunkState] = {}
        self._active_recorder: MetricsRecorder | None = None
        # 042: slot separado para el binding del lado UPLOAD para que el
        # flip de PREP (que se setea cuando el `chunk` N+1 entra a PREP
        # mientras el `chunk` N sigue subiendo) ya no perturbe el display
        # de percentiles + MB del tab UPLOAD. ``upload_recorder()``
        # devuelve esto; ``active_recorder()`` conserva la semántica
        # pre-042 "el más reciente entre PREP-o-UPLOAD" para el tab PREP.
        self._upload_active_recorder: MetricsRecorder | None = None
        self._state_lock = threading.Lock()

    @property
    def cancel_token(self) -> CancellationToken:
        """097: token de cancelación, delegado al StagedPipeline interno."""
        return self._pipeline.cancel_token

    # ----- Hooks de binding del TUI (030) ----------------------------

    def chunks_snapshot(self) -> list[ChunkState]:
        """Snapshot de sólo lectura del estado de cada `chunk` para el TUI."""
        with self._state_lock:
            return [self._chunks_state[k] for k in sorted(self._chunks_state)]

    def active_recorder(self) -> MetricsRecorder | None:
        """El recorder del `chunk` iniciado más recientemente, o ``None``."""
        with self._state_lock:
            return self._active_recorder

    def upload_recorder(self) -> MetricsRecorder | None:
        """042 — recorder del `chunk` que está actualmente dentro de S5, o ``None``.

        Distinto de ``active_recorder``: cuando el `chunk` N+1 entra a PREP
        mientras el `chunk` N está subiendo, ``active_recorder`` salta a N+1
        pero ``upload_recorder`` se mantiene en N. El tab UPLOAD del TUI se
        bindea acá para que su display de percentiles / MB siga al `chunk`
        que está realmente subiendo, no al que está preparando el próximo
        `batch`.
        """
        with self._state_lock:
            return self._upload_active_recorder

    def _upload_p95_observer(self) -> tuple[float, int]:
        """043 — fuente de p95 para el controller AIMD en modo multi-batch.

        Lee del recorder upload-active para que el controller vea la
        latencia del `chunk` actualmente en S5 — no el recorder propio del
        `pipeline`, que no recibe nada porque cada `chunk` usa su propio
        recorder por `batch`. Devuelve ``(0.0, 0)`` cuando todavía no hay
        ningún `chunk` subiendo (durante el `warmup`), lo que matchea la
        semántica del controller "sin datos → asumir holgura".

        061: devuelve ``(p95_ms, sample_count)`` para que el controller
        pueda gatear decisiones según un mínimo de muestras y evitar el
        halving por un único outlier de conexión fría en el primer
        `chunk`.
        """
        rec = self.upload_recorder()
        if rec is None:
            return 0.0, 0
        return rec.current_stage_p95_with_count("S5")

    def _update_chunk_state(
        self,
        *,
        chunk_idx: int,
        batch_id: str,
        status: str,
        s5_done: int = 0,
        s5_failed: int = 0,
        doc_count: int | None = None,
        total_bytes: int | None = None,
        prep_done: int | None = None,
        prep_skipped: int | None = None,
        prep_failed: int | None = None,
        prep_filtered: int | None = None,
        upload_skipped: int | None = None,
        prep_started_monotonic: float | None = None,
        prep_elapsed_s: float | None = None,
        upload_started_monotonic: float | None = None,
        upload_elapsed_s: float | None = None,
    ) -> None:
        """Transición atómica para un `chunk`. ``None`` significa "mantener el
        valor previo" para ese campo — así los callers sólo tienen que
        proveer lo que realmente cambió.
        """
        with self._state_lock:
            prev = self._chunks_state.get(chunk_idx)
            self._chunks_state[chunk_idx] = ChunkState(
                chunk_idx=chunk_idx,
                batch_id=batch_id,
                status=status,
                s5_done=s5_done,
                s5_failed=s5_failed,
                doc_count=(doc_count if doc_count is not None else (prev.doc_count if prev else 0)),
                total_bytes=(
                    total_bytes if total_bytes is not None else (prev.total_bytes if prev else 0)
                ),
                prep_done=(prep_done if prep_done is not None else (prev.prep_done if prev else 0)),
                prep_skipped=(
                    prep_skipped if prep_skipped is not None else (prev.prep_skipped if prev else 0)
                ),
                prep_failed=(
                    prep_failed if prep_failed is not None else (prev.prep_failed if prev else 0)
                ),
                prep_filtered=(
                    prep_filtered
                    if prep_filtered is not None
                    else (prev.prep_filtered if prev else 0)
                ),
                upload_skipped=(
                    upload_skipped
                    if upload_skipped is not None
                    else (prev.upload_skipped if prev else 0)
                ),
                prep_started_monotonic=(
                    prep_started_monotonic
                    if prep_started_monotonic is not None
                    else (prev.prep_started_monotonic if prev else None)
                ),
                prep_elapsed_s=(
                    prep_elapsed_s
                    if prep_elapsed_s is not None
                    else (prev.prep_elapsed_s if prev else 0.0)
                ),
                upload_started_monotonic=(
                    upload_started_monotonic
                    if upload_started_monotonic is not None
                    else (prev.upload_started_monotonic if prev else None)
                ),
                upload_elapsed_s=(
                    upload_elapsed_s
                    if upload_elapsed_s is not None
                    else (prev.upload_elapsed_s if prev else 0.0)
                ),
            )

    def _set_active_recorder(self, recorder: MetricsRecorder | None) -> None:
        with self._state_lock:
            self._active_recorder = recorder

    def _set_upload_active_recorder(self, recorder: MetricsRecorder | None) -> None:
        with self._state_lock:
            self._upload_active_recorder = recorder

    # ----- API pública ------------------------------------------------

    def run(
        self,
        *,
        source_descriptor: str,
        batch_size: int,
        batches_in_flight: int,
        from_stage: int = 1,
        resume_batch_id: str | None = None,
        total: int | None = None,
    ) -> MultiBatchRunReport:
        """Adquiere triggers, hace `chunk`ing, y los corre según ``batches_in_flight``.

        ``total`` (033) acota la cantidad de triggers luego del acquire.
        Se aplica de manera uniforme a los paths N=1 y N=2.
        """
        # 150 REQ-002: la lista de activos se verifica antes del primer
        # documento, y una sola vez para todos los `chunk`s. ``getattr``
        # defensivo — paridad con el patrón de los pools para los fakes de
        # test.
        preflight = getattr(self._pipeline, "preflight", None)
        if preflight is not None:
            preflight()
        try:
            if resume_batch_id is not None or batches_in_flight == 1 or from_stage > 1:
                # Resume + single-in-flight + from_stage no default fuerzan
                # todos el path legacy single-batch: preserva la semántica
                # byte-idéntica a las invocaciones pre-028 de ``pipeline.run``.
                return self._run_single(
                    source_descriptor=source_descriptor,
                    batch_size=batch_size,
                    resume_batch_id=resume_batch_id,
                    from_stage=from_stage,
                    total=total,
                )
            if batches_in_flight != 2:
                raise ValueError(
                    f"batches_in_flight={batches_in_flight} not supported "
                    "(spec 028 ships only 1 and 2; 3..5 deferred to a future change)"
                )
            return self._run_overlapped(
                source_descriptor=source_descriptor,
                batch_size=batch_size,
                total=total,
            )
        finally:
            # 119: los pools persistentes del pipeline viven la corrida
            # entera (todos los chunks los comparten) y se cierran acá.
            # ``getattr`` defensivo — paridad con el patrón del
            # reconciler para los fakes de test.
            shutdown_pools = getattr(self._pipeline, "shutdown_worker_pools", None)
            if shutdown_pools is not None:
                shutdown_pools()

    # ----- Path N=1 ---------------------------------------------------

    def _run_single(
        self,
        *,
        source_descriptor: str,
        batch_size: int,
        resume_batch_id: str | None,
        from_stage: int,
        total: int | None = None,
    ) -> MultiBatchRunReport:
        # 050: resume / from_stage>1 operan sobre un `batch` previamente
        # creado y ya acotado — se mantiene el pipeline.run() monolítico.
        # Una corrida N=1 fresca puede enfrentar la fuente completa
        # (20M filas), así que se hace `streaming` `chunk` a `chunk` vía
        # _run_sequential (memoria acotada).
        if resume_batch_id is not None or from_stage > 1:
            report = self._pipeline.run(
                source_descriptor=source_descriptor,
                batch_size=batch_size,
                batch_id=resume_batch_id,
                from_stage=from_stage,
                total=total,
            )
            return MultiBatchRunReport(chunks=[report])
        return self._run_sequential(
            source_descriptor=source_descriptor,
            batch_size=batch_size,
            total=total,
        )

    def _run_sequential(
        self,
        *,
        source_descriptor: str,
        batch_size: int,
        total: int | None = None,
    ) -> MultiBatchRunReport:
        """050: corrida fresca single-in-flight, en `streaming` `chunk` a `chunk`.

        La forma N=1 de :meth:`_run_overlapped` — sin overlap entre
        `thread`s producer-consumer, pero con la misma garantía de
        memoria acotada: los triggers se traen en olas de ``batch_size``,
        y la memoria de cada `chunk` se libera antes de traer el siguiente.
        """
        triggers: Iterator[Trigger] = self._pipeline._trigger_strategy.acquire(  # noqa: SLF001
            source_descriptor
        )
        if total is not None:
            triggers = itertools.islice(triggers, max(0, total))

        results: list[RunReport] = []
        failed: list[tuple[str, str]] = []
        results_lock = threading.Lock()
        controller = self._pipeline.auto_tune_controller
        sampler = self._pipeline.sampler
        if sampler is not None:
            sampler.start()
        if controller is not None:
            controller.set_p95_provider(self._upload_p95_observer)
            controller.start()
        # 096: arranca el reconciliador periódico AS400 para la corrida
        # multi-batch con overlap. getattr defensivo — los dobles de
        # test del pipeline no lo definen.
        periodic_recon = getattr(self._pipeline, "_periodic_reconciler", None)
        if periodic_recon is not None:
            periodic_recon.start()
        # 097: cancelación cooperativa — getattr defensivo porque los
        # dobles de test del pipeline no definen cancel_token.
        cancel_token = getattr(self._pipeline, "cancel_token", None)
        try:
            for idx, chunk in enumerate(chunked(triggers, batch_size)):
                # No arrancamos chunks nuevos; el chunk en vuelo drena
                # vía los chequeos per-doc del StagedPipeline.
                if cancel_token is not None and not cancel_token.checkpoint():
                    break
                prepared = self._prep_one_chunk(
                    idx, chunk, failed=failed, results_lock=results_lock
                )
                if prepared is not None:
                    self._upload_one_chunk(
                        prepared, results=results, failed=failed, results_lock=results_lock
                    )
        finally:
            if periodic_recon is not None:
                # 144: la pasada final se ve en el monitor como fase de
                # cierre (``cerrando · sincronizando AS400 k/N``).
                stop_reconciler_visibly(periodic_recon, getattr(self._pipeline, "pool_stats", None))
            if controller is not None:
                controller.stop(timeout=2.0)
            if sampler is not None:
                sampler.stop()

        results.sort(key=lambda r: r.batch_id)
        return MultiBatchRunReport(chunks=results, failed_chunks=failed)

    # ----- pasos compartidos por `chunk` (N=1 + N=2) ------------------

    def _prep_one_chunk(
        self,
        idx: int,
        chunk: list[Trigger],
        *,
        failed: list[tuple[str, str]],
        results_lock: threading.Lock,
    ) -> _PreparedChunk | None:
        """Corre S0..S4 para un `chunk`. Compartido por los paths N=1
        (_run_sequential) y N=2 (_run_overlapped). Devuelve el `chunk`
        preparado, o ``None`` cuando el prep falló (la falla se registra
        en ``failed``).

        El estado del `chunk` se siembra acá apenas se trae el `chunk` —
        no hay siembra de QUEUED por adelantado (conocer el conteo de
        `chunk`s implica materializar el conjunto completo de triggers,
        lo que 050 existe para evitar).
        """
        try:
            batch_id = self._pipeline._resolve_batch_id(  # noqa: SLF001
                None, from_stage=1, batch_size=len(chunk)
            )
            recorder = self._build_chunk_recorder()
            recorder.start_batch(pipeline=self._pipeline.pipeline_name, batch_id=batch_id)
            started = time.monotonic()
            self._update_chunk_state(
                chunk_idx=idx,
                batch_id=batch_id,
                status="PREP",
                prep_started_monotonic=started,
            )
            self._set_active_recorder(recorder)
            items, skipped, s1d, s1_filtered, s2f, s3f, s4f = self._pipeline.prep_chunk(
                triggers=chunk,
                batch_id=batch_id,
                recorder=recorder,
            )
            prep_elapsed = time.monotonic() - started
            total_bytes = _items_total_bytes(items)
            # Congela el desglose del lado PREP apenas el prep termina.
            self._update_chunk_state(
                chunk_idx=idx,
                batch_id=batch_id,
                status="PREP",
                doc_count=s1d + skipped + s1_filtered,
                total_bytes=total_bytes,
                prep_done=len(items),
                prep_skipped=skipped,
                prep_failed=s2f + s3f + s4f,
                prep_filtered=s1_filtered,
                prep_elapsed_s=prep_elapsed,
            )
            return _PreparedChunk(
                batch_id=batch_id,
                chunk_idx=idx,
                triggers=chunk,
                items=items,
                skipped=skipped,
                s1_done=s1d,
                s1_filtered=s1_filtered,
                s2_failed=s2f,
                s3_failed=s3f,
                s4_failed=s4f,
                recorder=recorder,
                started_at=started,
            )
        except BaseException as exc:  # noqa: BLE001 — registrado, la corrida continúa
            _log.exception(
                "multi-batch: prep failed",
                extra={"chunk_idx": idx, "reason": type(exc).__name__},
            )
            # 122: única lectura de ``_chunks_state`` que faltaba
            # proteger — el thread de upload puede estar escribiéndolo.
            with self._state_lock:
                failed_batch_id = self._chunks_state.get(
                    idx, ChunkState(chunk_idx=idx, batch_id="", status="FAILED")
                ).batch_id
            self._update_chunk_state(
                chunk_idx=idx,
                batch_id=failed_batch_id,
                status="FAILED",
            )
            with results_lock:
                failed.append((f"chunk-{idx}", type(exc).__name__))
            return None

    def _upload_one_chunk(
        self,
        item: _PreparedChunk,
        *,
        results: list[RunReport],
        failed: list[tuple[str, str]],
        results_lock: threading.Lock,
    ) -> None:
        """Corre S5 para un `chunk` preparado. Compartido por los paths
        N=1 y N=2. Agrega un :class:`RunReport` a ``results`` cuando
        sale bien, o un par ``(batch_id, reason)`` a ``failed`` cuando
        falla.
        """
        upload_started = time.monotonic()
        self._update_chunk_state(
            chunk_idx=item.chunk_idx,
            batch_id=item.batch_id,
            status="UPLOAD",
            upload_started_monotonic=upload_started,
        )
        self._set_active_recorder(item.recorder)
        # 042: binding independiente del lado UPLOAD para el tab del TUI.
        self._set_upload_active_recorder(item.recorder)
        try:
            s5_done, s5_failed = self._pipeline.upload_chunk(
                items=item.items,
                batch_id=item.batch_id,
                recorder=item.recorder,
            )
            self._pipeline._tracking_store.flush()  # noqa: SLF001
            self._pipeline._tracking_store.complete_batch(item.batch_id)  # noqa: SLF001
            elapsed = time.monotonic() - item.started_at
            upload_elapsed = time.monotonic() - upload_started
            total_docs = item.s1_done + item.skipped
            item.recorder.close_batch(
                pipeline=self._pipeline.pipeline_name,
                batch_id=item.batch_id,
                total_docs=total_docs,
                elapsed_s=elapsed,
            )
            self._update_chunk_state(
                chunk_idx=item.chunk_idx,
                batch_id=item.batch_id,
                status="DONE",
                s5_done=s5_done,
                s5_failed=s5_failed,
                upload_skipped=item.recorder.upload_skipped_count(),
                upload_elapsed_s=upload_elapsed,
            )
            with self._state_lock:
                if self._upload_active_recorder is item.recorder:
                    self._upload_active_recorder = None
            with results_lock:
                results.append(
                    RunReport(
                        batch_id=item.batch_id,
                        total_triggers=len(item.triggers),
                        total_docs=total_docs,
                        s1_done=item.s1_done,
                        s1_skipped_cross_batch=item.skipped,
                        s1_filtered=item.s1_filtered,
                        s2_done=len(item.items) + item.s2_failed,
                        s2_failed=item.s2_failed,
                        s3_done=len(item.items) + item.s3_failed,
                        s3_failed=item.s3_failed,
                        s4_done=len(item.items),
                        s4_failed=item.s4_failed,
                        s5_done=s5_done,
                        s5_failed=s5_failed,
                        elapsed_seconds=elapsed,
                    )
                )
        except BaseException as exc:  # noqa: BLE001 — registrado, la corrida continúa
            _log.exception(
                "multi-batch: upload failed",
                extra={
                    "batch_id": item.batch_id,
                    "chunk_idx": item.chunk_idx,
                    "reason": type(exc).__name__,
                },
            )
            self._update_chunk_state(
                chunk_idx=item.chunk_idx,
                batch_id=item.batch_id,
                status="FAILED",
            )
            with self._state_lock:
                if self._upload_active_recorder is item.recorder:
                    self._upload_active_recorder = None
            with results_lock:
                failed.append((item.batch_id, type(exc).__name__))

    # ----- Path N=2 ---------------------------------------------------

    def _run_overlapped(
        self,
        *,
        source_descriptor: str,
        batch_size: int,
        total: int | None = None,
    ) -> MultiBatchRunReport:
        # 050: hace `streaming` del iterador de triggers — nunca
        # materializa ni el conjunto completo de triggers ni la lista
        # completa de `chunk`s. El pico de memoria en vuelo es
        # O(batch_size × batches_in_flight), no O(total triggers).
        triggers: Iterator[Trigger] = self._pipeline._trigger_strategy.acquire(  # noqa: SLF001
            source_descriptor
        )
        if total is not None:
            triggers = itertools.islice(triggers, max(0, total))
        chunks_iter = chunked(triggers, batch_size)

        upload_queue: queue.Queue[object] = queue.Queue(maxsize=2)
        results: list[RunReport] = []
        failed: list[tuple[str, str]] = []
        results_lock = threading.Lock()

        def _prep_loop() -> None:
            # 050: trae `chunk`s de manera `lazy`; _prep_one_chunk siembra
            # el estado de cada `chunk` apenas se lo trae. Un iterador
            # vacío simplemente corre cero iteraciones → un
            # MultiBatchRunReport vacío.
            # 122: paridad con _run_sequential — al cancelar, el
            # productor deja de traer chunks nuevos (pre-122 seguía
            # pagando el scaffolding por chunk aunque los per-doc
            # abortaran). ``getattr`` porque los dobles de test del
            # pipeline no definen cancel_token (mismo patrón que :446).
            cancel_token = getattr(self._pipeline, "cancel_token", None)
            for idx, chunk in enumerate(chunks_iter):
                if cancel_token is not None and not cancel_token.checkpoint():
                    break
                prepared = self._prep_one_chunk(
                    idx, chunk, failed=failed, results_lock=results_lock
                )
                if prepared is not None:
                    upload_queue.put(prepared)
            upload_queue.put(_PREP_DONE)

        def _upload_loop() -> None:
            controller = self._pipeline.auto_tune_controller
            try:
                if controller is not None:
                    # 043: en modo multi-batch el recorder propio del
                    # `pipeline` nunca se escribe (cada `chunk` tiene el
                    # suyo), así que el p95_provider default del
                    # controller siempre lee cero. Lo apuntamos al
                    # recorder upload-active antes del start().
                    controller.set_p95_provider(self._upload_p95_observer)
                    controller.start()
                while True:
                    item = upload_queue.get()
                    if item is _PREP_DONE:
                        return
                    assert isinstance(item, _PreparedChunk)
                    self._upload_one_chunk(
                        item, results=results, failed=failed, results_lock=results_lock
                    )
            finally:
                if controller is not None:
                    controller.stop(timeout=2.0)

        sampler = self._pipeline.sampler
        if sampler is not None:
            sampler.start()
        try:
            prep_thread = threading.Thread(
                target=_prep_loop, name="cmcourier-multi-prep", daemon=False
            )
            upload_thread = threading.Thread(
                target=_upload_loop, name="cmcourier-multi-upload", daemon=False
            )
            prep_thread.start()
            upload_thread.start()
            prep_thread.join()
            upload_thread.join()
        finally:
            if sampler is not None:
                sampler.stop()

        # Ordena los resultados por el tiempo de inicio del `chunk` para
        # que el `stream` de salida sea estable.
        results.sort(key=lambda r: r.batch_id)
        return MultiBatchRunReport(chunks=results, failed_chunks=failed)

    # ----- internos ---------------------------------------------------

    def _build_chunk_recorder(self) -> MetricsRecorder:
        cfg = self._config.observability
        return MetricsRecorder(
            log_dir=self._log_dir,
            slow_op_threshold_ms=float(cfg.slow_op_threshold_ms),
            slow_op_top_n=cfg.slow_op_top_n,
            enabled=cfg.enabled,
            pipeline_metrics_enabled=cfg.pipeline_metrics,
        )
