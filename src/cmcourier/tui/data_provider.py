"""Adaptador de `snapshot` de sólo lectura para el TUI (025 fase 3).

El TUI corre en su propio `thread`; la pipeline corre en el `thread`
principal. Nunca comparten estado mutable directamente. En cambio,
cada ~250 ms el TUI llama a :meth:`TUIDataProvider.snapshot`, que
construye un :class:`TUISnapshot` inmutable a partir del estado
live de:

* :class:`MetricsRecorder` — timings por `stage` + `sampler` de
  `bandwidth` + agregador de `slow-op`.
* :class:`WorkerPoolStats` — capacity/busy/queue del `pool`.
* :class:`AutoTuneController` (opcional) — última decisión AIMD +
  countdown.
* :class:`CmisConfigModel` + :class:`CmisUploader` — `endpoint`,
  techo de `bandwidth`, `timeout` live de request.

El provider esconde a propósito todos los handles mutables para
que el TUI no pueda mutar el estado de orquestación por accidente.
"""

from __future__ import annotations

__all__ = ["TUIDataProvider", "TUISnapshot"]

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from cmcourier.adapters.upload.cmis_uploader import CmisUploader
from cmcourier.config.schema import CmisConfigModel
from cmcourier.domain.models import DocDetail
from cmcourier.domain.ports import ITrackingStore
from cmcourier.observability.metrics import MetricsRecorder
from cmcourier.observability.network_info import detect_link_speed_mbps
from cmcourier.orchestrators.streaming import StreamingSnapshot
from cmcourier.services.auto_tune import AutoTuneController
from cmcourier.services.lane_controller import LaneController, LaneSnapshot
from cmcourier.services.worker_pool_stats import (
    ClosingPhase,
    ResizableSemaphore,
    WorkerPoolStats,
    WorkerPoolStatsSnapshot,
)

# Stages que se muestran en el tab PREP. S5 vive en UPLOAD.
PREP_STAGES: tuple[str, ...] = ("S0", "S1", "S2", "S3", "S4")
UPLOAD_STAGE: str = "S5"


@dataclass(frozen=True, slots=True)
class TUISnapshot:
    """Vista inmutable de todos los campos que el TUI necesita en un instante."""

    # ---------- header
    pipeline: str
    batch_id: str
    elapsed_s: float
    throughput_docs_per_s: float
    is_complete: bool

    # ---------- per-stage (S0..S5)
    stages: dict[str, dict[str, float | int]] = field(default_factory=dict)

    # ---------- workers
    pool_capacity: int = 0
    pool_in_use: int = 0
    pool_idle: int = 0
    queue_depth: int = 0

    # ---------- auto-tune
    auto_tune_enabled: bool = False
    auto_tune_target_p95_ms: float = 0.0
    auto_tune_observed_p95_ms: float = 0.0
    auto_tune_adjust_interval_s: int = 0
    auto_tune_next_in_s: float = 0.0
    auto_tune_timeout_s: float = 0.0
    auto_tune_timeout_min_s: int = 0
    auto_tune_timeout_max_s: int = 0
    auto_tune_last_action: str = "—"
    auto_tune_last_workers_after: int = 0
    auto_tune_seconds_since_last_decision: float | None = None

    # ---------- network (CMIS)
    cmis_endpoint: str = ""
    bandwidth_current_mbps: float = 0.0
    bandwidth_peak_mbps: float = 0.0
    bandwidth_ceiling_mbps: float = 0.0  # 0 == auto-escala
    bandwidth_series: tuple[tuple[int, float], ...] = ()

    # ---------- slow ops + uploads recientes
    slow_ops_all: tuple[dict[str, object], ...] = ()

    # ---------- 104: desglose de la tasa de error de S5 por tipo + status
    failed_total: int = 0
    failures_by_type: dict[str, int] = field(default_factory=dict)
    failures_by_status: dict[int, int] = field(default_factory=dict)

    # ---------- 030: estado de `chunk`s (vista multi-batch)
    chunks_state: tuple[dict[str, object], ...] = ()

    # ---------- 036: estado de `lane`s heavy/light (None en modo single-lane)
    lane_snapshot: LaneSnapshot | None = None

    # ---------- 041: progreso UPLOAD por `chunk` (bytes + timer + ETA)
    # En modo single-batch (sin chunks_state) caen al contador
    # acumulativo del recorder y al elapsed global de la corrida.
    current_chunk_bytes_uploaded: int = 0
    current_chunk_bytes_total: int = 0
    current_chunk_elapsed_s: float = 0.0
    current_chunk_avg_mbps: float = 0.0
    current_chunk_eta_s: float | None = None

    # ---------- 051: docs filtrados en S1 (filas RVABREP con código de
    # baja), sumados entre todos los estados de `chunk`. 0 en el path
    # monolítico raro.
    s1_filtered: int = 0

    # ---------- 064: modo de orquestación (decide el tab BUCKET vs CHUNKS)
    mode: Literal["batched", "streaming"] = "batched"

    # ---------- 134: tasa por ventana deslizante + ETA de corrida.
    # ``docs_processed`` son resultados terminales (subido, fallido,
    # salteado, filtrado). La ventana es None hasta tener dos muestras
    # separadas ≥ 1 s; la ETA sólo existe con ``planned_total`` (el
    # `total` de [5] / --total): sin él la fuente puede tener 20M filas.
    docs_processed: int = 0
    throughput_window_docs_per_s: float | None = None
    planned_total: int | None = None
    eta_run_s: float | None = None

    # ---------- 064: snapshot del tab BUCKET en `streaming` (None en `batched`)
    bucket: StreamingSnapshot | None = None

    # ---------- 144: fase de cierre (pasada final del reconciler). None fuera
    # del cierre; mientras exista, el monitor muestra "cerrando · label k/N"
    # en lugar de "corriendo".
    closing: ClosingPhase | None = None


