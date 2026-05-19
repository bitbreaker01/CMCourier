"""093 — Tests del helper ``_record_s4_substages`` del orquestador.

Verifica que los sub-stage timings se registran como buckets
``S4.<sub>`` en el ``MetricsRecorder`` y que los campos en 0
(camino no tomado) se skipean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcourier.adapters.assembly.pdf_assembler import AssemblyTimings
from cmcourier.observability.metrics import MetricsRecorder
from cmcourier.orchestrators.staged import StagedPipeline

pytestmark = pytest.mark.unit


def _make_recorder(tmp_path: Path) -> MetricsRecorder:
    return MetricsRecorder(
        log_dir=tmp_path / "logs",
        slow_op_threshold_ms=5000.0,
        slow_op_top_n=10,
        enabled=False,  # no abre archivos
        pipeline_metrics_enabled=False,
    )


class TestNativePdfPath:
    def test_records_only_native_pdf_substages(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        timings = AssemblyTimings(
            path_kind="native_pdf",
            source_stat_ms=2.0,
            copy_native_ms=120.0,
            dst_stat_ms=1.5,
            discover_pages_ms=0.0,
            encode_pdf_ms=0.0,
        )
        StagedPipeline._record_s4_substages(rec, timings)

        # Los 3 buckets del camino native deben existir, los 2 del paged NO.
        summary = rec._build_summary(  # type: ignore[attr-defined]
            pipeline="test", batch_id="b", total_docs=1, elapsed_s=1.0
        )
        names = set(summary.stages.keys())
        assert "S4.source_stat" in names
        assert "S4.copy_native" in names
        assert "S4.dst_stat" in names
        assert "S4.discover_pages" not in names
        assert "S4.encode_pdf" not in names


class TestPagedPath:
    def test_records_only_paged_substages(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        timings = AssemblyTimings(
            path_kind="paged_img2pdf",
            source_stat_ms=0.0,
            copy_native_ms=0.0,
            discover_pages_ms=4.0,
            encode_pdf_ms=350.0,
            dst_stat_ms=1.0,
        )
        StagedPipeline._record_s4_substages(rec, timings)

        summary = rec._build_summary(  # type: ignore[attr-defined]
            pipeline="test", batch_id="b", total_docs=1, elapsed_s=1.0
        )
        names = set(summary.stages.keys())
        assert "S4.discover_pages" in names
        assert "S4.encode_pdf" in names
        assert "S4.dst_stat" in names
        assert "S4.copy_native" not in names
        assert "S4.source_stat" not in names


class TestZeroValuesSkipped:
    def test_all_zero_records_nothing(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        timings = AssemblyTimings(path_kind="native_pdf")  # todo en 0
        StagedPipeline._record_s4_substages(rec, timings)

        summary = rec._build_summary(  # type: ignore[attr-defined]
            pipeline="test", batch_id="b", total_docs=1, elapsed_s=1.0
        )
        assert summary.stages == {}


class TestValuePropagation:
    def test_duration_ms_propagates_correctly(self, tmp_path: Path) -> None:
        rec = _make_recorder(tmp_path)
        timings = AssemblyTimings(
            path_kind="native_pdf",
            copy_native_ms=42.5,
        )
        StagedPipeline._record_s4_substages(rec, timings)

        summary = rec._build_summary(  # type: ignore[attr-defined]
            pipeline="test", batch_id="b", total_docs=1, elapsed_s=1.0
        )
        copy_bucket = summary.stages["S4.copy_native"]
        assert copy_bucket["count"] == 1
        assert copy_bucket["sum_ms"] == 42.5
