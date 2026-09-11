"""148 REQ-004 — los ``except BaseException`` de streaming dejan de ser mudos.

Este es, de paso, el arreglo de los ~200 uploads que el operador perdió:
la excepción no-CMIS se contaba en el tally y no persistía NADA, y como
``mark_stage_pending`` es ``INSERT OR IGNORE`` el documento se quedaba
registrado como ``S4_DONE``. Con el censo ese camino escribe ``CRASHED``
y deja de ser invisible.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cmcourier.domain.models import ReasonCode

from .test_streaming import _build_orch, _FakePipeline, _make_triggers

pytestmark = pytest.mark.unit


class _CrashingPipeline(_FakePipeline):
    """``_FakePipeline`` que revienta en los índices pedidos."""

    def __init__(
        self,
        *,
        prep_raises: tuple[int, ...] = (),
        upload_raises: tuple[int, ...] = (),
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._prep_raises = set(prep_raises)
        self._upload_raises = set(upload_raises)
        self.prep_crashes: list[tuple[Any, str]] = []
        self.upload_crashes: list[tuple[Any, str]] = []

    def streaming_prep_one(self, trigger: Any, batch_id: str, recorder: Any) -> Any:
        idx = self._idx_for_trigger.get(id(trigger), -1)
        if idx in self._prep_raises:
            raise MemoryError("algo no contemplado")  # no-CMIS a propósito
        return super().streaming_prep_one(trigger, batch_id, recorder)

    def streaming_upload_one(
        self, item: Any, batch_id: str, recorder: Any, lane: Any = None
    ) -> Any:
        if item.__idx__ in self._upload_raises:
            raise MemoryError("algo no contemplado")
        return super().streaming_upload_one(item, batch_id, recorder, lane)

    def record_prep_crash(self, trigger: Any, batch_id: str, exc: BaseException) -> None:
        self.prep_crashes.append((trigger, batch_id))

    def record_upload_crash(self, item: Any, batch_id: str, exc: BaseException) -> None:
        self.upload_crashes.append((item.document.txn_num, batch_id))


class TestStreamingCrashCensus148:
    def test_prep_crash_is_recorded(self, tmp_path: Path) -> None:
        triggers = _make_triggers(3)
        pipeline = _CrashingPipeline(triggers=triggers, prep_raises=(1,), pool_ceiling=2)
        orch = _build_orch(pipeline, tmp_path, bucket_size=4, prep_workers=1)

        orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)

        assert [t.shortname for t, _b in pipeline.prep_crashes] == ["SN_1"]

    def test_upload_crash_is_recorded(self, tmp_path: Path) -> None:
        triggers = _make_triggers(3)
        pipeline = _CrashingPipeline(triggers=triggers, upload_raises=(2,), pool_ceiling=2)
        orch = _build_orch(pipeline, tmp_path, bucket_size=4, prep_workers=1)

        report = orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)

        assert pipeline.upload_crashes == [("TXN_2", "B1")]
        assert report.chunks[0].s5_failed == 1  # el tally sigue intacto

    def test_upload_crash_is_recorded_in_dual_lane_too(self, tmp_path: Path) -> None:
        """El handler del `_lane_upload_loop` es OTRO bloque de código: si
        sólo se arregla el single-lane, el bug sobrevive con lanes puestas."""
        triggers = _make_triggers(3)
        pipeline = _CrashingPipeline(triggers=triggers, upload_raises=(0, 1, 2), pool_ceiling=2)
        orch = _build_orch(pipeline, tmp_path, bucket_size=4, prep_workers=1, lanes_enabled=True)

        orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)

        assert {txn for txn, _b in pipeline.upload_crashes} == {"TXN_0", "TXN_1", "TXN_2"}

    def test_a_crash_while_recording_the_crash_does_not_kill_the_worker(
        self, tmp_path: Path
    ) -> None:
        """El registro del censo es best-effort: si el tracking está caído,
        la corrida sigue — nunca al revés."""
        triggers = _make_triggers(2)
        pipeline = _CrashingPipeline(triggers=triggers, upload_raises=(0, 1), pool_ceiling=2)
        pipeline.record_upload_crash = _boom  # type: ignore[method-assign]
        orch = _build_orch(pipeline, tmp_path, bucket_size=4, prep_workers=1)

        report = orch.run(source_descriptor="", batch_size=10, batches_in_flight=2)

        assert report.chunks[0].s5_failed == 2


def _boom(*_a: Any, **_k: Any) -> None:
    raise RuntimeError("tracking caído")


def test_fake_item_shape_matches_what_the_recorder_needs() -> None:
    """Candado del doble: el recorder lee ``item.document.txn_num``."""
    item = SimpleNamespace(document=SimpleNamespace(txn_num="TXN_0"))
    assert item.document.txn_num == "TXN_0"
    assert ReasonCode.CRASHED.value == "CRASHED"
