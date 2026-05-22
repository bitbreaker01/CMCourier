"""Aggregators de pipeline (`tier` 2) + `network events` (`tier` 3) + `slow ops` (`tier` 4).

Lo posee el orquestador. Los timings por etapa fluyen a través de
:class:`StageTimer`; el recorder agrega por batch y emite una sola línea
de resumen al cerrar. La agregación de `slow ops` corre a través de un
:class:`logging.Handler` custom atacheado al arrancar el batch, así el
código de los adaptadores se mantiene inconsciente (emite al logger de
red conocido y el handler atrapa lo que supere el `threshold`).
"""

from __future__ import annotations

__all__ = [
    "BatchSummary",
    "MetricsRecorder",
    "NetworkEvent",
    "SlowOpAggregator",
    "StageTimer",
]

import datetime as _dt
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

_pipeline_log = logging.getLogger("cmcourier.metrics.pipeline")
_app_log = logging.getLogger("cmcourier")


# ---------------------------------------------------------------------------
# Payload de `network event` (`tier` 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NetworkEvent:
    """Una observación individual del `tier` de red.

    La emiten los adaptadores de AS400 + CMIS vía el logger
    ``cmcourier.metrics.network`` como ``extra={...}``.
    """

    kind: str
    duration_ms: float
    sql_prefix: str = ""
    row_count: int | None = None
    size_bytes: int | None = None
    status: int | None = None
    url_prefix: str = ""
    txn_num: str = ""


# ---------------------------------------------------------------------------
# Agregación de timing por etapa (`tier` 2)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _StageBucket:
    """Acumulador `thread-safe` de timing por etapa (025).

    La versión single-threaded de 020 no tenía lock — append desde un
    `thread`, snapshot desde el mismo `thread`. 025 agrega concurrencia
    de `workers` de S5: ``record`` se llama desde N `worker threads`
    mientras que ``summary`` se llama desde los `threads` del orquestador
    + TUI. El lock mantiene la lista subyacente consistente.
    """

    durations_ms: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, duration_ms: float) -> None:
        with self._lock:
            self.durations_ms.append(duration_ms)

    def summary(self) -> dict[str, float | int]:
        with self._lock:
            snapshot = list(self.durations_ms)
        if not snapshot:
            return {
                "count": 0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
                "sum_ms": 0.0,
            }
        sorted_ms = sorted(snapshot)
        return {
            "count": len(sorted_ms),
            "p50_ms": _percentile(sorted_ms, 0.50),
            "p95_ms": _percentile(sorted_ms, 0.95),
            "p99_ms": _percentile(sorted_ms, 0.99),
            "sum_ms": sum(sorted_ms),
        }


def _percentile(sorted_values: list[float], q: float) -> float:
    """Percentil `nearest-rank`. ``q`` en ``[0, 1]``."""
    if not sorted_values:
        return 0.0
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    # rank = ceil(q * n), `1-indexed`
    rank = max(1, int(q * len(sorted_values) + 0.999999))
    return sorted_values[min(rank, len(sorted_values)) - 1]


# ---------------------------------------------------------------------------
# `Aggregator` de `slow ops` (`tier` 4)
# ---------------------------------------------------------------------------


class SlowOpAggregator:
    """Junta candidatos a `slow ops`; emite el top-N al cerrar el batch.

    `Thread-safe` (025): ``consider`` se llama desde los `worker threads`
    de S5 vía el handler del logger de red.
    """

    def __init__(self, *, threshold_ms: float, top_n: int) -> None:
        self._threshold_ms = float(threshold_ms)
        self._top_n = int(top_n)
        self._candidates: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def consider(
        self,
        *,
        kind: str,
        duration_ms: float,
        txn_num: str = "",
        stage: str = "",
        size_bytes: int | None = None,
        url_prefix: str = "",
        worker: str = "",
    ) -> None:
        if duration_ms < self._threshold_ms:
            return
        entry: dict[str, Any] = {
            "kind": kind,
            "duration_ms": float(duration_ms),
        }
        if txn_num:
            entry["txn_num"] = txn_num
        if stage:
            entry["stage"] = stage
        if size_bytes is not None:
            entry["size_bytes"] = int(size_bytes)
        if url_prefix:
            entry["url_prefix"] = url_prefix
        if worker:
            entry["worker"] = worker
        with self._lock:
            self._candidates.append(entry)

    def top(self) -> list[dict[str, Any]]:
        with self._lock:
            snapshot = list(self._candidates)
        ranked = sorted(snapshot, key=lambda d: d["duration_ms"], reverse=True)[: self._top_n]
        return [{"rank": i + 1, **entry} for i, entry in enumerate(ranked)]


