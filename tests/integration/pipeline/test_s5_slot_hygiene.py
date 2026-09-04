"""Tests del pre-flight de S5 fuera del slot del semáforo (109).

Pre-109, ``_upload_one`` adquiría el slot del semáforo y RECIÉN después
hacía ``is_stage_done`` + ``mark_stage_pending`` + ``try_claim`` (1-2
round-trips AS400 en modo claim) — trabajo de coordinación quemando
presupuesto de concurrencia de upload. Los skips (ya subido, claim
perdido) consumían un slot completo para no subir nada.

109: el pre-flight corre antes del ``acquire``; el slot cubre solo el
upload real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import respx

from cmcourier.domain.models import StageStatus

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _write_trigger_csv(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    path = tmp_path / "triggers.csv"
    lines = ["ShortName,CIF,SystemID"]
    lines.extend(",".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n")
    return path


class _RecordingCoordinator:
    """Fake del IdempotencyCoordinator que registra el orden de llamadas."""

    def __init__(self, events: list[str], *, claim_result: bool = True) -> None:
        self._events = events
        self._claim_result = claim_result

    def try_claim(self, **kwargs: Any) -> bool:
        self._events.append("try_claim")
        return self._claim_result

    def mark_uploaded(self, **kwargs: Any) -> None:
        self._events.append("mark_uploaded")

    def mark_failed(self, **kwargs: Any) -> None:
        self._events.append("mark_failed")


def _instrument(pipeline: Any, events: list[str]) -> None:
    """Espía el acquire del semáforo y el is_stage_done de S5."""
    real_acquire = pipeline._concurrency_limit.acquire  # noqa: SLF001

    def spy_acquire() -> None:
        events.append("acquire")
        real_acquire()

    pipeline._concurrency_limit.acquire = spy_acquire  # noqa: SLF001

    store = pipeline._tracking_store  # noqa: SLF001
    real_done = store.is_stage_done

    def spy_done(txn: str, batch_id: str, stage: StageStatus) -> bool:
        if stage is StageStatus.S5_DONE:
            events.append("is_stage_done_s5")
        return real_done(txn, batch_id, stage)

    store.is_stage_done = spy_done


class TestSlotHygiene:
    @respx.mock
    def test_preflight_runs_before_acquire(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """E1: is_stage_done → try_claim → acquire, en ese orden."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        pipeline = pipeline_harness.build_pipeline(triggers)

        events: list[str] = []
        _instrument(pipeline, events)
        pipeline._coordinator = _RecordingCoordinator(events)  # noqa: SLF001

        report = pipeline.run(source_descriptor=str(triggers))
        assert report.s5_done == 1

        assert "acquire" in events and "try_claim" in events
        assert events.index("is_stage_done_s5") < events.index("acquire"), (
            f"is_stage_done corrió con el slot tomado: {events}"
        )
        assert events.index("try_claim") < events.index("acquire"), (
            f"try_claim corrió con el slot tomado: {events}"
        )

    @respx.mock
    def test_lost_claim_does_not_consume_slot(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """E3: claim perdido → skipped, sin acquire."""
        pipeline_harness.register_cmis_for_docs([])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])
        pipeline = pipeline_harness.build_pipeline(triggers)

        events: list[str] = []
        _instrument(pipeline, events)
        pipeline._coordinator = _RecordingCoordinator(events, claim_result=False)  # noqa: SLF001

        report = pipeline.run(source_descriptor=str(triggers))
        assert report.s5_done == 0
        assert "try_claim" in events
        assert "acquire" not in events, (
            f"un claim perdido no debe consumir slot de upload: {events}"
        )

    @respx.mock
    def test_already_uploaded_does_not_consume_slot(self, pipeline_harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """E2: doc ya S5_DONE en el mismo batch → done, sin acquire."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [("TESTCLIENT01", "123456", "1")])

        # Primera corrida: sube el doc.
        first = pipeline_harness.build_pipeline(triggers)
        report = first.run(source_descriptor=str(triggers))
        assert report.s5_done == 1
        pipeline_harness.tracking_store.flush()

        # Resume del MISMO batch desde S5: el doc ya está S5_DONE.
        second = pipeline_harness.build_pipeline(triggers)
        events: list[str] = []
        _instrument(second, events)

        resumed = second.run(
            source_descriptor=str(triggers),
            batch_id=report.batch_id,
            from_stage=5,
        )
        assert resumed.s5_done == 1
        assert "is_stage_done_s5" in events
        assert "acquire" not in events, (
            f"un doc ya subido no debe consumir slot de upload: {events}"
        )
