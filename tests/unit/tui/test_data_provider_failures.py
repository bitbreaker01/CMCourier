"""155 — ``failed_total`` cuenta las fallas terminales de TODAS las etapas.

Hasta 155 el campo salía de ``MetricsRecorder.failure_breakdown()``, que
es el desglose de S5 de la spec 104: una falla de S1..S4 no pasaba por
ahí y el monitor mostraba ``0 fallidos`` en verde con documentos muertos
en el medio del pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.config.schema import AutoTuneConfig, CmisConfigModel
from cmcourier.observability.metrics import MetricsRecorder
from cmcourier.orchestrators.multi_batch import ChunkState
from cmcourier.services.worker_pool_stats import ResizableSemaphore, WorkerPoolStats
from cmcourier.tui.data_provider import TUIDataProvider

pytestmark = pytest.mark.unit


def _provider(
    tmp_path: Path,
    chunks: list[Any] | None = None,
) -> tuple[TUIDataProvider, MetricsRecorder]:
    recorder = MetricsRecorder(
        log_dir=tmp_path / "logs",
        slow_op_threshold_ms=0.0,
        slow_op_top_n=10,
    )
    cmis = CmisConfigModel(
        base_url="http://cmis.bank.test:9080/cmis",
        repo_id="$x!t",
        workers=4,
        max_bandwidth_mbps=0.0,
        auto_tune=AutoTuneConfig(enabled=False),
    )
    uploader = MagicMock()
    uploader._timeout_s = 300.0
    provider = TUIDataProvider(
        pipeline_name="csv-trigger",
        metrics_recorder=recorder,
        pool_stats=WorkerPoolStats(),
        concurrency_limit=ResizableSemaphore(4),
        cmis_config=cmis,
        uploader=uploader,
        chunks_provider=(lambda: list(chunks or [])),
    )
    return provider, recorder


def _chunk(**kwargs: Any) -> ChunkState:
    base: dict[str, Any] = {"chunk_idx": 0, "batch_id": "B1", "status": "DONE"}
    base.update(kwargs)
    return ChunkState(**base)


class TestFailedTotalCuentaTodasLasEtapas:
    """155 REQ-002 — el total de la corrida deja de ser el total de S5."""

    def test_suma_las_fallas_de_todas_las_etapas(self, tmp_path: Path) -> None:
        provider, _rec = _provider(
            tmp_path,
            [_chunk(s1_failed=1, s2_failed=10, s3_failed=2, s4_failed=3, s5_failed=4)],
        )
        snap = provider.snapshot()
        assert snap.failed_total == 20
        assert snap.failures_by_stage == {"S1": 1, "S2": 10, "S3": 2, "S4": 3, "S5": 4}

    def test_una_falla_solo_de_prep_se_cuenta(self, tmp_path: Path) -> None:
        # El caso del operador: diez documentos muertos en S2, cero uploads.
        provider, _rec = _provider(tmp_path, [_chunk(s2_failed=10)])
        snap = provider.snapshot()
        assert snap.failed_total == 10
        assert snap.failures_by_stage["S2"] == 10
        assert snap.failures_by_stage["S5"] == 0

    def test_suma_entre_chunks(self, tmp_path: Path) -> None:
        provider, _rec = _provider(
            tmp_path,
            [
                _chunk(chunk_idx=0, s2_failed=2, s5_failed=1),
                _chunk(chunk_idx=1, s4_failed=3),
            ],
        )
        assert provider.snapshot().failed_total == 6

    def test_corrida_limpia_queda_en_cero(self, tmp_path: Path) -> None:
        provider, _rec = _provider(tmp_path, [_chunk(s5_done=50)])
        snap = provider.snapshot()
        assert snap.failed_total == 0
        assert snap.failures_by_stage == {"S1": 0, "S2": 0, "S3": 0, "S4": 0, "S5": 0}

    def test_sin_chunks_cae_al_contador_de_upload(self, tmp_path: Path) -> None:
        # Path monolítico raro (sin filas de chunk): S5 es lo único que hay.
        provider, recorder = _provider(tmp_path, [])
        recorder.record_upload_failed("http_5xx", 503)
        recorder.record_upload_failed("timeout")
        snap = provider.snapshot()
        assert snap.failed_total == 2
        assert snap.failures_by_stage["S5"] == 2


class TestDesgloseDeUploadIntacto:
    """155 REQ-002 — el desglose de 104 sigue siendo de S5 y sólo de S5."""

    def test_el_desglose_por_tipo_no_ve_las_fallas_de_prep(self, tmp_path: Path) -> None:
        provider, recorder = _provider(tmp_path, [_chunk(s2_failed=10, s5_failed=2)])
        recorder.record_upload_failed("http_5xx", 503)
        recorder.record_upload_failed("timeout")
        snap = provider.snapshot()
        assert snap.failures_by_type == {"http_5xx": 1, "timeout": 1}
        assert snap.failures_by_status == {503: 1}
        # El header del bloque ERRORS BY TYPE (UPLOAD) usa este total, no
        # el de la corrida: 12 fallidos en total, 2 de ellos de upload.
        assert snap.upload_failed_total == 2
        assert snap.failed_total == 12