class _BandwidthSampler:
    """Sampler `rolling` de 1 Hz para el `bandwidth` de upload CMIS (025 fase 3).

    Alimenta el chart de `bandwidth` del TUI y los paneles WORKERS/NETWORK.
    Agrupa los tamaños de ``cmis_upload`` por segundo de `wall-clock`;
    mantiene una `rolling window` de 60 `buckets` para que el chart muestre
    el último minuto. `Thread-safe` — lo alimentan los `worker threads`
    vía el ``_BandwidthHandler``.
    """

    _WINDOW_SECONDS = 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # bucket_ts (segundos enteros desde el epoch) → total de bytes en ese segundo
        self._buckets: dict[int, int] = {}
        # 041: bytes `cumulative` subidos desde que se creó este sampler.
        # Los `buckets` `rolling` de arriba se descartan después de 60 s;
        # este contador nunca, así la TUI puede mostrar un total
        # "MB subidos hasta ahora" por `chunk`. El ciclo de vida de
        # ``MetricsRecorder`` es por `chunk`, así que naturalmente queda
        # scopeado al `chunk`.
        self._cumulative_bytes: int = 0

    def record_upload(
        self,
        size_bytes: int,
        *,
        started_at: float,
        completed_at: float,
    ) -> None:
        """069: distribuye ``size_bytes`` uniformemente sobre los `buckets`
        de un segundo que solapan ``[started_at, completed_at]``.

        Pre-069 acreditaba el tamaño completo del archivo a
        ``int(completed_at)`` — para un upload de 30 MB sobre 3 segundos,
        los 30 MB enteros caían en un solo `bucket` y los otros dos
        mostraban cero. Eso producía lecturas con picos, peaks engañosos
        y una `sparkline` que no reflejaba el `throughput` sostenido.
        Distribuir hace que ``current_mbps`` / ``peak_mbps`` / ``series``
        sean fieles a la tasa real de transmisión.

        Se asume que la tasa de transmisión es constante dentro de un
        upload. Suaviza levemente las ráfagas internas pero es correcto
        en agregado.
        """
        if size_bytes <= 0:
            return
        duration = max(completed_at - started_at, 0.0)
        end_ts = int(completed_at)
        cutoff = end_ts - self._WINDOW_SECONDS
        with self._lock:
            self._cumulative_bytes += int(size_bytes)
            if duration <= 0.0:
                # Upload sub-milisegundo o de duración cero — acredita al
                # segundo de finalización (shape pre-069, fallback defensivo).
                self._buckets[end_ts] = self._buckets.get(end_ts, 0) + int(size_bytes)
            else:
                bytes_per_s = float(size_bytes) / duration
                start_ts = int(started_at)
                for ts in range(start_ts, end_ts + 1):
                    overlap_start = max(started_at, float(ts))
                    overlap_end = min(completed_at, float(ts) + 1.0)
                    overlap = overlap_end - overlap_start
                    if overlap <= 0.0:
                        continue
                    bytes_in_bucket = int(bytes_per_s * overlap)
                    if bytes_in_bucket > 0:
                        self._buckets[ts] = self._buckets.get(ts, 0) + bytes_in_bucket
            stale = [k for k in self._buckets if k < cutoff]
            for k in stale:
                del self._buckets[k]

    def record_progress(self, bytes_delta: int, ts: float | None = None) -> None:
        """077: agrega ``bytes_delta`` al bucket del segundo current.

        A diferencia de :meth:`record_upload` (que distribuye un total
        sobre ``[started_at, completed_at]`` al completar el upload),
        este método agrega bytes parciales al wall-clock second en
        que se transmiten. Lo llama ``_BandwidthHandler`` con cada
        evento ``cmis_upload_progress`` que emite el
        ``MultipartEncoderMonitor`` del uploader.

        Thread-safe vía el lock interno del sampler. El bucket se
        elige según ``ts`` (default ``time.time()``).
        """
        if bytes_delta <= 0:
            return
        ts = ts if ts is not None else time.time()
        bucket = int(ts)
        cutoff = bucket - self._WINDOW_SECONDS
        with self._lock:
            self._cumulative_bytes += int(bytes_delta)
            self._buckets[bucket] = self._buckets.get(bucket, 0) + int(bytes_delta)
            stale = [k for k in self._buckets if k < cutoff]
            for k in stale:
                del self._buckets[k]

    def cumulative_bytes(self) -> int:
        """Total de bytes subidos desde que arrancó este sampler (nunca decae)."""
        with self._lock:
            return self._cumulative_bytes

    def current_mbps(self) -> float:
        """MB/s en el `bucket` de 1 segundo más reciente que esté completo."""
        now = int(time.time())
        with self._lock:
            # Mira el `bucket` completo anterior — el actual todavía puede
            # estar llenándose.
            return self._buckets.get(now - 1, 0) / 1_000_000.0

    def peak_mbps(self) -> float:
        with self._lock:
            if not self._buckets:
                return 0.0
            return max(self._buckets.values()) / 1_000_000.0

    def series(self, seconds: int = _WINDOW_SECONDS) -> list[tuple[int, float]]:
        """Devuelve ``[(offset_s_negative, mbps)...]`` con el más nuevo al final."""
        now = int(time.time())
        seconds = max(1, min(seconds, self._WINDOW_SECONDS))
        with self._lock:
            return [
                (
                    -(seconds - i - 1),
                    self._buckets.get(now - (seconds - i - 1), 0) / 1_000_000.0,
                )
                for i in range(seconds)
            ]


