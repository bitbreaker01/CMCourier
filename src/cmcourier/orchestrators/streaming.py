"""Orchestrator de `streaming` (063 — POST-MVP §13).

Coexiste con :class:`MultiBatchOrchestrator`. Se selecciona con
``processing.mode == "streaming"`` en el YAML del operador.

Forma:

* **Un** batch_id lógico para toda la corrida.
* **Un** :class:`MetricsRecorder` para la corrida — AIMD lee
  ``current_stage_p95_with_count("S5")`` de este único recorder.
* Una :class:`queue.Queue` acotada (el *`bucket`*) se ubica entre
  PREP (`producer`s S1-S4) y UPLOAD (`consumer`s S5). Los
  `producer`s empujan items preparados dentro del `bucket`; los
  `consumer`s los sacan. ``bucket.put`` bloquea cuando el `bucket`
  está lleno → `back-pressure` automática sobre PREP.
  ``bucket.get`` bloquea cuando el `bucket` está vacío → los
  `consumer`s quedan idle en un `futex`, no en un `spinloop`.
* Los `producer`s (``processing.prep_workers``) traen triggers de
  un iterador compartido y protegido por `lock` sobre la fuente
  de triggers. Cuando el iterador se agota, el `producer` que lo
  observa empuja ``N`` `poison pill`s (una por cada `consumer`)
  al `bucket` y sale.
* Los `consumer`s (dimensionados a ``_pool_ceiling()``, igual que
  el pool S5 del path batched — spec 057) llaman al
  ``streaming_upload_one`` existente. Un `consumer` que saca una
  `poison pill` sale.

Resultado: el pico de memoria colapsa a ``bucket_size``
(independiente del conteo total de triggers); S5 nunca espera a
que termine el PREP de un `chunk`; PREP nunca se bloquea en un
slot de `chunk` en vuelo.

El resume queda rechazado en modo `streaming` (``from_stage > 1``
o ``resume_batch_id`` no-None → ``ValueError``). La
`idempotency` cross-batch (062, filas ``S1_SKIPPED``) da
trazabilidad para docs ya subidos en corridas previas.
"""

from __future__ import annotations

__all__ = ["StreamingOrchestrator", "StreamingSnapshot", "_TriggerIter"]

import itertools
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cmcourier.config.schema import PipelineConfig
from cmcourier.domain.models import Trigger
from cmcourier.observability.metrics import MetricsRecorder
from cmcourier.orchestrators.multi_batch import ChunkState, MultiBatchRunReport
from cmcourier.orchestrators.staged import RunReport, StagedPipeline, _StageItem
from cmcourier.services.cancellation import CancellationToken
from cmcourier.services.lane_controller import Lane, LaneController, LaneSnapshot
from cmcourier.services.reconciler import stop_reconciler_visibly

_log = logging.getLogger(__name__)

_POISON: object = object()


def _record_crash(
    recorder: Callable[[Any, str, BaseException], None],
    subject: Any,
    batch_id: str,
    exc: BaseException,
) -> None:
    """148 REQ-004: deja la fila del crash sin poder matar al worker.

    El registro del censo es la RED, no la corrida: si el tracking está
    caído, el `worker` sigue drenando su cola. Al revés —un crash del
    censo tumbando el `pipeline`— sería cambiar un documento perdido por
    todos los que quedaban.
    """
    try:
        recorder(subject, batch_id, exc)
    except Exception:  # noqa: BLE001 — S6 nunca bloquea el pipeline
        _log.exception("streaming: could not record crash", extra={"batch_id": batch_id})


class _TriggerIter:
    """Wrapper thread-safe sobre un único iterador de triggers.

    Todos los `producer`s comparten una instancia. ``next()`` está
    protegido por un ``threading.Lock`` para que cada trigger se
    entregue a exactamente un `producer`. Se levanta
    ``StopIteration`` al agotarse (contrato estándar de iterador —
    el `producer` que lo observa es responsable del shutdown
    `fan-out`).
    """

    __slots__ = ("_inner", "_lock", "_count")

    def __init__(self, inner: Iterator[Trigger]) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._count = 0

    def __iter__(self) -> _TriggerIter:
        return self

    def __next__(self) -> Trigger:
        with self._lock:
            value = next(self._inner)
            self._count += 1
            return value

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


@dataclass(slots=True)
class _StreamingTally:
    """Counters mutables por corrida que son propiedad de `consumer`s + `producer`s."""

    s5_done: int = 0
    s5_failed: int = 0
    s5_skipped: int = 0
    s1_filtered: int = 0
    prep_failed: int = 0
    cross_batch_skipped: int = 0


