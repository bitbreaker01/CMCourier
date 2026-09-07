"""Techo manual de workers en caliente (133).

El presupuesto que ve el pool es ``min(aimd_total, user_cap)`` acotado a
``[1, _pool_ceiling()]``; AIMD y operador escriben cada uno su variable y
un solo helper aplica el resultado al semáforo / lane controller.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cmcourier.config.schema import AutoTuneConfig, HeavyLightLanesConfig
from cmcourier.orchestrators.staged import StagedPipeline

pytestmark = pytest.mark.unit


def _make_pipeline(
    *,
    workers: int = 4,
    auto_tune: AutoTuneConfig | None = None,
    heavy_light_lanes: HeavyLightLanesConfig | None = None,
) -> StagedPipeline:
    return StagedPipeline(
        trigger_strategy=MagicMock(),
        indexing_service=MagicMock(),
        mapping_service=MagicMock(),
        metadata_service=MagicMock(),
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=MagicMock(),
        workers=workers,
        auto_tune=auto_tune,
        heavy_light_lanes=heavy_light_lanes,
    )


class TestWithoutAimd:
    def test_no_cap_by_default(self) -> None:
        p = _make_pipeline(workers=4)
        assert p.worker_cap is None
        assert p.concurrency_limit.capacity == 4

    def test_adjust_down_lowers_the_semaphore(self) -> None:
        p = _make_pipeline(workers=4)
        assert p.adjust_worker_cap(-1) == 3
        assert p.worker_cap == 3
        assert p.concurrency_limit.capacity == 3

    def test_adjust_up_is_bounded_by_pool_ceiling(self) -> None:
        p = _make_pipeline(workers=4)
        assert p.adjust_worker_cap(+5) == 4
        assert p.concurrency_limit.capacity == 4

    def test_never_below_one(self) -> None:
        p = _make_pipeline(workers=2)
        p.adjust_worker_cap(-1)
        assert p.adjust_worker_cap(-1) == 1
        assert p.adjust_worker_cap(-1) == 1
        assert p.concurrency_limit.capacity == 1

    def test_clearing_the_cap_restores_the_budget(self) -> None:
        p = _make_pipeline(workers=4)
        p.set_worker_cap(2)
        assert p.set_worker_cap(None) == 4
        assert p.worker_cap is None
        assert p.concurrency_limit.capacity == 4

    def test_adjust_starts_from_effective_not_previous_cap(self) -> None:
        p = _make_pipeline(workers=4)
        p.set_worker_cap(10)  # por encima del techo: efectivo 4
        assert p.adjust_worker_cap(-1) == 3


class TestWithAimd:
    def _aimd(self) -> StagedPipeline:
        return _make_pipeline(workers=4, auto_tune=AutoTuneConfig(enabled=True, max_threads=8))

    def test_aimd_resize_respects_manual_cap(self) -> None:
        p = self._aimd()
        p.set_worker_cap(2)
        p._on_pool_resize(6)  # noqa: SLF001 — hook del AIMD
        assert p.concurrency_limit.capacity == 2
        assert p.set_worker_cap(None) == 6
        assert p.concurrency_limit.capacity == 6

    def test_manual_cap_can_raise_up_to_ceiling_when_aimd_allows(self) -> None:
        p = self._aimd()
        p._on_pool_resize(8)  # noqa: SLF001
        assert p.adjust_worker_cap(-3) == 5
        assert p.adjust_worker_cap(+10) == 8

    def test_aimd_reads_the_effective_budget(self) -> None:
        p = self._aimd()
        p.set_worker_cap(2)
        assert p._current_total_workers() == 2  # noqa: SLF001

    def test_plus_pushes_the_aimd_budget_too(self) -> None:
        """Antagonista I1: el AIMD converge al cap+1 (lee el efectivo), así que
        subir sólo el techo era un no-op tras el primer paso. ``+`` empuja
        también el presupuesto del AIMD; ``-`` no lo toca."""
        p = self._aimd()
        p.set_worker_cap(2)
        p._on_pool_resize(3)  # noqa: SLF001 — el AIMD ya convergió a cap+1
        assert p.adjust_worker_cap(+1) == 3
        assert p.adjust_worker_cap(+1) == 4
        assert p.adjust_worker_cap(+1) == 5
        assert p.adjust_worker_cap(-2) == 3
        assert p.pool_ceiling == 8


class TestDualLane:
    def test_cap_goes_through_the_lane_controller(self) -> None:
        lanes = HeavyLightLanesConfig(enabled=True, heavy_threshold_bytes=1)
        p = _make_pipeline(workers=6, heavy_light_lanes=lanes)
        assert p.lane_controller is not None
        p.adjust_worker_cap(-2)
        assert p.lane_controller.snapshot().total_budget == 4

    def test_dual_lane_floor_is_two_one_slot_per_lane(self) -> None:
        lanes = HeavyLightLanesConfig(enabled=True, heavy_threshold_bytes=1)
        p = _make_pipeline(workers=4, heavy_light_lanes=lanes)
        assert p.lane_controller is not None
        assert p.set_worker_cap(1) == 2
        assert p.lane_controller.snapshot().total_budget == 2
        assert p.effective_workers == 2