class _BandwidthHandler(logging.Handler):
    """Handler de logging que alimenta el `bandwidth sampler` desde los `network events`.

    042: filtra por ``batch_id`` para que `chunks` solapados
    (``batches_in_flight>1``) no se filtren bytes entre sus samplers por
    `chunk`. Pre-042, cada handler vivo recibía todos los eventos
    ``cmis_upload`` sin importar qué `chunk` los produjo — el mismo shape
    que ``_SlowOpHandler`` resolvió en 025 con un short-circuit sobre
    ``record.batch_id != self._batch_id``.
    """

    def __init__(self, sampler: _BandwidthSampler, *, batch_id: str) -> None:
        super().__init__(level=logging.INFO)
        self._sampler = sampler
        self._batch_id = batch_id

    def emit(self, record: logging.LogRecord) -> None:
        kind = getattr(record, "kind", "")
        # 077: progress events parciales alimentan el bucket actual.
        # Llegan durante el upload (no esperan a completion).
        if kind == "cmis_upload_progress":
            if getattr(record, "batch_id", None) != self._batch_id:
                return
            delta = getattr(record, "bytes_delta", None)
            if delta is None:
                return
            try:
                delta_int = int(delta)
            except (TypeError, ValueError):
                return
            if delta_int <= 0:
                return
            self._sampler.record_progress(delta_int, ts=float(record.created))
            return
        if kind != "cmis_upload":
            return
        if getattr(record, "batch_id", None) != self._batch_id:
            return
        size = getattr(record, "size_bytes", None)
        if size is None:
            return
        # 077: si hubo progress events, ya los contabilizamos en
        # ``record_progress``. Restamos esos bytes del total para evitar
        # double-counting. El residuo (el "último chunk" que no alcanzó
        # el threshold + cualquier discrepancia menor) se distribuye con
        # la lógica 069. Cuando ``progress_bytes`` es 0 (uploads chiquitos
        # que nunca dispararon el threshold), el comportamiento es
        # idéntico a pre-077.
        progress_bytes = int(getattr(record, "progress_bytes", 0) or 0)
        residual = max(0, int(size) - progress_bytes)
        if residual == 0:
            return
        # 069: deriva la ventana de transmisión a partir de ``duration_ms``
        # para que el sampler pueda distribuir los bytes uniformemente.
        # ``duration_ms`` siempre lo setea ``CmisUploader._emit_network``
        # para los eventos ``cmis_upload``; fallback defensivo a acreditar
        # a la finalización cuando falta o es cero (shape pre-069).
        completed_at = float(record.created)
        duration_ms = getattr(record, "duration_ms", 0.0) or 0.0
        try:
            duration_s = float(duration_ms) / 1000.0
        except (TypeError, ValueError):
            duration_s = 0.0
        started_at = completed_at - max(0.0, duration_s)
        self._sampler.record_upload(
            residual,
            started_at=started_at,
            completed_at=completed_at,
        )