class TUIDataProvider:
    """Factory de `snapshot`s que el TUI consulta en cada tick de refresh.

    Todas las llamadas de acceso golpean las APIs de `snapshot` de
    ``MetricsRecorder`` / ``WorkerPoolStats``, que son thread-safe por
    construcción (Fases 1+2).
    """

    def __init__(
        self,
        *,
        pipeline_name: str,
        metrics_recorder: MetricsRecorder,
        pool_stats: WorkerPoolStats,
        concurrency_limit: ResizableSemaphore,
        cmis_config: CmisConfigModel,
        uploader: CmisUploader,
        auto_tune: AutoTuneController | None = None,
        recorder_provider: Callable[[], MetricsRecorder | None] | None = None,
        upload_recorder_provider: Callable[[], MetricsRecorder | None] | None = None,
        chunks_provider: Callable[[], list[Any]] | None = None,
        lane_controller: LaneController | None = None,
        tracking_store: ITrackingStore | None = None,
        mode: Literal["batched", "streaming"] = "batched",
        bucket_provider: Callable[[], StreamingSnapshot | None] | None = None,
        planned_total: int | None = None,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pipeline_name = pipeline_name
        # 134: ventana deslizante de (t, procesados). Se alimenta en cada
        # ``snapshot()``; con [6] en otra pestaña hay huecos, pero la tasa
        # es Δdocs/Δt entre la muestra más vieja dentro de la ventana y la
        # última, así que sigue siendo real.
        self._planned_total = planned_total if planned_total is None else max(0, int(planned_total))
        self._window_s = float(window_s)
        self._clock = clock
        self._window: deque[tuple[float, int]] = deque()
        self._fallback_recorder = metrics_recorder
        # 030: cuando el orchestrator multi-batch maneja la corrida, el
        # provider sigue apuntando al recorder del `chunk` activo. Para
        # corridas single-batch se usa el fallback (== el recorder propio
        # de la pipeline).
        self._recorder_provider: Callable[[], MetricsRecorder | None] | None = recorder_provider
        # 042: binding independiente del tab UPLOAD. Cuando está seteado,
        # se usa para todo lo S5-shaped (bytes subidos, percentiles de S5,
        # contadores live done/failed). Cae a ``recorder_provider`` cuando
        # no está seteado (p.ej. single-batch sin el slot dual expuesto).
        self._upload_recorder_provider: Callable[[], MetricsRecorder | None] | None = (
            upload_recorder_provider
        )
        self._chunks_provider: Callable[[], list[Any]] | None = chunks_provider
        # 052: `tracking store` para el drill-down por `chunk` (panel DETAIL).
        self._tracking_store = tracking_store
        self._pool_stats = pool_stats
        self._concurrency_limit = concurrency_limit
        self._cmis_config = cmis_config
        # 079: cachear el ceiling del chart de bandwidth UNA VEZ al
        # construir. Prioridad: throttle configurado (Mbps) > velocidad
        # detectada de la NIC física más rápida (Mbps) > auto-scale al
        # peak. El chart muestra MB/s para coincidir con el sampler, así
        # que ambas fuentes (config + NIC) se dividen por 8 para
        # convertir Mbps → MB/s.
        # 081: ``max_bandwidth_mbps`` ahora se interpreta como Mbps reales
        # (megabits/s), igual que ``detect_link_speed_mbps()``. Pre-081
        # el config se asumía como MB/s; ese mismatch resultaba en un
        # ceiling 8x sobreestimado en el chart cuando había throttle.
        configured_mbps = float(cmis_config.max_bandwidth_mbps)
        if configured_mbps > 0:
            self._bandwidth_ceiling_mbps = configured_mbps / 8.0
        else:
            link_mbps = detect_link_speed_mbps()
            self._bandwidth_ceiling_mbps = link_mbps / 8.0 if link_mbps > 0 else 0.0
        self._uploader = uploader
        self._auto_tune = auto_tune
        self._lane_controller = lane_controller
        self._batch_id: str = ""
        self._batch_started_monotonic: float | None = None
        # 052: se sella al completarse para que el timer de la corrida
        # CONGELE en vez de seguir ticando después del último `chunk`.
        self._batch_completed_monotonic: float | None = None
        self._is_complete = False
        # 064: modo de orquestación + data source del tab BUCKET.
        self._mode: Literal["batched", "streaming"] = mode
        self._bucket_provider: Callable[[], StreamingSnapshot | None] | None = bucket_provider

    @property
    def mode(self) -> Literal["batched", "streaming"]:
        """064: ``"streaming"`` activa el tab BUCKET + oculta CHUNKS."""
        return self._mode

    @property
    def _metrics(self) -> MetricsRecorder:
        """Recorder activo live-bound; cae al construido por defecto."""
        if self._recorder_provider is not None:
            live = self._recorder_provider()
            if live is not None:
                return live
        return self._fallback_recorder

    @property
    def _upload_metrics(self) -> MetricsRecorder:
        """042 — recorder del lado UPLOAD; aislado de los flips del lado PREP.

        Cae a ``self._metrics`` cuando no hay provider del lado upload
        cableado (corridas single-batch) para que el dashboard siga
        funcionando sin cambios.
        """
        if self._upload_recorder_provider is not None:
            live = self._upload_recorder_provider()
            if live is not None:
                return live
        return self._metrics

    # ------------------------------------------------------- hooks de ciclo de vida

    def mark_batch_started(self, batch_id: str) -> None:
        self._batch_id = batch_id
        self._batch_started_monotonic = self._clock()
        self._batch_completed_monotonic = None
        self._is_complete = False

    def mark_batch_complete(self) -> None:
        self._is_complete = True
        # 052: congela el reloj de la corrida al completarse.
        self._batch_completed_monotonic = self._clock()

    # ------------------------------------------------------- drill-down (052)

    def docs_for_batch(self, batch_id: str) -> list[DocDetail]:
        """Detalle por-doc del batch de un `chunk` — para el panel DETAIL.

        Lee del `tracking store` bajo demanda (memoria acotada). Devuelve
        una lista vacía cuando no hay store cableado o ``batch_id`` está
        en blanco.
        """
        if self._tracking_store is None or not batch_id:
            return []
        return self._tracking_store.list_docs_for_batch(batch_id)

    # ------------------------------------------------------- snapshot

    def snapshot(self) -> TUISnapshot:
        stages = self._metrics.stages_snapshot()
        # 042: sobrescribe la entrada S5 con la data del recorder del lado
        # upload para que el bloque de percentiles del tab UPLOAD refleje
        # el `chunk` que está subiendo ahora, no el último que flipeó el
        # slot activo.
        if self._upload_recorder_provider is not None:
            upload_rec = self._upload_recorder_provider()
            if upload_rec is not None:
                upload_stages = upload_rec.stages_snapshot()
                if UPLOAD_STAGE in upload_stages:
                    stages = {**stages, UPLOAD_STAGE: upload_stages[UPLOAD_STAGE]}
        pool = self._pool_stats.snapshot()
        # 052: una vez completo, mide contra el tiempo de finalización
        # congelado para que el timer del footer pare en vez de seguir
        # contando después del fin de la corrida.
        if self._batch_started_monotonic is None:
            elapsed = 0.0
        else:
            end = self._batch_completed_monotonic or self._clock()
            elapsed = end - self._batch_started_monotonic
        completed = pool.completed
        throughput = (completed / elapsed) if elapsed > 0 and completed > 0 else 0.0

        chunks_snapshot = self._chunks_state_snapshot()
        processed = self._docs_processed(chunks_snapshot, pool)
        window_rate = self._sample_window(processed)
        eta_run_s = self._eta_run(processed, window_rate)
        (
            chunk_bytes_uploaded,
            chunk_bytes_total,
            chunk_elapsed_s,
            chunk_avg_mbps,
            chunk_eta_s,
        ) = self._current_chunk_progress(chunks_snapshot, global_elapsed_s=elapsed)

        bw_cfg = self._cmis_config.auto_tune
        failed_total, failures_by_type, failures_by_status = (
            self._upload_metrics.failure_breakdown()
        )
        return TUISnapshot(
            pipeline=self._pipeline_name,
            batch_id=self._batch_id,
            elapsed_s=elapsed,
            throughput_docs_per_s=throughput,
            is_complete=self._is_complete,
            stages=stages,
            pool_capacity=self._concurrency_limit.capacity,
            pool_in_use=pool.busy,
            pool_idle=max(0, self._concurrency_limit.capacity - pool.busy),
            queue_depth=pool.queue_depth,
            closing=pool.closing,
            auto_tune_enabled=bw_cfg.enabled,
            auto_tune_target_p95_ms=bw_cfg.target_p95_ms,
            auto_tune_observed_p95_ms=self._metrics.current_stage_p95(UPLOAD_STAGE),
            auto_tune_adjust_interval_s=bw_cfg.adjustment_interval_s,
            auto_tune_next_in_s=(self._auto_tune.seconds_to_next_tick if self._auto_tune else 0.0),
            auto_tune_timeout_s=self._uploader.current_timeout_s,
            auto_tune_timeout_min_s=bw_cfg.min_timeout_s,
            auto_tune_timeout_max_s=bw_cfg.max_timeout_s,
            auto_tune_last_action=self._last_action(),
            auto_tune_last_workers_after=self._last_workers_after(),
            auto_tune_seconds_since_last_decision=(
                self._auto_tune.seconds_since_last_decision if self._auto_tune else None
            ),
            cmis_endpoint=self._cmis_config.base_url,
            # 054: `bandwidth` + slow ops son S5-shaped — tienen que leer el
            # recorder del lado UPLOAD. ``self._metrics`` (que es PREP-aware)
            # flipea al `chunk` N+1 apenas entra a PREP, y los
            # _BandwidthHandler / _SlowOpHandler por-batch de N+1 filtran
            # los eventos cmis_upload del batch N, así que leerlo acá
            # mostraba 0 de `bandwidth` / `sparkline` vacía / sin slow ops
            # durante el upload. ``_upload_metrics`` cae a ``_metrics``
            # para corridas single-batch.
            bandwidth_current_mbps=self._upload_metrics.bandwidth.current_mbps(),
            bandwidth_peak_mbps=self._upload_metrics.bandwidth.peak_mbps(),
            bandwidth_ceiling_mbps=self._bandwidth_ceiling_mbps,
            bandwidth_series=tuple(self._upload_metrics.bandwidth.series(60)),
            slow_ops_all=tuple(self._upload_metrics.aggregator_snapshot()),
            failed_total=failed_total,
            failures_by_type=failures_by_type,
            failures_by_status=failures_by_status,
            chunks_state=chunks_snapshot,
            # 051: total de docs filtrados en S1 entre todos los `chunk`s
            # vistos hasta ahora. ``prep_filtered`` es un ``int`` en
            # ``ChunkState`` — el dict lo encajona como ``object``, así
            # que la coerción es runtime-safe.
            s1_filtered=sum(
                v for c in chunks_snapshot if isinstance((v := c.get("prep_filtered", 0)), int)
            ),
            lane_snapshot=(
                self._lane_controller.snapshot() if self._lane_controller is not None else None
            ),
            current_chunk_bytes_uploaded=chunk_bytes_uploaded,
            current_chunk_bytes_total=chunk_bytes_total,
            current_chunk_elapsed_s=chunk_elapsed_s,
            current_chunk_avg_mbps=chunk_avg_mbps,
            current_chunk_eta_s=chunk_eta_s,
            docs_processed=processed,
            throughput_window_docs_per_s=window_rate,
            planned_total=self._planned_total,
            eta_run_s=eta_run_s,
            mode=self._mode,
            bucket=(self._bucket_provider() if self._bucket_provider is not None else None),
        )

    # ------------------------------------------ 134: ventana deslizante + ETA

    @staticmethod
    def _docs_processed(
        chunks_snapshot: tuple[dict[str, object], ...], pool: WorkerPoolStatsSnapshot
    ) -> int:
        """Resultados terminales de la corrida. Con `chunk`s, suma sobre
        ellos; en el path monolítico (resume) cae a los contadores del pool."""
        if not chunks_snapshot:
            return int(pool.completed) + int(pool.failed)
        keys = (
            "s5_done",
            "s5_failed",
            "upload_skipped",
            "prep_failed",
            "prep_filtered",
            "prep_skipped",
        )
        total = 0
        for row in chunks_snapshot:
            for k in keys:
                v = row.get(k, 0)
                if isinstance(v, (int, float)):
                    total += int(v)
        return total

    def _sample_window(self, processed: int) -> float | None:
        now = self._clock()
        self._window.append((now, processed))
        cutoff = now - self._window_s
        # Queda como más vieja la última muestra en o antes del corte:
        # la ventana cubre ≥ window_s sin perder el punto de partida.
        while len(self._window) > 1 and self._window[1][0] <= cutoff:
            self._window.popleft()
        t0, c0 = self._window[0]
        dt = now - t0
        if dt > self._window_s * 1.5:
            # Hueco de muestreo (terminal suspendida, timer frenado): la
            # muestra sobreviviente ya no representa "los últimos 60 s".
            # Se descarta y la ventana arranca de nuevo desde ahora.
            while len(self._window) > 1:
                self._window.popleft()
            return None
        if dt < 1.0:
            return None
        return max(0, processed - c0) / dt

    def _eta_run(self, processed: int, window_rate: float | None) -> float | None:
        if self._planned_total is None or self._is_complete:
            return None
        remaining = self._planned_total - processed
        if remaining <= 0 or not window_rate or window_rate <= 0.0:
            return None
        return remaining / window_rate

    def _chunks_state_snapshot(self) -> tuple[dict[str, object], ...]:
        """Renderiza la máquina de estados de `chunk`s del orchestrator para el TUI.

        041 expande cada fila con estadísticas por `stage` y valores
        elapsed live. 042 sobrescribe ``s5_done`` / ``s5_failed`` desde
        el recorder activo de upload mientras un `chunk` está en estado
        ``UPLOAD`` para que la fila de CHUNKS tique live en vez de
        esperar la transición a DONE.
        """
        if self._chunks_provider is None:
            return ()
        chunks = self._chunks_provider()
        now = self._clock()
        # 042: el recorder del lado upload es el que tiene los contadores
        # del `chunk` que está actualmente en S5. Leerlo una vez por
        # snapshot evita contención de `lock` por fila.
        upload_rec = self._upload_recorder_provider() if self._upload_recorder_provider else None
        out: list[dict[str, object]] = []
        for chunk in chunks:
            status = str(getattr(chunk, "status", "?"))
            prep_started = getattr(chunk, "prep_started_monotonic", None)
            upload_started = getattr(chunk, "upload_started_monotonic", None)
            prep_elapsed = float(getattr(chunk, "prep_elapsed_s", 0.0) or 0.0)
            upload_elapsed = float(getattr(chunk, "upload_elapsed_s", 0.0) or 0.0)
            # Elapsed live para `stage`s en vuelo: lee el timestamp de inicio
            # y le resta AHORA. El orchestrator congela el valor cuando el
            # `chunk` deja el `stage`, así que los estados terminales
            # mantienen su número congelado.
            if status == "PREP" and isinstance(prep_started, (int, float)):
                prep_elapsed = max(prep_elapsed, now - float(prep_started))
            if status == "UPLOAD" and isinstance(upload_started, (int, float)):
                upload_elapsed = max(upload_elapsed, now - float(upload_started))
            s5_done = int(getattr(chunk, "s5_done", 0) or 0)
            s5_failed = int(getattr(chunk, "s5_failed", 0) or 0)
            upload_skipped = int(getattr(chunk, "upload_skipped", 0) or 0)
            # 042: override live mientras está en UPLOAD. El orchestrator
            # setea ``upload_active_recorder`` en exactamente un `chunk`
            # por vez (la `lane` de S5 es single-threaded entre `chunk`s),
            # así que esto es seguro.
            if status == "UPLOAD" and upload_rec is not None:
                s5_done = max(s5_done, upload_rec.upload_done_count())
                s5_failed = max(s5_failed, upload_rec.upload_failed_count())
                upload_skipped = max(upload_skipped, upload_rec.upload_skipped_count())
            out.append(
                {
                    "chunk_idx": getattr(chunk, "chunk_idx", -1),
                    "batch_id": getattr(chunk, "batch_id", ""),
                    "status": status,
                    "s5_done": s5_done,
                    "s5_failed": s5_failed,
                    # 041 — plan por `chunk` + desglose por `stage`
                    "doc_count": getattr(chunk, "doc_count", 0),
                    "total_bytes": getattr(chunk, "total_bytes", 0),
                    "prep_done": getattr(chunk, "prep_done", 0),
                    "prep_skipped": getattr(chunk, "prep_skipped", 0),
                    "prep_failed": getattr(chunk, "prep_failed", 0),
                    "prep_filtered": getattr(chunk, "prep_filtered", 0),
                    "upload_skipped": upload_skipped,
                    "prep_started_monotonic": prep_started,
                    "prep_elapsed_s": prep_elapsed,
                    "upload_started_monotonic": upload_started,
                    "upload_elapsed_s": upload_elapsed,
                }
            )
        return tuple(out)

    def _current_chunk_progress(
        self,
        chunks_snapshot: tuple[dict[str, object], ...],
        *,
        global_elapsed_s: float,
    ) -> tuple[int, int, float, float, float | None]:
        """Resuelve los cinco campos "current chunk" del tab UPLOAD (041).

        Devuelve ``(bytes_uploaded, bytes_total, elapsed_s, avg_mbps, eta_s)``.

        Estrategia:
        * Bytes-uploaded sale siempre del contador acumulativo del recorder
          live — funciona en single Y multi-batch porque el recorder es
          por-`chunk` en multi-batch y por corrida en single-batch.
        * Bytes-total + elapsed vienen del ``ChunkState`` del `chunk`
          activo cuando es multi-batch; si no (single-batch) bytes-total
          es 0 y elapsed es el elapsed global de la corrida.
        * avg_mbps y ETA son derivados. ETA queda oculto (``None``) hasta
          que el `chunk` pasa el 5 % de sus bytes (la proyección
          temprana es ruidosa).
        """
        # 042: bindea el contador de bytes al recorder del lado UPLOAD
        # (el `chunk` actualmente en S5), no a ``self._metrics`` que es
        # PREP-aware y flipearía al recorder del próximo `chunk` mid-upload.
        recorder = self._upload_metrics
        bytes_uploaded = recorder.bandwidth.cumulative_bytes()
        bytes_total = 0
        elapsed_s = global_elapsed_s
        active = self._active_chunk(chunks_snapshot)
        if active is not None:
            bt = active.get("total_bytes")
            if isinstance(bt, int):
                bytes_total = bt
            # 054: el timer del tab UPLOAD tiene que medir la ventana de
            # S5, no prep+upload. Pre-054 leía ``prep_started_monotonic``
            # — así que para el `chunk` 0 el "chunk elapsed" contaba desde
            # ~el arranque del programa y ``avg_mbps`` quedaba diluido por
            # toda la fase PREP.
            status = str(active.get("status", ""))
            if status == "UPLOAD":
                mono = active.get("upload_started_monotonic")
                elapsed_s = (
                    max(0.0, self._clock() - float(mono)) if isinstance(mono, (int, float)) else 0.0
                )
            elif status == "DONE":
                frozen = active.get("upload_elapsed_s")
                elapsed_s = float(frozen) if isinstance(frozen, (int, float)) else 0.0
            else:
                # PREP (o desconocido) — S5 no arrancó; sin upload elapsed todavía.
                elapsed_s = 0.0
        if elapsed_s > 0 and bytes_uploaded > 0:
            avg_mbps = (bytes_uploaded / 1_048_576.0) / elapsed_s
        else:
            avg_mbps = 0.0
        eta_s: float | None = None
        if bytes_total > 0 and bytes_uploaded > 0 and elapsed_s > 0:
            progress = bytes_uploaded / bytes_total
            if 0.05 < progress < 1.0:
                # proyección lineal naive — misma forma que una ETA por
                # conteo de docs, pero en bytes. Los operadores lo leen
                # como "alcanza para decidir si tomar otro café", que es
                # lo que piden.
                eta_s = elapsed_s * (1.0 - progress) / progress
        return bytes_uploaded, bytes_total, elapsed_s, avg_mbps, eta_s

    @staticmethod
    def _active_chunk(
        chunks_snapshot: tuple[dict[str, object], ...],
    ) -> dict[str, object] | None:
        """Elige el `chunk` que debería manejar el bloque de progreso del tab UPLOAD.

        Orden de preferencia: UPLOAD-en-vuelo > PREP-en-vuelo > último DONE.
        Devuelve ``None`` cuando no hay `chunk`s (modo single-batch).
        """
        if not chunks_snapshot:
            return None
        upload = [c for c in chunks_snapshot if str(c.get("status", "")) == "UPLOAD"]
        if upload:
            return upload[-1]
        prep = [c for c in chunks_snapshot if str(c.get("status", "")) == "PREP"]
        if prep:
            return prep[-1]
        done = [c for c in chunks_snapshot if str(c.get("status", "")) == "DONE"]
        if done:
            return done[-1]
        return None

    # ------------------------------------------------------- `helper`s

    def _last_action(self) -> str:
        if self._auto_tune is None or self._auto_tune.last_decision is None:
            return "—"
        return self._auto_tune.last_decision.action

    def _last_workers_after(self) -> int:
        if self._auto_tune is None or self._auto_tune.last_decision is None:
            return 0
        return self._auto_tune.last_decision.workers
