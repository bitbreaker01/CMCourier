"""Tests unitarios para el renderizador de la pestaña BUCKET (064)."""

from __future__ import annotations

import pytest

from cmcourier.orchestrators.streaming import StreamingSnapshot
from cmcourier.tui.bucket_tab import render_bucket
from cmcourier.tui.data_provider import TUISnapshot

pytestmark = pytest.mark.unit


def _streaming_snapshot(**kwargs: object) -> TUISnapshot:
    defaults = {
        "pipeline": "csv-trigger",
        "batch_id": "B1",
        "elapsed_s": 1.0,
        "throughput_docs_per_s": 0.0,
        "is_complete": False,
        "mode": "streaming",
        "bucket": StreamingSnapshot(
            bucket_level=4,
            bucket_cap=10,
            bucket_peak=8,
            prep_workers=4,
            prep_in_flight=2,
            upload_workers=8,
            prep_docs_per_s=12.0,
            upload_docs_per_s=10.5,
        ),
        "chunks_state": ({"s5_done": 50, "s5_failed": 1, "prep_skipped": 3},),
        "s1_filtered": 2,
        # 155: el bloque OUTCOMES lee el desglose por etapa del snapshot,
        # no sólo las dos columnas de S5 de las filas de `chunk`.
        "failed_total": 1,
        "failures_by_stage": {"S1": 0, "S2": 0, "S3": 0, "S4": 0, "S5": 1},
    }
    defaults.update(kwargs)
    return TUISnapshot(**defaults)  # type: ignore[arg-type]


class TestRenderBucket:
    def test_streaming_mode_renders_all_blocks(self) -> None:
        out = render_bucket(_streaming_snapshot())
        # Secciones
        assert "BUCKET" in out
        assert "THROUGHPUT" in out
        assert "WORKERS" in out
        assert "OUTCOMES" in out
        # Datos `live`
        assert "4 / 10" in out  # nivel
        assert "8 / 10" in out  # pico
        assert "12.00 docs/s" in out
        assert "10.50 docs/s" in out
        # Conteos `in-flight` + `worker`
        assert "2 in-flight / 4" in out
        assert "8" in out  # `worker`s de upload
        # Outcomes (acumulados). 155: el total de fallas de la corrida
        # reemplaza a la línea suelta de S5_FAILED; la etapa se detalla
        # indentada debajo, y sólo cuando tiene algo.
        assert "S5_DONE" in out
        assert "FALLIDOS" in out
        assert "S5_FAILED" in out
        assert "S1_FILTERED" in out
        assert "S1_SKIPPED" in out

    def test_batched_mode_emits_stub(self) -> None:
        snap = TUISnapshot(
            pipeline="x",
            batch_id="b",
            elapsed_s=0.0,
            throughput_docs_per_s=0.0,
            is_complete=False,
            mode="batched",
            bucket=None,
        )
        out = render_bucket(snap)
        assert "streaming mode only" in out

    def test_missing_bucket_in_streaming_mode_emits_stub(self) -> None:
        # Defensivo: modo `streaming` pero sin datos de `bucket` cableados.
        snap = TUISnapshot(
            pipeline="x",
            batch_id="b",
            elapsed_s=0.0,
            throughput_docs_per_s=0.0,
            is_complete=False,
            mode="streaming",
            bucket=None,
        )
        out = render_bucket(snap)
        assert "streaming mode only" in out

    def test_cumulative_outcomes_sum_correctly(self) -> None:
        snap = _streaming_snapshot()
        out = render_bucket(snap)
        # s5_done=50, s5_failed=1, s1_filtered=2 (TUISnapshot field),
        # s1_skipped=3 (chunks_state[0].prep_skipped)
        assert "50" in out
        assert "1" in out
        assert "2" in out
        assert "3" in out

    def test_renders_lane_block_when_lane_snapshot_present(self) -> None:
        from cmcourier.services.lane_controller import LaneSnapshot
        from cmcourier.services.worker_pool_stats import WorkerPoolStatsSnapshot

        heavy = WorkerPoolStatsSnapshot(
            pool_size=3,
            busy=1,
            idle=2,
            queue_depth=4,
            completed=12,
            failed=0,
        )
        light = WorkerPoolStatsSnapshot(
            pool_size=5,
            busy=2,
            idle=3,
            queue_depth=6,
            completed=33,
            failed=1,
        )
        bucket = StreamingSnapshot(
            bucket_level=2,
            bucket_cap=10,
            bucket_peak=8,
            prep_workers=4,
            prep_in_flight=1,
            upload_workers=8,
            prep_docs_per_s=5.0,
            upload_docs_per_s=4.0,
            lane_snapshot=LaneSnapshot(heavy=heavy, light=light, total_budget=8),
        )
        snap = _streaming_snapshot(bucket=bucket)
        out = render_bucket(snap)
        assert "LANES" in out
        assert "heavy" in out and "light" in out
        # `heavy` budget 3, busy 1, queue 4
        assert "3" in out and "1" in out
        # `total budget` 8
        assert "total budget 8" in out