class _SlowOpHandler(logging.Handler):
    """Handler de logging que alimenta el `aggregator` de `slow ops` por batch.

    Se atachea a ``cmcourier``, ``cmcourier.metrics.network`` al arrancar
    el batch. Cualquier record con atributo ``duration_ms`` es candidato;
    el `threshold` del `aggregator` filtra las operaciones baratas antes
    de la asignación.

    028: cada handler se taggea con el ``batch_id`` del recorder que lo
    posee. Los records cuyo ``record.batch_id`` no coincide se descartan
    a nivel del handler para que múltiples recorders concurrentes (uno
    por `chunk`) no se polinicen entre sí. Un record sin el extra
    ``batch_id`` también se descarta — los `slow ops` fuera del ciclo de
    vida de algún batch no son significativos.
    """

    def __init__(self, aggregator: SlowOpAggregator, *, batch_id: str) -> None:
        super().__init__(level=logging.INFO)
        self._agg = aggregator
        self._batch_id = batch_id

    def emit(self, record: logging.LogRecord) -> None:
        dms = getattr(record, "duration_ms", None)
        if dms is None:
            return
        record_batch_id = getattr(record, "batch_id", None)
        if record_batch_id != self._batch_id:
            return
        self._agg.consider(
            kind=getattr(record, "kind", record.name),
            duration_ms=float(dms),
            txn_num=getattr(record, "txn_num", "") or "",
            stage=getattr(record, "stage", "") or "",
            size_bytes=getattr(record, "size_bytes", None),
            url_prefix=getattr(record, "url_prefix", "") or "",
            worker=getattr(record, "worker", "") or "",
        )


# ---------------------------------------------------------------------------
# Builder del resumen por batch
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchSummary:
    pipeline: str
    batch_id: str
    total_docs: int
    elapsed_s: float
    throughput_docs_per_s: float
    stages: dict[str, dict[str, float | int]]
    # 104: desglose de la tasa de error de S5 por tipo y por status HTTP.
    failed_total: int = 0
    failures_by_type: dict[str, int] = field(default_factory=dict)
    failures_by_status: dict[int, int] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "ts": _dt.datetime.now(tz=_dt.UTC).isoformat(),
            "kind": "batch_summary",
            "pipeline": self.pipeline,
            "batch_id": self.batch_id,
            "total_docs": self.total_docs,
            "elapsed_s": round(self.elapsed_s, 4),
            "throughput_docs_per_s": round(self.throughput_docs_per_s, 4),
            "failed_total": self.failed_total,
            "failures_by_type": self.failures_by_type,
            "failures_by_status": self.failures_by_status,
            "stages": self.stages,
        }


# ---------------------------------------------------------------------------
# MetricsRecorder — fachada de cara al orquestador
# ---------------------------------------------------------------------------


