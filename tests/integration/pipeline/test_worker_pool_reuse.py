"""Tests de los pools de threads de vida larga (119).

Pre-119 cada stage de prep (S2/S3/S4) y cada chunk de S5 creaba y
destruía su propio ThreadPoolExecutor — ~80 000 ciclos de spawn/join de
threads en una corrida de 20M docs, y la causa raíz de la fuga de
conexiones ODBC que 106 mitigó desde el adapter.

119: el StagedPipeline es dueño de pools lazy persistentes, reutilizados
por todos los chunks, con shutdown explícito al final de la corrida.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import respx

import cmcourier.orchestrators.staged as staged_module

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _write_trigger_csv(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    path = tmp_path / "triggers.csv"
    lines = ["ShortName,CIF,SystemID"]
    lines.extend(",".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def executor_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Cuenta las construcciones de ThreadPoolExecutor del módulo staged."""
    created: list[dict[str, Any]] = []

    class _SpyExecutor(ThreadPoolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(staged_module, "ThreadPoolExecutor", _SpyExecutor)
    return created


class TestPoolReuse:
    @respx.mock
    def test_run_creates_one_pool_per_role(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
        executor_spy: list[dict[str, Any]],
    ) -> None:
        """E1/E4: un run con prep_workers=2 construye exactamente 2
        executors (prep + S5) — pre-119 eran 4 (S2, S3, S4, S5)."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001", "TXN_PIPE_002"])
        triggers = _write_trigger_csv(
            tmp_path,
            [("TESTCLIENT01", "123456", "1"), ("TESTCLIENT02", "234567", "1")],
        )
        pipeline = pipeline_harness.build_pipeline(triggers, prep_workers=2)
        report = pipeline.run(source_descriptor=str(triggers))

        assert report.s5_done == 2
        prefixes = sorted(k.get("thread_name_prefix", "") for k in executor_spy)
        assert prefixes == ["cmcourier-prep", "cmcourier-s5"], (
            f"esperados 2 pools persistentes, se crearon {len(executor_spy)}: {prefixes}"
        )

    @respx.mock
    def test_prep_stage_reuses_pool_across_calls(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
        executor_spy: list[dict[str, Any]],
    ) -> None:
        """E2: dos invocaciones de _run_prep_stage → un solo executor."""
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        pipeline = pipeline_harness.build_pipeline(triggers, prep_workers=2)

        def worker(item: object) -> tuple[object | None, bool]:
            return item, False

        pipeline._run_prep_stage([], worker)  # noqa: SLF001
        pipeline._run_prep_stage([], worker)  # noqa: SLF001
        assert len(executor_spy) == 1
        pipeline.shutdown_worker_pools()

    @respx.mock
    def test_run_shuts_pools_down(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """E3: tras run(), los pools quedan cerrados y limpios."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        pipeline = pipeline_harness.build_pipeline(triggers, prep_workers=2)
        pipeline.run(source_descriptor=str(triggers))

        assert pipeline._prep_pool is None  # noqa: SLF001
        assert pipeline._s5_pool is None  # noqa: SLF001

    def test_shutdown_is_idempotent_and_pools_recreate(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        pipeline = pipeline_harness.build_pipeline(triggers, prep_workers=2)

        def worker(item: object) -> tuple[object | None, bool]:
            return item, False

        pipeline._run_prep_stage([], worker)  # noqa: SLF001
        first = pipeline._prep_pool  # noqa: SLF001
        assert first is not None
        pipeline.shutdown_worker_pools()
        pipeline.shutdown_worker_pools()  # idempotente
        assert pipeline._prep_pool is None  # noqa: SLF001

        pipeline._run_prep_stage([], worker)  # noqa: SLF001
        assert pipeline._prep_pool is not None  # noqa: SLF001
        assert pipeline._prep_pool is not first  # noqa: SLF001
        pipeline.shutdown_worker_pools()
