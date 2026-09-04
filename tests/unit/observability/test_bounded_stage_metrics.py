"""Tests de las métricas de stage acotadas (108).

Pre-108 ``_StageBucket`` guardaba TODAS las muestras de la corrida y
``summary()`` copiaba + ordenaba la lista completa bajo lock en cada
llamada — con el TUI a 4 Hz y el recorder único del modo streaming eso
era O(total_docs·log) trece veces por segundo, un leak de memoria, y un
p95 acumulativo que dejaba al AIMD ciego ante degradaciones recientes.

108: ventana deslizante (deque maxlen=2048) para percentiles,
count/sum_ms acumulativos aparte, summary cacheado, y lock sobre el
dict de buckets del recorder.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from cmcourier.observability.metrics import (
    _STAGE_WINDOW_SAMPLES,
    MetricsRecorder,
    _StageBucket,
)

pytestmark = pytest.mark.unit


def _make_recorder(tmp_path: Path) -> MetricsRecorder:
    return MetricsRecorder(
        enabled=True,
        pipeline_metrics_enabled=True,
        log_dir=tmp_path,
        slow_op_threshold_ms=5000.0,
        slow_op_top_n=5,
    )


class TestBoundedWindow:
    def test_window_is_bounded_but_count_is_cumulative(self) -> None:
        # E1: 100k muestras → a lo sumo _STAGE_WINDOW_SAMPLES retenidas,
        # count y sum_ms totales.
        bucket = _StageBucket()
        total = 100_000
        for _ in range(total):
            bucket.record(10.0)
        s = bucket.summary()
        assert s["count"] == total
        assert s["sum_ms"] == pytest.approx(10.0 * total)
        assert len(bucket.window) <= _STAGE_WINDOW_SAMPLES

    def test_percentiles_follow_recent_samples(self) -> None:
        # E2: histórico rápido + presente lento → el p95 refleja el
        # presente (pre-108 quedaba diluido por el histórico).
        bucket = _StageBucket()
        for _ in range(5000):
            bucket.record(100.0)
        for _ in range(3000):
            bucket.record(9000.0)
        s = bucket.summary()
        assert s["p95_ms"] == pytest.approx(9000.0)
        assert s["p50_ms"] == pytest.approx(9000.0)
        assert s["count"] == 8000

    def test_empty_bucket_shape_unchanged(self) -> None:
        # E5: mismas claves y tipos que pre-108.
        s = _StageBucket().summary()
        assert s == {
            "count": 0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "sum_ms": 0.0,
        }

    def test_small_sample_percentiles_exact(self) -> None:
        bucket = _StageBucket()
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            bucket.record(v)
        s = bucket.summary()
        assert s["count"] == 5
        assert s["p50_ms"] == 3.0
        assert s["sum_ms"] == 15.0


class TestSummaryCache:
    def test_summary_is_cached_between_records(self) -> None:
        # E3: sin muestras nuevas, summary() devuelve el mismo objeto
        # (no recomputa ni reordena).
        bucket = _StageBucket()
        bucket.record(50.0)
        first = bucket.summary()
        second = bucket.summary()
        assert first is second

    def test_cache_invalidated_by_new_sample(self) -> None:
        bucket = _StageBucket()
        bucket.record(50.0)
        before = bucket.summary()
        bucket.record(150.0)
        after = bucket.summary()
        assert after is not before
        assert after["count"] == 2
        assert after["sum_ms"] == 200.0


class TestBucketsDictRace:
    def test_concurrent_new_stages_and_snapshots(self, tmp_path: Path) -> None:
        """E4: setdefault de stages nuevos desde N threads mientras otro
        thread hace stages_snapshot() en loop — sin RuntimeError."""
        recorder = _make_recorder(tmp_path)
        recorder.start_batch(pipeline="test", batch_id="b1")
        stop = threading.Event()
        errors: list[Exception] = []

        def writer(worker: int) -> None:
            i = 0
            while not stop.is_set():
                recorder.record_stage(stage=f"S{worker}.sub{i % 97}", duration_ms=1.0)
                i += 1

        def snapshotter() -> None:
            try:
                while not stop.is_set():
                    snap = recorder.stages_snapshot()
                    assert isinstance(snap, dict)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        writers = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
        reader = threading.Thread(target=snapshotter)
        for t in [*writers, reader]:
            t.start()
        import time

        time.sleep(0.5)
        stop.set()
        for t in [*writers, reader]:
            t.join()
        assert not errors, f"snapshot concurrente falló: {errors[0]}"

    def test_p95_with_count_uses_window(self, tmp_path: Path) -> None:
        # REQ-005: la señal del AIMD es reciente.
        recorder = _make_recorder(tmp_path)
        recorder.start_batch(pipeline="test", batch_id="b1")
        for _ in range(4000):
            recorder.record_stage(stage="S5", duration_ms=100.0)
        for _ in range(3000):
            recorder.record_stage(stage="S5", duration_ms=8000.0)
        p95, count = recorder.current_stage_p95_with_count("S5")
        assert p95 == pytest.approx(8000.0)
        assert count == 7000