class MetricsRecorder:
    """Lo posee el orquestador. Ciclo de vida por batch.

    El recorder es responsable de:
    * agregación de timing por etapa (S0..S5)
    * recolección de `slow ops` (vía un logging.Handler instalado para el batch)
    * emisión del resumen por batch a ``cmcourier.metrics.pipeline``
    * emisión del archivo de `slow ops` al cerrar el batch
    """

    def __init__(
        self,
        *,
        log_dir: Path,
        slow_op_threshold_ms: float,
        slow_op_top_n: int,
        enabled: bool = True,
        pipeline_metrics_enabled: bool = True,
    ) -> None:
        self._log_dir = log_dir
        self._slow_op_threshold_ms = float(slow_op_threshold_ms)
        self._slow_op_top_n = int(slow_op_top_n)
        self._enabled = enabled
        self._pipeline_metrics_enabled = pipeline_metrics_enabled
        self._stage_buckets: dict[str, _StageBucket] = {}
        self._aggregator: SlowOpAggregator | None = None
        self._slow_op_handler: _SlowOpHandler | None = None
        self._monitored_loggers: list[logging.Logger] = []
        # 025 fase 3: el `bandwidth sampler` es la fuente de datos del chart
        # de la tab UPLOAD del TUI. Activo durante todo el ciclo de vida
        # del batch; el handler se ataca/desataca junto con el handler de
        # `slow ops`.
        self._bandwidth = _BandwidthSampler()
        self._bandwidth_handler: _BandwidthHandler | None = None
        # 041: contador de "ya subidos" de S5 para que la tab CHUNKS pueda
        # mostrar los hits de idempotencia por `chunk`. Recorder por
        # `chunk` ⇒ count por `chunk`.
        self._s5_skipped: int = 0
        self._s5_skipped_lock = threading.Lock()
        # 042: contadores live de done/failed para que la tab CHUNKS pueda
        # mostrar s5_done mientras UPLOAD está en vuelo (pre-042 el
        # orquestador solo persistía los totales en la transición DONE,
        # dejando la fila en 0/0/0 durante toda la fase de upload).
        self._s5_done: int = 0
        self._s5_failed: int = 0
        self._s5_done_lock = threading.Lock()
        self._s5_failed_lock = threading.Lock()
        # 104: desglose de fallas de S5 por categoría y por status HTTP.
        # Se guardan bajo ``_s5_failed_lock`` junto con ``_s5_failed``.
        self._failures_by_type: dict[str, int] = {}
        self._failures_by_status: dict[int, int] = {}

    def start_batch(self, *, pipeline: str, batch_id: str) -> None:
        self._stage_buckets = {}
        if not self._enabled:
            return
        self._aggregator = SlowOpAggregator(
            threshold_ms=self._slow_op_threshold_ms,
            top_n=self._slow_op_top_n,
        )
        self._slow_op_handler = _SlowOpHandler(self._aggregator, batch_id=batch_id)
        self._bandwidth_handler = _BandwidthHandler(self._bandwidth, batch_id=batch_id)
        self._monitored_loggers = [
            logging.getLogger("cmcourier"),
            logging.getLogger("cmcourier.metrics.network"),
        ]
        for lg in self._monitored_loggers:
            lg.addHandler(self._slow_op_handler)
        logging.getLogger("cmcourier.metrics.network").addHandler(self._bandwidth_handler)
        _ = pipeline, batch_id  # reservado para uso futuro

    def record_stage(
        self,
        *,
        stage: str,
        duration_ms: float,
    ) -> None:
        bucket = self._stage_buckets.setdefault(stage, _StageBucket())
        bucket.record(duration_ms)

    def current_stage_p95(self, stage: str) -> float:
        """Latencia p95 live para una etapa (025 — alimenta el auto-tune)."""
        bucket = self._stage_buckets.get(stage)
        if bucket is None:
            return 0.0
        return float(bucket.summary()["p95_ms"])

    def current_stage_p95_with_count(self, stage: str) -> tuple[float, int]:
        """061: p95 + cantidad de samples en una sola lectura atómica para AIMD.

        El controller de auto-tune necesita ambos para decidir si la
        observación es lo suficientemente confiable como para actuar (un
        p95 `nearest-rank` con pocos samples queda dominado por outliers).
        El `dict-builder` en ``_StageBucket.summary()`` ya sostiene el lock
        del `bucket` para ambos valores, así que el par queda consistente.
        """
        bucket = self._stage_buckets.get(stage)
        if bucket is None:
            return 0.0, 0
        snap = bucket.summary()
        return float(snap["p95_ms"]), int(snap["count"])

    def close_batch(
        self,
        *,
        pipeline: str,
        batch_id: str,
        total_docs: int,
        elapsed_s: float,
    ) -> None:
        if not self._enabled:
            return
        try:
            if self._pipeline_metrics_enabled:
                summary = self._build_summary(
                    pipeline=pipeline,
                    batch_id=batch_id,
                    total_docs=total_docs,
                    elapsed_s=elapsed_s,
                )
                _pipeline_log.info(
                    "batch_summary",
                    extra={
                        "pipeline": summary.pipeline,
                        "batch_id": summary.batch_id,
                        "total_docs": summary.total_docs,
                        "elapsed_s": summary.elapsed_s,
                        "throughput_docs_per_s": summary.throughput_docs_per_s,
                        "failed_total": summary.failed_total,
                        "failures_by_type": summary.failures_by_type,
                        "failures_by_status": summary.failures_by_status,
                        "stages": summary.stages,
                        "kind": "batch_summary",
                    },
                )
            self._flush_slow_ops(batch_id=batch_id)
        finally:
            self._detach_handler()

    # ------------------------------------------------------------------ internos

    def _build_summary(
        self,
        *,
        pipeline: str,
        batch_id: str,
        total_docs: int,
        elapsed_s: float,
    ) -> BatchSummary:
        throughput = (total_docs / elapsed_s) if elapsed_s > 0 else 0.0
        stages: dict[str, dict[str, float | int]] = {}
        for stage_name, bucket in sorted(self._stage_buckets.items()):
            stages[stage_name] = bucket.summary()
        failed_total, failures_by_type, failures_by_status = self.failure_breakdown()
        return BatchSummary(
            pipeline=pipeline,
            batch_id=batch_id,
            total_docs=total_docs,
            elapsed_s=elapsed_s,
            throughput_docs_per_s=throughput,
            stages=stages,
            failed_total=failed_total,
            failures_by_type=failures_by_type,
            failures_by_status=failures_by_status,
        )

    def _flush_slow_ops(self, *, batch_id: str) -> None:
        if self._aggregator is None:
            return
        top = self._aggregator.top()
        if not top:
            return
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_dir / f"slow-ops-{batch_id}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for entry in top:
                fh.write(json.dumps(entry, ensure_ascii=False))
                fh.write("\n")

    def _detach_handler(self) -> None:
        if self._slow_op_handler is None:
            return
        for lg in self._monitored_loggers:
            lg.removeHandler(self._slow_op_handler)
        if self._bandwidth_handler is not None:
            logging.getLogger("cmcourier.metrics.network").removeHandler(self._bandwidth_handler)
        self._slow_op_handler = None
        self._bandwidth_handler = None
        self._monitored_loggers = []
        self._aggregator = None

    # ---------------------------------------------------- hooks de provider del TUI

    @property
    def bandwidth(self) -> _BandwidthSampler:
        """Handle de solo lectura para que la TUI obtenga la `series` del chart de `bandwidth`."""
        return self._bandwidth

    def aggregator_snapshot(self) -> list[dict[str, Any]]:
        """Snapshot del top-N de `slow ops` para la TUI; vacío cuando no hay batch activo."""
        if self._aggregator is None:
            return []
        return self._aggregator.top()

    def stages_snapshot(self) -> dict[str, dict[str, float | int]]:
        """Snapshot de percentiles/count por etapa para la TUI."""
        return {stage: bucket.summary() for stage, bucket in self._stage_buckets.items()}

    def record_upload_skipped(self) -> None:
        """041: contabiliza un outcome S5 de ``"skipped"`` (idempotencia / `claim-lost`)."""
        with self._s5_skipped_lock:
            self._s5_skipped += 1

    def upload_skipped_count(self) -> int:
        with self._s5_skipped_lock:
            return self._s5_skipped

    def record_upload_done(self) -> None:
        """042: contabiliza un outcome S5 de ``"done"`` (upload real completado)."""
        with self._s5_done_lock:
            self._s5_done += 1

    def upload_done_count(self) -> int:
        with self._s5_done_lock:
            return self._s5_done

    def record_upload_failed(
        self,
        category: str = "app_error",
        status_code: int | None = None,
    ) -> None:
        """042/104: contabiliza un outcome S5 de ``"failed"`` (upload con error).

        ``category`` viene de :func:`classify_failure` (``timeout`` /
        ``http_4xx`` / ``http_5xx`` / ``transport`` / ``app_error``).
        ``status_code`` es el código HTTP exacto para las categorías HTTP,
        o ``None`` para el resto — solo los no-``None`` cuentan en el
        sub-desglose por status.
        """
        with self._s5_failed_lock:
            self._s5_failed += 1
            self._failures_by_type[category] = self._failures_by_type.get(category, 0) + 1
            if status_code is not None:
                self._failures_by_status[status_code] = (
                    self._failures_by_status.get(status_code, 0) + 1
                )

    def upload_failed_count(self) -> int:
        with self._s5_failed_lock:
            return self._s5_failed

    def failure_breakdown(self) -> tuple[int, dict[str, int], dict[int, int]]:
        """Snapshot ``(total, por_categoría, por_status)`` de las fallas de S5.

        Lo consume :meth:`_build_summary` y la pestaña de upload del TUI."""
        with self._s5_failed_lock:
            return (
                self._s5_failed,
                dict(self._failures_by_type),
                dict(self._failures_by_status),
            )


