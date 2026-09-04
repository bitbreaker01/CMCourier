"""Tests de la restitución de capacidad de lanes (115).

Pre-115 el rebalance era de un solo sentido: una lane drenada 15 s
quedaba con capacidad 1 y la única vía de vuelta era que la OTRA lane
se vaciara 15 s — con flujo continuo, nunca. Un hueco transitorio sin
docs heavy dejaba la lane heavy procesando de a 1 el resto de la
corrida.

115: el controller recuerda la lane drenada y restituye el split
inicial apenas ``set_queue_depth`` le reporta trabajo nuevo.
"""

from __future__ import annotations

import pytest

from cmcourier.services.lane_controller import LaneController

pytestmark = pytest.mark.unit


def _build(
    *,
    total: int = 10,
    ratio: float = 0.2,
    idle_threshold_s: float = 5.0,
) -> tuple[LaneController, list[float]]:
    clock = [0.0]

    def now() -> float:
        return clock[0]

    ctl = LaneController(
        total_budget=total,
        heavy_initial_ratio=ratio,
        rebalance_interval_s=1.0,
        idle_threshold_s=idle_threshold_s,
        clock=now,
    )
    return ctl, clock


def _drain_heavy(ctl: LaneController, clock: list[float]) -> None:
    ctl.set_queue_depth("heavy", 0)
    clock[0] += 10.0
    ctl.rebalance_tick()


class TestRestitution:
    def test_work_arrival_restores_initial_split(self) -> None:
        # E1: heavy 2/light 8 → drenaje → 1/10 → llega trabajo heavy →
        # 2/8 inmediato, sin esperar tick.
        ctl, clock = _build(total=10, ratio=0.2)
        assert (ctl.heavy_capacity, ctl.light_capacity) == (2, 8)

        _drain_heavy(ctl, clock)
        assert (ctl.heavy_capacity, ctl.light_capacity) == (1, 10)

        ctl.set_queue_depth("heavy", 3)
        assert (ctl.heavy_capacity, ctl.light_capacity) == (2, 8), (
            "la lane drenada debe recuperar su capacidad al llegar trabajo"
        )

    def test_light_drain_restores_symmetrically(self) -> None:
        ctl, clock = _build(total=10, ratio=0.5)
        ctl.set_queue_depth("light", 0)
        clock[0] = 10.0
        ctl.rebalance_tick()
        assert (ctl.heavy_capacity, ctl.light_capacity) == (10, 1)

        ctl.set_queue_depth("light", 1)
        assert (ctl.heavy_capacity, ctl.light_capacity) == (5, 5)

    def test_restitution_uses_current_aimd_budget(self) -> None:
        # El AIMD subió el budget durante la migración: la restitución
        # respeta el total vigente, con el ratio inicial.
        ctl, clock = _build(total=10, ratio=0.2)
        _drain_heavy(ctl, clock)
        ctl.set_total_budget(20)
        ctl.set_queue_depth("heavy", 1)
        assert ctl.heavy_capacity + ctl.light_capacity == 20
        assert ctl.heavy_capacity == 4  # ceil(20 × 0.2)

    def test_zero_depth_updates_do_not_restore(self) -> None:
        # Reportar 0 en la lane drenada no dispara restitución.
        ctl, clock = _build(total=10, ratio=0.2)
        _drain_heavy(ctl, clock)
        ctl.set_queue_depth("heavy", 0)
        assert (ctl.heavy_capacity, ctl.light_capacity) == (1, 10)

    def test_other_lane_depth_does_not_restore(self) -> None:
        # Trabajo en la lane NO drenada no restituye nada.
        ctl, clock = _build(total=10, ratio=0.2)
        _drain_heavy(ctl, clock)
        ctl.set_queue_depth("light", 4)
        assert (ctl.heavy_capacity, ctl.light_capacity) == (1, 10)

    def test_drain_migrate_restore_cycle_is_repeatable(self) -> None:
        ctl, clock = _build(total=10, ratio=0.2)
        for cycle in range(3):
            _drain_heavy(ctl, clock)
            assert ctl.heavy_capacity == 1, f"ciclo {cycle}"
            ctl.set_queue_depth("heavy", 1)
            assert (ctl.heavy_capacity, ctl.light_capacity) == (2, 8), f"ciclo {cycle}"
            clock[0] += 100.0