class TestOutcomesPorEtapa155:
    """155 REQ-003 — `OUTCOMES` dice DÓNDE murieron los documentos."""

    def test_total_de_fallidos_con_las_etapas_que_tienen_algo(self) -> None:
        snap = _streaming_snapshot(
            chunks_state=({"s5_done": 0, "s5_failed": 0, "prep_skipped": 0},),
            s1_filtered=22618,
            failed_total=10,
            failures_by_stage={"S1": 0, "S2": 10, "S3": 0, "S4": 0, "S5": 0},
            # 157: las cuatro categorías. Acá sólo hay fallidos y excluidos.
            blocked_total=0,
            excluded_total=22618,
        )
        out = render_bucket(snap)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        block = lines[lines.index("OUTCOMES (cumulative)") :]
        assert block[2] == "  S5_DONE          0"
        assert block[3] == "  FALLIDOS        10"
        assert block[4] == "    S2_FAILED     10"
        # 157: BLOQUEADOS y EXCLUIDOS van entre las fallas y su detalle.
        assert block[5] == "  BLOQUEADOS       0"
        assert block[6] == "  EXCLUIDOS    22618"
        assert block[7] == "  S1_FILTERED  22618"
        assert block[8] == "  S1_SKIPPED       0"

    def test_las_etapas_sin_fallas_no_se_renderizan(self) -> None:
        # Nueve líneas en cero son ruido: sólo se muestra lo que pasó.
        out = render_bucket(
            _streaming_snapshot(
                failed_total=3,
                failures_by_stage={"S1": 0, "S2": 0, "S3": 3, "S4": 0, "S5": 0},
            )
        )
        assert "S3_FAILED" in out
        for stage in ("S1_FAILED", "S2_FAILED", "S4_FAILED", "S5_FAILED"):
            assert stage not in out

    def test_s5_done_y_el_total_siempre_se_renderizan_en_cero(self) -> None:
        out = render_bucket(
            _streaming_snapshot(
                chunks_state=({"s5_done": 0, "s5_failed": 0, "prep_skipped": 0},),
                failed_total=0,
                failures_by_stage={"S1": 0, "S2": 0, "S3": 0, "S4": 0, "S5": 0},
            )
        )
        assert "  S5_DONE          0" in out
        assert "  FALLIDOS         0" in out
        assert "_FAILED" not in out

    def test_varias_etapas_se_ordenan_de_s1_a_s5(self) -> None:
        out = render_bucket(
            _streaming_snapshot(
                failed_total=6,
                failures_by_stage={"S1": 1, "S2": 0, "S3": 2, "S4": 0, "S5": 3},
            )
        )
        rendered = [ln.strip().split()[0] for ln in out.splitlines() if "_FAILED" in ln]
        assert rendered == ["S1_FAILED", "S3_FAILED", "S5_FAILED"]


class TestOutcomesCuatroCategorias157:
    """157 REQ-004 — OUTCOMES separa fallidos / bloqueados / excluidos."""

    def test_bloqueados_y_excluidos_se_rinden_siempre(self) -> None:
        out = render_bucket(_streaming_snapshot(failed_total=0, blocked_total=0, excluded_total=0))
        assert "BLOQUEADOS" in out
        assert "EXCLUIDOS" in out

    def test_una_corrida_solo_de_bloqueados_no_muestra_fallas(self) -> None:
        out = render_bucket(
            _streaming_snapshot(
                chunks_state=({"s5_done": 0, "s5_failed": 0, "prep_skipped": 0},),
                s1_filtered=0,
                failed_total=0,
                failures_by_stage={"S1": 0, "S2": 0, "S3": 0, "S4": 0, "S5": 0},
                blocked_total=7,
                excluded_total=0,
            )
        )
        lines = [ln for ln in out.splitlines() if ln.strip()]
        block = lines[lines.index("OUTCOMES (cumulative)") :]
        assert "  FALLIDOS         0" in block
        assert "  BLOQUEADOS       7" in block
        assert "_FAILED" not in out