# ---------------------------------------------------------------------------
# StageTimer — `context manager`
# ---------------------------------------------------------------------------


class StageTimer:
    """Context manager que mide el tiempo de una llamada de etapa y registra en un recorder.

    En ``__exit__``:
    * registra ``duration_ms`` en el recorder (para que se agreguen los percentiles)
    * emite un evento ``stage_complete`` al logger ``cmcourier`` en INFO
      con los campos estructurados que el formatter JSON promueve.
    """

    __slots__ = (
        "_batch_id",
        "_outcome",
        "_pipeline",
        "_recorder",
        "_stage",
        "_start_monotonic",
        "_txn_num",
    )

    def __init__(
        self,
        recorder: MetricsRecorder,
        *,
        pipeline: str,
        stage: str,
        batch_id: str,
        txn_num: str = "",
    ) -> None:
        self._recorder = recorder
        self._pipeline = pipeline
        self._stage = stage
        self._batch_id = batch_id
        self._txn_num = txn_num
        self._outcome = "OK"
        self._start_monotonic = 0.0

    def mark_failed(self) -> None:
        """Llamar desde dentro del bloque ``with`` cuando el caller atrapa una
        excepción de falla conocida y continúa. Sin este hook, una excepción
        atrapada parece un éxito desde el punto de vista de ``__exit__``.
        """
        self._outcome = "FAIL"

    def __enter__(self) -> StageTimer:
        self._start_monotonic = time.monotonic()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        duration_ms = (time.monotonic() - self._start_monotonic) * 1000.0
        if exc_type is not None:
            self._outcome = "FAIL"
        self._recorder.record_stage(stage=self._stage, duration_ms=duration_ms)
        _app_log.info(
            "stage_complete",
            extra={
                "pipeline": self._pipeline,
                "stage": self._stage,
                "batch_id": self._batch_id,
                "txn_num": self._txn_num,
                "outcome": self._outcome,
                "duration_ms": round(duration_ms, 3),
            },
        )