@dataclass(frozen=True, slots=True)
class StreamingSnapshot:
    """064 — snapshot de sólo lectura del estado del modo `streaming` para el TUI.

    065 agrega ``lane_snapshot`` para la operación dual heavy/light;
    ``None`` en modo single-lane.
    """

    bucket_level: int
    bucket_cap: int
    bucket_peak: int
    prep_workers: int
    prep_in_flight: int
    upload_workers: int
    prep_docs_per_s: float
    upload_docs_per_s: float
    lane_snapshot: LaneSnapshot | None = None


class _ThroughputWindow:
    """Estimador de `throughput` por ventana deslizante (064).

    Registra el timestamp monotónico de cada evento en un deque y
    devuelve ``count / window_s`` para los eventos más nuevos que el
    cut-off. Thread-safe vía un `lock` interno.
    """

    __slots__ = ("_events", "_lock", "_window_s")

    def __init__(self, window_s: float = 5.0) -> None:
        from collections import deque

        self._events: deque[float] = deque()
        self._lock = threading.Lock()
        self._window_s = window_s

    def record(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._events.append(now)
            cutoff = now - self._window_s
            while self._events and self._events[0] < cutoff:
                self._events.popleft()

    def rate(self) -> float:
        cutoff = time.monotonic() - self._window_s
        with self._lock:
            while self._events and self._events[0] < cutoff:
                self._events.popleft()
            return len(self._events) / self._window_s if self._events else 0.0


class StreamingOrchestrator:
    """`Pipeline` continuo producer-consumer (063).

    Expone la misma forma de ``.run(...)`` que
    :class:`MultiBatchOrchestrator` para paridad con el CLI.
    Devuelve un :class:`MultiBatchRunReport` que lleva un único
    :class:`RunReport` sintético resumiendo toda la corrida.
    """

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
        self._bucket_size = max(1, int(config.processing.streaming.bucket_size))
        self._prep_workers = max(1, int(config.processing.prep_workers))
        self._consumer_count = max(1, int(pipeline._pool_ceiling()))  # noqa: SLF001
        self._state_lock = threading.Lock()
        self._chunk_state: ChunkState | None = None
        self._recorder: MetricsRecorder | None = None
        self._bucket: queue.Queue[_StageItem | object] | None = None
        self._peak_qsize = 0
        # 064: counters de observabilidad en vivo para el tab BUCKET.
        self._prep_in_flight = 0
        self._prep_in_flight_lock = threading.Lock()
        self._prep_window = _ThroughputWindow(window_s=5.0)
        self._upload_window = _ThroughputWindow(window_s=5.0)
        # 067: refs a las `queue`s por-`lane` (sólo se populan cuando
        # las `lane`s están habilitadas). Las usa
        # ``_publish_pending_count`` para computar el trabajo
        # pendiente en vivo + el dispatcher/consumers para reportar
        # el qsize real de vuelta a pool_stats y al lane controller.
        self._heavy_queue: queue.Queue[_StageItem | object] | None = None
        self._light_queue: queue.Queue[_StageItem | object] | None = None
        # 144: píldoras `_POISON` encoladas y todavía no retiradas. Se
        # descuentan del pending publicado — pre-144 ``qsize()`` las
        # contaba y el monitor mostraba ``30 pending`` tras el último doc.
        self._pills_pending = 0
        self._pills_lock = threading.Lock()
        # 065: `lane`s heavy/light. ``None`` mantiene el path
        # single-lane de 063 byte-idéntico.
        # 070: el LaneController es propiedad del StagedPipeline
        # (wiring de 036). Pre-070 el orchestrator construía su
        # PROPIA instancia de LaneController, lo que dejaba el
        # binding lane_snapshot del TUIDataProvider (que lee
        # pipeline.lane_controller) reportando ceros para siempre —
        # y rompía silenciosamente el steering del budget por-`lane`
        # de AIMD. Reusar la instancia del `pipeline` unifica la
        # fuente de datos.
        self._lanes_config = config.processing.heavy_light_lanes

    # ------------------------------------------- Hooks de binding del TUI (063)

    def chunks_snapshot(self) -> list[ChunkState]:
        """Vista de `chunk` sintético único de la corrida.

        El tab CHUNKS degrada con elegancia en modo `streaming` — la
        spec 064 lo reemplaza con un tab BUCKET real.
        """
        with self._state_lock:
            return [self._chunk_state] if self._chunk_state is not None else []

    def active_recorder(self) -> MetricsRecorder | None:
        with self._state_lock:
            return self._recorder

    def upload_recorder(self) -> MetricsRecorder | None:
        with self._state_lock:
            return self._recorder

    @property
    def cancel_token(self) -> CancellationToken:
        """097: token de cancelación, delegado al StagedPipeline interno."""
        return self._pipeline.cancel_token

    @property
    def bucket_size(self) -> int:
        return self._bucket_size

    @property
    def peak_qsize(self) -> int:
        return self._peak_qsize

    # ------------------------------------------- 064 accessors del tab BUCKET

    def bucket_level(self) -> int:
        """Ocupación actual aproximada del `bucket` (0 fuera de una corrida)."""
        bucket = self._bucket
        return int(bucket.qsize()) if bucket is not None else 0

    def prep_in_flight(self) -> int:
        with self._prep_in_flight_lock:
            return self._prep_in_flight

    def streaming_snapshot(self) -> StreamingSnapshot:
        """Lectura única de cada campo del tab BUCKET."""
        return StreamingSnapshot(
            bucket_level=self.bucket_level(),
            bucket_cap=self._bucket_size,
            bucket_peak=self._peak_qsize,
            prep_workers=self._prep_workers,
            prep_in_flight=self.prep_in_flight(),
            upload_workers=self._consumer_count,
            prep_docs_per_s=self._prep_window.rate(),
            upload_docs_per_s=self._upload_window.rate(),
            lane_snapshot=(
                self._lane_controller.snapshot() if self._lane_controller is not None else None
            ),
        )

    @property
    def lane_controller(self) -> LaneController | None:
        """065 + 070: handle de sólo lectura para el TUI / tests.
        ``None`` en modo single-lane. 070: forwardea a la instancia
        del `pipeline` para que haya exactamente un LaneController
        por corrida — ver spec 070 para el bug que esto soluciona
        (la `queue` LANES del tab UPLOAD trabada en 0)."""
        return self._pipeline.lane_controller

    @property
    def _lane_controller(self) -> LaneController | None:
        """070: alias interno de lectura `read-through`. El
        orchestrator escribía ``self._lane_controller`` por todos
        lados pre-070; la property mantiene esos call sites sin
        cambios mientras rutea las lecturas a la instancia del
        `pipeline`."""
        return self._pipeline.lane_controller

    # ----------------------------------------------------- 067 bindings vivos del TUI

    def _publish_pending_count(self) -> None:
        """067: reporta el total de pendientes en vivo (`bucket`
        principal + `queue`s por-`lane`) a los ``pool_stats`` del
        `pipeline`. Esto alimenta ``snap.queue_depth`` en el
        snapshot del tab UPLOAD — sin esto, ``target = count + 0``
        y la barra de progreso muestra ``count/count`` para siempre."""
        bucket = self._bucket
        if bucket is None:
            return
        total = int(bucket.qsize())
        if self._heavy_queue is not None:
            total += int(self._heavy_queue.qsize())
        if self._light_queue is not None:
            total += int(self._light_queue.qsize())
        # 144: las píldoras no son trabajo pendiente.
        with self._pills_lock:
            total -= self._pills_pending
        self._pipeline.pool_stats.set_queue_depth(max(0, total))

    def _put_pill(self, target: queue.Queue[_StageItem | object]) -> None:
        """144: encola una ``_POISON`` contándola ANTES del ``put`` — si
        un consumer la retira antes de que actualicemos el contador, el
        pending publicado nunca queda negativo ni la cuenta dos veces."""
        with self._pills_lock:
            self._pills_pending += 1
        target.put(_POISON)

    def _pop_pill(self) -> None:
        """144: un consumer/dispatcher retiró una píldora → republica el
        pending para que el monitor baje a ``0 pending`` con el último doc."""
        with self._pills_lock:
            self._pills_pending = max(0, self._pills_pending - 1)
        self._publish_pending_count()

    def _publish_chunk_state(
        self,
        *,
        batch_id: str,
        tally: _StreamingTally,
        tally_lock: threading.Lock,
    ) -> None:
        """067: escribe los counters en vivo dentro del ChunkState
        sintético para que el tab CHUNKS + el timer/avg-speed del tab
        UPLOAD reflejen el trabajo en progreso. Llamado luego de
        cada resultado de S5 por ambos loops de `consumer`."""
        with tally_lock:
            s5d = tally.s5_done
            s5f = tally.s5_failed
            s5sk = tally.s5_skipped
            fil = tally.s1_filtered
            csk = tally.cross_batch_skipped
        completed = s5d + s5f + s5sk
        with self._state_lock:
            prev = self._chunk_state
            self._chunk_state = ChunkState(
                chunk_idx=0,
                batch_id=batch_id,
                status="UPLOAD",
                s5_done=s5d,
                s5_failed=s5f,
                doc_count=completed + fil + csk,
                prep_done=completed,
                prep_skipped=csk,
                prep_filtered=fil,
                upload_skipped=s5sk,
                prep_started_monotonic=(prev.prep_started_monotonic if prev is not None else None),
                upload_started_monotonic=(
                    prev.upload_started_monotonic if prev is not None else None
                ),
            )

    # ----------------------------------------------------------- API pública

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
        """Maneja el `pipeline` `streaming` end-to-end.

        ``batch_size`` y ``batches_in_flight`` se aceptan por paridad
        con el CLI pero se **ignoran** — el `streaming` usa el
        ``bucket_size`` configurado como su única perilla de control
        de memoria. ``from_stage > 1`` o ``resume_batch_id`` no-None
        levanta ``ValueError``; resume en modo `streaming` = re-run
        + las filas ``S1_SKIPPED`` de 062.
        """
        if from_stage > 1:
            raise ValueError(
                "streaming mode does not support --from-stage > 1; "
                "re-run with --from-stage 1 (cross-batch idempotency "
                "produces S1_SKIPPED rows for already-uploaded docs)"
            )
        if resume_batch_id is not None:
            raise ValueError(
                "streaming mode does not support --batch-id (resume); "
                "each run uses a fresh batch_id"
            )

        # 150 REQ-002: la lista de activos se verifica antes de adquirir el
        # primer trigger. Si está rota, la corrida no arranca. ``getattr``
        # defensivo — paridad con el patrón del reconciler para los dobles de
        # test del pipeline.
        preflight = getattr(self._pipeline, "preflight", None)
        if preflight is not None:
            preflight()

        triggers: Iterator[Trigger] = self._pipeline._trigger_strategy.acquire(  # noqa: SLF001
            source_descriptor
        )
        if total is not None:
            triggers = itertools.islice(triggers, max(0, total))
        trigger_iter = _TriggerIter(triggers)

        batch_id = self._pipeline._tracking_store.start_batch(total_records=0)  # noqa: SLF001
        record_audit = getattr(self._pipeline, "record_eligibility_audit", None)
        if record_audit is not None:
            record_audit(batch_id)
        recorder = self._build_run_recorder()
        recorder.start_batch(pipeline=self._pipeline.pipeline_name, batch_id=batch_id)
        bucket: queue.Queue[_StageItem | object] = queue.Queue(maxsize=self._bucket_size)
        self._bucket = bucket
        self._peak_qsize = 0
        with self._pills_lock:
            self._pills_pending = 0
        tally = _StreamingTally()
        tally_lock = threading.Lock()

        start = time.monotonic()
        # 067: siembra el chunk_state sintético en estado "UPLOAD"
        # con ambos stamps monotónicos seteados al inicio de la
        # corrida. En modo `streaming` PREP y UPLOAD corren
        # simultáneamente, así que "UPLOAD" es la fase dominante
        # para el binding de timer + avg-speed del tab UPLOAD.
        # Pre-067 esto era ``status="PREP"`` durante toda la
        # corrida, lo que hacía que el helper
        # ``_current_chunk_progress`` devolviera elapsed_s=0 para
        # siempre.
        with self._state_lock:
            self._recorder = recorder
            self._chunk_state = ChunkState(
                chunk_idx=0,
                batch_id=batch_id,
                status="UPLOAD",
                upload_started_monotonic=start,
                prep_started_monotonic=start,
            )
        sampler = self._pipeline.sampler
        controller = self._pipeline.auto_tune_controller
        if sampler is not None:
            sampler.start()
        if controller is not None:

            def _p95_provider() -> tuple[float, int]:
                return recorder.current_stage_p95_with_count("S5")

            controller.set_p95_provider(_p95_provider)
            controller.start()
        # 096: el reconciliador periódico AS400 corre durante toda la
        # corrida streaming; su pasada final está en stop(). getattr
        # defensivo — los dobles de test del pipeline no lo definen.
        periodic_recon = getattr(self._pipeline, "_periodic_reconciler", None)
        if periodic_recon is not None:
            periodic_recon.start()
        try:
            # 038: pre-abre el `connection pool` de S5 para que el
            # primer `batch` de uploads no pague el handshake
            # TCP+`TLS`+session en el critical path.
            self._pipeline.warm_upload_pool(self._consumer_count)

            producers = [
                threading.Thread(
                    target=self._prep_loop,
                    args=(trigger_iter, bucket, batch_id, recorder, tally, tally_lock),
                    name=f"cmcourier-stream-prep-{i}",
                    daemon=False,
                )
                for i in range(self._prep_workers)
            ]

            if self._lane_controller is not None:
                # 065: modo dual-lane. Dos `queue`s por-`lane` + un
                # `thread` dispatcher + `pool`s de `consumer` heavy
                # y light que comparten el budget total
                # ``_pool_ceiling()``.
                self._lane_controller.start()
                heavy_queue: queue.Queue[_StageItem | object] = queue.Queue(
                    maxsize=self._bucket_size
                )
                light_queue: queue.Queue[_StageItem | object] = queue.Queue(
                    maxsize=self._bucket_size
                )
                # 067: guarda las refs a las `queue`s para que
                # ``_publish_pending_count`` pueda leer su qsize en
                # vivo desde cualquier `thread`.
                self._heavy_queue = heavy_queue
                self._light_queue = light_queue
                heavy_consumers = [
                    threading.Thread(
                        target=self._lane_upload_loop,
                        args=(
                            heavy_queue,
                            "heavy",
                            batch_id,
                            recorder,
                            tally,
                            tally_lock,
                        ),
                        name=f"cmcourier-stream-upload-heavy-{i}",
                        daemon=False,
                    )
                    for i in range(self._consumer_count)
                ]
                light_consumers = [
                    threading.Thread(
                        target=self._lane_upload_loop,
                        args=(
                            light_queue,
                            "light",
                            batch_id,
                            recorder,
                            tally,
                            tally_lock,
                        ),
                        name=f"cmcourier-stream-upload-light-{i}",
                        daemon=False,
                    )
                    for i in range(self._consumer_count)
                ]
                dispatcher = threading.Thread(
                    target=self._dispatcher_loop,
                    args=(
                        bucket,
                        heavy_queue,
                        light_queue,
                        len(heavy_consumers),
                        len(light_consumers),
                    ),
                    name="cmcourier-stream-dispatch",
                    daemon=False,
                )

                for p in producers:
                    p.start()
                dispatcher.start()
                for c in heavy_consumers:
                    c.start()
                for c in light_consumers:
                    c.start()

                for p in producers:
                    p.join()
                # Señaliza fin de `stream` al dispatcher; éste
                # forwardea `_POISON` a ambas `queue`s de `lane` ×
                # cantidad de `consumer`s.
                self._put_pill(bucket)
                dispatcher.join()
                for c in heavy_consumers:
                    c.join()
                for c in light_consumers:
                    c.join()
            else:
                consumers = [
                    threading.Thread(
                        target=self._upload_loop,
                        args=(bucket, batch_id, recorder, tally, tally_lock),
                        name=f"cmcourier-stream-upload-{i}",
                        daemon=False,
                    )
                    for i in range(self._consumer_count)
                ]
                for p in producers:
                    p.start()
                for c in consumers:
                    c.start()

                for p in producers:
                    p.join()
                # Los `producer`s terminaron; aseguramos que los
                # `consumer`s reciban N `poison pill`s.
                for _ in range(self._consumer_count):
                    self._put_pill(bucket)
                for c in consumers:
                    c.join()
        finally:
            # 119: inofensivo en streaming (los consumers son threads
            # propios), pero el S4 vía prep y cualquier path compartido
            # del pipeline pudo haber creado pools persistentes.
            # ``getattr`` defensivo — paridad con el patrón del
            # reconciler para los fakes de test.
            shutdown_pools = getattr(self._pipeline, "shutdown_worker_pools", None)
            if shutdown_pools is not None:
                shutdown_pools()
            if periodic_recon is not None:
                # 144: la pasada final se ve en el monitor como fase de
                # cierre (``cerrando · sincronizando AS400 k/N``).
                stop_reconciler_visibly(periodic_recon, getattr(self._pipeline, "pool_stats", None))
            if self._lane_controller is not None:
                self._lane_controller.stop()
            if controller is not None:
                controller.stop(timeout=2.0)
            if sampler is not None:
                sampler.stop()
            # 067: libera las refs a las `queue`s de `lane` para que
            # una corrida siguiente no filtre las `queue`s de la
            # corrida previa dentro de ``_publish_pending_count``.
            self._heavy_queue = None
            self._light_queue = None

        elapsed = time.monotonic() - start
        self._pipeline._tracking_store.flush()  # noqa: SLF001
        self._pipeline._tracking_store.complete_batch(batch_id)  # noqa: SLF001

        with tally_lock:
            snapshot = _StreamingTally(
                s5_done=tally.s5_done,
                s5_failed=tally.s5_failed,
                s5_skipped=tally.s5_skipped,
                s1_filtered=tally.s1_filtered,
                prep_failed=tally.prep_failed,
                cross_batch_skipped=tally.cross_batch_skipped,
            )

        total_triggers = trigger_iter.count
        total_docs = snapshot.s5_done + snapshot.s5_failed + snapshot.s5_skipped
        recorder.close_batch(
            pipeline=self._pipeline.pipeline_name,
            batch_id=batch_id,
            total_docs=total_docs,
            elapsed_s=elapsed,
        )

        with self._state_lock:
            self._chunk_state = ChunkState(
                chunk_idx=0,
                batch_id=batch_id,
                status="DONE",
                s5_done=snapshot.s5_done,
                s5_failed=snapshot.s5_failed,
                doc_count=total_docs,
                prep_done=snapshot.s5_done + snapshot.s5_failed + snapshot.s5_skipped,
                prep_skipped=snapshot.cross_batch_skipped,
                prep_filtered=snapshot.s1_filtered,
                upload_skipped=snapshot.s5_skipped,
                upload_elapsed_s=elapsed,
            )

        report = RunReport(
            batch_id=batch_id,
            total_triggers=total_triggers,
            total_docs=total_docs,
            s1_done=total_docs,
            s1_skipped_cross_batch=snapshot.cross_batch_skipped,
            s1_filtered=snapshot.s1_filtered,
            s2_done=total_docs,
            s2_failed=0,
            s3_done=total_docs,
            s3_failed=0,
            s4_done=total_docs,
            s4_failed=0,
            s5_done=snapshot.s5_done,
            s5_failed=snapshot.s5_failed,
            elapsed_seconds=elapsed,
        )
        return MultiBatchRunReport(chunks=[report])

    # ------------------------------------------------------ `producer` / `consumer`

    def _prep_loop(
        self,
        trigger_iter: _TriggerIter,
        bucket: queue.Queue[_StageItem | object],
        batch_id: str,
        recorder: MetricsRecorder,
        tally: _StreamingTally,
        tally_lock: threading.Lock,
    ) -> None:
        # 097: cancelación cooperativa — getattr defensivo porque los
        # dobles de test del pipeline no definen cancel_token.
        cancel_token = getattr(self._pipeline, "cancel_token", None)
        while True:
            # El producer deja de tomar triggers nuevos; lo que ya está
            # en el bucket lo drena el consumer vía los chequeos per-doc.
            if cancel_token is not None and not cancel_token.checkpoint():
                return
            try:
                trigger = next(trigger_iter)
            except StopIteration:
                return
            with self._prep_in_flight_lock:
                self._prep_in_flight += 1
            try:
                try:
                    survivor, skipped, filtered = self._pipeline.streaming_prep_one(
                        trigger, batch_id, recorder
                    )
                except BaseException as exc:  # noqa: BLE001 — log + count, la corrida continúa
                    _log.exception(
                        "streaming: prep failed",
                        extra={"batch_id": batch_id, "reason": type(exc).__name__},
                    )
                    # 148 REQ-004: hasta acá esto sólo movía un contador en
                    # memoria — el documento se evaporaba del batch sin
                    # estado terminal y sin razón.
                    _record_crash(self._pipeline.record_prep_crash, trigger, batch_id, exc)
                    with tally_lock:
                        tally.prep_failed += 1
                    continue
                with tally_lock:
                    tally.cross_batch_skipped += skipped
                    tally.s1_filtered += filtered
                if survivor is None:
                    # filtrado / saltado cross-batch / fallado en
                    # S2-S4. Ya persistido por los helpers internos;
                    # los counters de arriba capturan el resultado
                    # para el RunReport sintético.
                    continue
                bucket.put(survivor)
                self._prep_window.record()
                current = bucket.qsize()
                # 122: read-modify-write bajo lock — N producers
                # concurrentes subestimaban el pico del tab BUCKET.
                with self._prep_in_flight_lock:
                    if current > self._peak_qsize:
                        self._peak_qsize = current
                # 067: reporta el conteo en vuelo en vivo a
                # pool_stats para que la barra de progreso del tab
                # UPLOAD muestre progreso real en lugar de
                # ``count/count``.
                self._publish_pending_count()
            finally:
                with self._prep_in_flight_lock:
                    self._prep_in_flight -= 1

    def _upload_loop(
        self,
        bucket: queue.Queue[_StageItem | object],
        batch_id: str,
        recorder: MetricsRecorder,
        tally: _StreamingTally,
        tally_lock: threading.Lock,
    ) -> None:
        while True:
            item = bucket.get()
            try:
                if item is _POISON:
                    self._pop_pill()  # 144: republica sin la píldora
                    return
                # 067: un `consumer` acaba de hacer pop → los pendientes bajan en 1.
                self._publish_pending_count()
                # ``bucket`` lleva _StageItem salvo el `sentinel` de poison.
                stage_item: _StageItem = item  # type: ignore[assignment]
                try:
                    outcome = self._pipeline.streaming_upload_one(stage_item, batch_id, recorder)
                except BaseException as exc:  # noqa: BLE001
                    _log.exception(
                        "streaming: upload crashed",
                        extra={"batch_id": batch_id, "reason": type(exc).__name__},
                    )
                    # 148 REQ-004: el bug de los ~200 uploads perdidos — sin
                    # esto el doc se queda en ``S4_DONE``, dado por preparado.
                    _record_crash(self._pipeline.record_upload_crash, stage_item, batch_id, exc)
                    with tally_lock:
                        tally.s5_failed += 1
                    self._publish_chunk_state(batch_id=batch_id, tally=tally, tally_lock=tally_lock)
                    continue
                with tally_lock:
                    if outcome == "done":
                        tally.s5_done += 1
                        recorder.record_upload_done()
                    elif outcome == "failed":
                        # 104: la falla ya quedó contabilizada (total + tipo
                        # + status) por ``_upload_one``.
                        tally.s5_failed += 1
                    elif outcome == "skipped":
                        tally.s5_skipped += 1
                        recorder.record_upload_skipped()
                self._upload_window.record()
                # 067: refresca los bindings vivos del tab CHUNKS +
                # tab UPLOAD.
                self._publish_chunk_state(batch_id=batch_id, tally=tally, tally_lock=tally_lock)
            finally:
                bucket.task_done()

    # ------------------------------------------------------ 065 dual-lane

    def _dispatcher_loop(
        self,
        bucket: queue.Queue[_StageItem | object],
        heavy_queue: queue.Queue[_StageItem | object],
        light_queue: queue.Queue[_StageItem | object],
        heavy_consumer_count: int,
        light_consumer_count: int,
    ) -> None:
        """065: rutea items preparados desde el `bucket` principal a
        `queue`s por-`lane` en función de ``staged_file.size_bytes``.

        Cuando llega ``_POISON`` desde el `bucket` principal:
        empuja N `poison pill`s a cada `queue` de `lane` (una por
        cada `consumer`) y sale.

        067: la `queue depth` que se reporta al LaneController es
        el ``lane_queue.qsize()`` en vivo — el counter monotónico
        pre-067 sólo subía, excedía ``bucket_size``, y rompía la
        heurística de rebalance dirigida por drenaje.
        """
        threshold = self._lanes_config.heavy_threshold_bytes
        # 115: overflow acotado por lane. El put directo es no-bloqueante
        # — pre-115 una cola heavy llena clavaba al dispatcher entero y
        # los consumers light quedaban ociosos con trabajo en el bucket
        # (head-of-line blocking entre lanes). El buffering extra está
        # acotado por ``bucket_size`` items por lane (metadatos, no
        # bytes de archivo).
        max_overflow = max(1, self._bucket_size)
        overflow: dict[Lane, deque[_StageItem]] = {"heavy": deque(), "light": deque()}
        queues: dict[Lane, queue.Queue[_StageItem | object]] = {
            "heavy": heavy_queue,
            "light": light_queue,
        }

        def report_depth(lane: Lane) -> None:
            if self._lane_controller is not None:
                self._lane_controller.set_queue_depth(lane, queues[lane].qsize())

        def flush_overflow() -> None:
            for lane, buf in overflow.items():
                moved = False
                while buf:
                    try:
                        queues[lane].put_nowait(buf[0])
                    except queue.Full:
                        break
                    buf.popleft()
                    moved = True
                if moved:
                    report_depth(lane)

        while True:
            flush_overflow()
            try:
                item = bucket.get(timeout=0.25)
            except queue.Empty:
                # Sin items nuevos: seguir drenando el overflow mientras
                # los consumers hacen lugar en las colas de lane.
                continue
            try:
                if item is _POISON:
                    # 115: entregar TODO el overflow antes de las pills
                    # (bloqueante está bien acá — los consumers siguen
                    # vivos drenando). Ningún item se pierde.
                    for lane, buf in overflow.items():
                        while buf:
                            queues[lane].put(buf.popleft())
                        report_depth(lane)
                    # 144: la píldora del bucket se retira y se fan-outea
                    # a las lanes — el contador sigue a cada una.
                    self._pop_pill()
                    for _ in range(heavy_consumer_count):
                        self._put_pill(heavy_queue)
                    for _ in range(light_consumer_count):
                        self._put_pill(light_queue)
                    return
                stage_item: _StageItem = item  # type: ignore[assignment]
                size_bytes = (
                    stage_item.staged_file.size_bytes if stage_item.staged_file is not None else 0
                )
                lane_name: Lane = "heavy" if size_bytes >= threshold else "light"
                overflow[lane_name].append(stage_item)
                flush_overflow()
                # Back-pressure acotada: recién cuando el overflow de la
                # lane rebalsa, el dispatcher espera — pero sigue
                # drenando el overflow de la OTRA lane en cada vuelta.
                while len(overflow[lane_name]) > max_overflow:
                    try:
                        queues[lane_name].put(overflow[lane_name][0], timeout=0.25)
                        overflow[lane_name].popleft()
                        report_depth(lane_name)
                    except queue.Full:
                        pass
                    flush_overflow()
                # 067: hace tick a pool_stats para que la barra del
                # tab UPLOAD avance.
                self._publish_pending_count()
            finally:
                bucket.task_done()

    def _lane_upload_loop(
        self,
        lane_queue: queue.Queue[_StageItem | object],
        lane: Lane,
        batch_id: str,
        recorder: MetricsRecorder,
        tally: _StreamingTally,
        tally_lock: threading.Lock,
    ) -> None:
        """065: `consumer` S5 por-`lane`. Adquiere el `semaphore` de
        `lane` vía ``streaming_upload_one(lane=...)`` para que el
        LaneController limite la concurrencia por-`lane`."""
        while True:
            item = lane_queue.get()
            try:
                if item is _POISON:
                    self._pop_pill()  # 144: republica sin la píldora
                    return
                # 067: el `consumer` acaba de hacer pop → reporta el
                # qsize en vivo para que la heurística de drenaje
                # del LaneController vea el decremento y los tabs
                # BUCKET/UPLOAD reflejen la ocupación en vivo de la
                # `lane`.
                if self._lane_controller is not None:
                    self._lane_controller.set_queue_depth(lane, lane_queue.qsize())
                self._publish_pending_count()
                stage_item: _StageItem = item  # type: ignore[assignment]
                try:
                    outcome = self._pipeline.streaming_upload_one(
                        stage_item, batch_id, recorder, lane=lane
                    )
                except BaseException as exc:  # noqa: BLE001
                    _log.exception(
                        "streaming: upload crashed",
                        extra={
                            "batch_id": batch_id,
                            "lane": lane,
                            "reason": type(exc).__name__,
                        },
                    )
                    # 148 REQ-004: mismo arreglo que en ``_upload_loop`` — con
                    # lanes puestas el handler es OTRO bloque de código.
                    _record_crash(self._pipeline.record_upload_crash, stage_item, batch_id, exc)
                    with tally_lock:
                        tally.s5_failed += 1
                    self._publish_chunk_state(batch_id=batch_id, tally=tally, tally_lock=tally_lock)
                    continue
                with tally_lock:
                    if outcome == "done":
                        tally.s5_done += 1
                        recorder.record_upload_done()
                    elif outcome == "failed":
                        # 104: la falla ya quedó contabilizada (total + tipo
                        # + status) por ``_upload_one``.
                        tally.s5_failed += 1
                    elif outcome == "skipped":
                        tally.s5_skipped += 1
                        recorder.record_upload_skipped()
                self._upload_window.record()
                self._publish_chunk_state(batch_id=batch_id, tally=tally, tally_lock=tally_lock)
            finally:
                lane_queue.task_done()

    # ------------------------------------------------------ internos

    def _build_run_recorder(self) -> MetricsRecorder:
        cfg = self._config.observability
        return MetricsRecorder(
            log_dir=self._log_dir,
            slow_op_threshold_ms=float(cfg.slow_op_threshold_ms),
            slow_op_top_n=cfg.slow_op_top_n,
            enabled=cfg.enabled,
            pipeline_metrics_enabled=cfg.pipeline_metrics,
        )
