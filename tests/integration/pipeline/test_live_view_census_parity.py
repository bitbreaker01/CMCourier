"""155 REQ-005 — el candado entre la vista en vivo y el censo.

> **Lo que la vista en vivo cuenta al terminar tiene que coincidir con lo
> que ``batch show`` cuenta después.** Si difieren, la vista en vivo está
> mintiendo — la base es la verdad.

Sin este test la próxima etapa que se agregue al `pipeline` vuelve a
quedar afuera de ``TUISnapshot.failed_total`` y nadie se entera hasta que
un operador mira ``0 fallidos`` en verde con diez documentos muertos.

La corrida rompe las CINCO etapas a la vez. 157: el sufijo del status sigue
al balde, así que la vista en vivo separa las cuatro categorías y el candado
las compara TODAS contra el censo:

* ``NO_SUCH_CLIENT`` — sin filas en RVABREP → ``S1_FAILED``  (fallido)
* ``TESTUNMAPPED``   — IDRVI sin mapeo      → ``S2_BLOCKED`` (bloqueado)
* ``TESTMETAFAIL``   — CIF fuera de clients → ``S3_BLOCKED`` (bloqueado)
* ``TESTMISSFILES``  — página inexistente   → ``S4_FAILED``  (fallido)
* ``TESTCLIENT01``   — CMIS responde 400    → ``S5_FAILED``  (fallido)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from cmcourier.config.schema import (
    AutoTuneConfig,
    CmisConfigModel,
    ObservabilityConfig,
    PipelineConfig,
    ProcessingConfig,
    StreamingConfig,
)
from cmcourier.domain.models import BatchDetails
from cmcourier.orchestrators.staged import StagedPipeline
from cmcourier.orchestrators.streaming import StreamingOrchestrator
from cmcourier.services.worker_pool_stats import ResizableSemaphore
from cmcourier.tui.data_provider import TUIDataProvider, TUISnapshot

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_CMIS_BASE_URL = "http://cmis.example.test:9080/opencmcmis/browser"
_CMIS_REPO_ID = "$x!testrepo"

_BROKEN_ROWS: list[tuple[str, str, str]] = [
    ("NO_SUCH_CLIENT", "123456", "1"),
    ("TESTUNMAPPED", "123456", "1"),
    ("TESTMETAFAIL", "999999", "1"),
    ("TESTMISSFILES", "123456", "1"),
    ("TESTCLIENT01", "123456", "1"),
]


def _write_trigger_csv(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    path = tmp_path / "triggers.csv"
    lines = ["ShortName,CIF,SystemID"]
    lines.extend(",".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n")
    return path


def _stub_cmis_rejecting_uploads() -> None:
    """Warmup + carpeta OK; el `upload` en sí devuelve 400 (fail-fast)."""
    respx.get(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}").mock(
        return_value=httpx.Response(
            200, json={"repositoryId": _CMIS_REPO_ID, "productName": "IBM Content Manager"}
        )
    )
    respx.post(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}/root").mock(
        return_value=httpx.Response(201, json={"ok": True})
    )
    respx.post(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}/root/$type/BAC_04_01_01_01_01").mock(
        return_value=httpx.Response(400, json={"error": "bad request"})
    )


def _build_orchestrator(pipeline: StagedPipeline, tmp_path: Path) -> StreamingOrchestrator:
    cfg = MagicMock(spec=PipelineConfig)
    cfg.observability = ObservabilityConfig(log_dir=tmp_path)
    cfg.processing = ProcessingConfig(
        mode="streaming",
        streaming=StreamingConfig(bucket_size=8),
        prep_workers=1,
    )
    return StreamingOrchestrator(pipeline=pipeline, config=cfg, log_dir=tmp_path)


def _build_provider(
    pipeline: StagedPipeline, orchestrator: StreamingOrchestrator
) -> TUIDataProvider:
    """El MISMO cableado que ``cli/console/runner.py`` arma para el monitor."""
    return TUIDataProvider(
        pipeline_name=pipeline.pipeline_name,
        metrics_recorder=pipeline.metrics_recorder,
        pool_stats=pipeline.pool_stats,
        concurrency_limit=ResizableSemaphore(4),
        cmis_config=CmisConfigModel(
            base_url=_CMIS_BASE_URL,
            repo_id=_CMIS_REPO_ID,
            workers=4,
            max_bandwidth_mbps=0.0,
            auto_tune=AutoTuneConfig(enabled=False),
        ),
        uploader=pipeline.uploader,
        recorder_provider=orchestrator.active_recorder,
        upload_recorder_provider=orchestrator.upload_recorder,
        chunks_provider=orchestrator.chunks_snapshot,
        tracking_store=pipeline.tracking_store,
        mode="streaming",
        bucket_provider=orchestrator.streaming_snapshot,
    )


def _census_failures(details: BatchDetails) -> int:
    """Lo que ``batch show`` / el panel ``[7] BATCHES`` llaman "fallidos"."""
    return sum(states.get("FAILED", 0) for states in details.stage_counts.values())


def _census_blocked(details: BatchDetails) -> int:
    """157: los ``*_BLOCKED`` del censo."""
    return sum(states.get("BLOCKED", 0) for states in details.stage_counts.values())


def _census_excluded(details: BatchDetails) -> int:
    """157: los ``*_EXCLUDED`` + ``S1_FILTERED`` + ``S1_SKIPPED`` del censo."""
    return sum(
        states.get("EXCLUDED", 0) + states.get("FILTERED", 0) + states.get("SKIPPED", 0)
        for states in details.stage_counts.values()
    )


class TestLaVistaEnVivoCoincideConElCenso:
    """155 REQ-001 — la invariante, atada con un test."""

    @respx.mock
    def test_failed_total_iguala_las_fallas_del_censo(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        _stub_cmis_rejecting_uploads()
        triggers = _write_trigger_csv(tmp_path, _BROKEN_ROWS)
        pipeline = pipeline_harness.build_pipeline(triggers)
        orchestrator = _build_orchestrator(pipeline, tmp_path)
        provider = _build_provider(pipeline, orchestrator)

        report = orchestrator.run(
            source_descriptor=str(triggers), batch_size=10, batches_in_flight=1
        )
        batch_id = report.chunks[0].batch_id
        pipeline_harness.tracking_store.flush()

        snap: TUISnapshot = provider.snapshot()
        details = pipeline_harness.tracking_store.get_batch_details(batch_id)
        assert details is not None

        # 157: el escenario tocó las cinco etapas, pero S2 y S3 son BLOQUEOS
        # (falta config), no fallas — así que ya no cuentan en failures_by_stage.
        assert snap.failures_by_stage == {"S1": 1, "S2": 0, "S3": 0, "S4": 1, "S5": 1}
        # …y la invariante extendida a 157: la vista en vivo cuenta lo mismo
        # que la base, en las CUATRO categorías.
        assert snap.failed_total == _census_failures(details) == 3
        assert snap.blocked_total == _census_blocked(details) == 2
        assert snap.excluded_total == _census_excluded(details) == 0
        # El desglose de 104 sigue siendo de upload y sólo de upload.
        assert snap.upload_failed_total == 1
        assert sum(snap.failures_by_type.values()) == 1

    @respx.mock
    def test_una_corrida_limpia_tambien_coincide(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        # El candado tiene que valer para el cero: un falso positivo en la
        # corrida sana es tan mentiroso como un falso negativo en la rota.
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001", "TXN_PIPE_002"])
        triggers = _write_trigger_csv(
            tmp_path,
            [("TESTCLIENT01", "123456", "1"), ("TESTCLIENT02", "234567", "1")],
        )
        pipeline = pipeline_harness.build_pipeline(triggers)
        orchestrator = _build_orchestrator(pipeline, tmp_path)
        provider = _build_provider(pipeline, orchestrator)

        report = orchestrator.run(
            source_descriptor=str(triggers), batch_size=10, batches_in_flight=1
        )
        batch_id = report.chunks[0].batch_id
        pipeline_harness.tracking_store.flush()

        snap = provider.snapshot()
        details = pipeline_harness.tracking_store.get_batch_details(batch_id)
        assert details is not None
        assert snap.failed_total == _census_failures(details) == 0
        assert snap.blocked_total == _census_blocked(details) == 0
        assert snap.excluded_total == _census_excluded(details) == 0
