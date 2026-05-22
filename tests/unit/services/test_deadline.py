"""Tests de :class:`DeadlineWatchdog` (103, REQ-002/004/007)."""

from __future__ import annotations

import logging
import time

import pytest

from cmcourier.services.cancellation import CancellationToken
from cmcourier.services.deadline import DeadlineWatchdog

pytestmark = pytest.mark.unit


def _wait_until(predicate: object, timeout_s: float = 2.0) -> bool:
    """Poll ``predicate`` hasta que sea truthy o se agote *timeout_s*."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return bool(predicate())  # type: ignore[operator]


def test_new_watchdog_is_not_fired() -> None:
    wd = DeadlineWatchdog(CancellationToken(), duration_s=10.0)
    assert wd.fired is False


def test_watchdog_fires_and_cancels_token() -> None:
    """REQ-002: vencido el plazo, se prende el CancellationToken."""
    token = CancellationToken()
    wd = DeadlineWatchdog(token, duration_s=0.05)
    wd.start()
    try:
        assert _wait_until(lambda: wd.fired) is True
        assert token.is_cancelled() is True
    finally:
        wd.stop()


def test_stop_before_deadline_does_not_fire() -> None:
    """REQ-007: si el pipeline termina antes, stop() impide el disparo."""
    token = CancellationToken()
    wd = DeadlineWatchdog(token, duration_s=30.0)
    wd.start()
    wd.stop()
    time.sleep(0.1)
    assert wd.fired is False
    assert token.is_cancelled() is False


def test_stop_after_fire_is_safe() -> None:
    """REQ-007: stop() tras el disparo es inofensivo."""
    token = CancellationToken()
    wd = DeadlineWatchdog(token, duration_s=0.05)
    wd.start()
    assert _wait_until(lambda: wd.fired) is True
    wd.stop()  # no debe levantar
    assert wd.fired is True


def test_stop_without_start_is_safe() -> None:
    wd = DeadlineWatchdog(CancellationToken(), duration_s=10.0)
    wd.stop()  # no debe levantar


def test_fire_emits_structured_event(caplog: pytest.LogCaptureFixture) -> None:
    """REQ-004: el disparo emite un evento estructurado."""
    token = CancellationToken()
    wd = DeadlineWatchdog(token, duration_s=0.05)
    with caplog.at_level(logging.WARNING, logger="cmcourier.services.deadline"):
        wd.start()
        assert _wait_until(lambda: wd.fired) is True
        wd.stop()
    events = [
        r for r in caplog.records if getattr(r, "event", None) == "pipeline_stopped_by_deadline"
    ]
    assert len(events) == 1
    assert events[0].max_duration_s == pytest.approx(0.05)
