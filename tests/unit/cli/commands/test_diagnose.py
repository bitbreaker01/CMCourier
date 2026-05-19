"""092 — Tests del comando ``cmcourier diagnose``.

Cubre:
- Detección del bottleneck por dominancia (>= 50% del wall)
- Sugerencias contextuales por stage
- Manejo robusto de archivos faltantes / vacíos
- Selección por --latest y --batch <id>
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cmcourier.cli.commands.diagnose import (
    _BatchSummary,
    _diagnose_bottleneck,
    _find_metrics_files,
    _iter_batch_summaries,
    _select_summary,
    _stage_totals,
)

pytestmark = pytest.mark.unit


def _write_summary(file: Path, payload: dict) -> None:
    payload["kind"] = "batch_summary"
    line = json.dumps(payload)
    with file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _build_summary(
    *,
    batch_id: str = "abc-123",
    total_docs: int = 100,
    elapsed_s: float = 50.0,
    stages: dict | None = None,
) -> _BatchSummary:
    payload = {
        "pipeline": "csv-trigger",
        "batch_id": batch_id,
        "total_docs": total_docs,
        "elapsed_s": elapsed_s,
        "throughput_docs_per_s": total_docs / elapsed_s if elapsed_s > 0 else 0.0,
        "stages": stages
        or {
            "S1": {"count": 100, "p50_ms": 5.0, "p95_ms": 15.0, "p99_ms": 20.0, "sum_ms": 500.0},
            "S4": {
                "count": 100,
                "p50_ms": 200.0,
                "p95_ms": 600.0,
                "p99_ms": 1000.0,
                "sum_ms": 30000.0,
            },
            "S5": {
                "count": 100,
                "p50_ms": 50.0,
                "p95_ms": 200.0,
                "p99_ms": 400.0,
                "sum_ms": 8000.0,
            },
        },
        "kind": "batch_summary",
    }
    return _BatchSummary(payload)


class TestBottleneckDetection:
    def test_dominant_stage_is_flagged(self) -> None:
        s = _build_summary()
        rows = _stage_totals(s)
        # S4 sum_ms=30000 vs total 38500 = 77.9% > 50% — dominant.
        bottleneck, sugg = _diagnose_bottleneck(rows)
        assert bottleneck == "S4"
        assert any("S4" in line for line in sugg)

    def test_no_clear_bottleneck_when_balanced(self) -> None:
        # 3 stages con ~33% c/u → ninguno > 50%.
        s = _build_summary(
            stages={
                "S2": {
                    "count": 100,
                    "p50_ms": 10.0,
                    "p95_ms": 20.0,
                    "p99_ms": 30.0,
                    "sum_ms": 3000.0,
                },
                "S3": {
                    "count": 100,
                    "p50_ms": 10.0,
                    "p95_ms": 20.0,
                    "p99_ms": 30.0,
                    "sum_ms": 3000.0,
                },
                "S4": {
                    "count": 100,
                    "p50_ms": 10.0,
                    "p95_ms": 20.0,
                    "p99_ms": 30.0,
                    "sum_ms": 3000.0,
                },
            }
        )
        rows = _stage_totals(s)
        bottleneck, sugg = _diagnose_bottleneck(rows)
        assert bottleneck is None
        assert any("No single stage dominates" in line for line in sugg)

    def test_s5_with_high_latency_emits_server_side_hints(self) -> None:
        s = _build_summary(
            stages={
                "S4": {
                    "count": 100,
                    "p50_ms": 5.0,
                    "p95_ms": 10.0,
                    "p99_ms": 15.0,
                    "sum_ms": 500.0,
                },
                "S5": {
                    "count": 100,
                    "p50_ms": 800.0,
                    "p95_ms": 2000.0,
                    "p99_ms": 3000.0,
                    "sum_ms": 80000.0,
                },
            }
        )
        rows = _stage_totals(s)
        bottleneck, sugg = _diagnose_bottleneck(rows)
        assert bottleneck == "S5"
        assert any("CMIS server" in line for line in sugg)

    def test_s4_with_high_latency_emits_disk_hints(self) -> None:
        s = _build_summary(
            stages={
                "S4": {
                    "count": 100,
                    "p50_ms": 1500.0,
                    "p95_ms": 4000.0,
                    "p99_ms": 6000.0,
                    "sum_ms": 200000.0,
                },
                "S5": {
                    "count": 100,
                    "p50_ms": 10.0,
                    "p95_ms": 20.0,
                    "p99_ms": 30.0,
                    "sum_ms": 1000.0,
                },
            }
        )
        rows = _stage_totals(s)
        bottleneck, sugg = _diagnose_bottleneck(rows)
        assert bottleneck == "S4"
        assert any("disk" in line.lower() or "Defender" in line for line in sugg)


class TestFileSelection:
    def test_find_metrics_files_orders_by_recency(self, tmp_path: Path) -> None:
        import time

        old = tmp_path / "metrics-2026-01-01.jsonl"
        new = tmp_path / "metrics-2026-05-19.jsonl"
        old.write_text("")
        time.sleep(0.05)
        new.write_text("")
        files = _find_metrics_files(tmp_path)
        assert files[0] == new
        assert files[1] == old

    def test_missing_log_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(Exception) as exc:
            _find_metrics_files(tmp_path / "nonexistent")
        assert "log_dir" in str(exc.value).lower()

    def test_empty_log_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(Exception) as exc:
            _find_metrics_files(tmp_path)
        assert "No metrics" in str(exc.value)


class TestBatchSelection:
    def test_select_latest_returns_first(self) -> None:
        s1 = _build_summary(batch_id="first")
        s2 = _build_summary(batch_id="second")
        # _select_summary devuelve summaries[0] cuando batch_id is None.
        result = _select_summary([s2, s1], batch_id=None)
        assert result.batch_id == "second"

    def test_select_by_batch_id(self) -> None:
        s1 = _build_summary(batch_id="alpha")
        s2 = _build_summary(batch_id="bravo")
        result = _select_summary([s1, s2], batch_id="bravo")
        assert result.batch_id == "bravo"

    def test_select_unknown_batch_raises(self) -> None:
        s1 = _build_summary(batch_id="alpha")
        with pytest.raises(Exception) as exc:
            _select_summary([s1], batch_id="zzz")
        assert "not found" in str(exc.value).lower()

    def test_select_from_empty_list_raises(self) -> None:
        with pytest.raises(Exception) as exc:
            _select_summary([], batch_id=None)
        assert "No batch_summary" in str(exc.value)


class TestJsonlParsing:
    def test_iter_skips_non_summary_events(self, tmp_path: Path) -> None:
        log = tmp_path / "metrics-2026-05-19.jsonl"
        log.write_text(
            json.dumps({"kind": "other", "foo": "bar"})
            + "\n"
            + json.dumps(
                {
                    "kind": "batch_summary",
                    "batch_id": "x",
                    "pipeline": "p",
                    "total_docs": 1,
                    "elapsed_s": 1.0,
                    "throughput_docs_per_s": 1.0,
                    "stages": {},
                }
            )
            + "\n"
        )
        summaries = _iter_batch_summaries([log])
        assert len(summaries) == 1
        assert summaries[0].batch_id == "x"

    def test_iter_skips_malformed_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "metrics-2026-05-19.jsonl"
        log.write_text(
            "not-json-at-all\n"
            + json.dumps(
                {
                    "kind": "batch_summary",
                    "batch_id": "y",
                    "pipeline": "p",
                    "total_docs": 1,
                    "elapsed_s": 1.0,
                    "throughput_docs_per_s": 1.0,
                    "stages": {},
                }
            )
            + "\n"
            + "{ broken json\n"
        )
        summaries = _iter_batch_summaries([log])
        assert len(summaries) == 1
        assert summaries[0].batch_id == "y"
