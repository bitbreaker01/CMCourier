"""Tests de :class:`CancellationToken` (097)."""

from __future__ import annotations

import threading

import pytest

from cmcourier.services.cancellation import CancellationToken

pytestmark = pytest.mark.unit


def test_new_token_is_not_cancelled() -> None:
    assert CancellationToken().is_cancelled() is False


def test_cancel_flips_the_flag() -> None:
    token = CancellationToken()
    token.cancel()
    assert token.is_cancelled() is True


def test_cancel_is_idempotent() -> None:
    token = CancellationToken()
    token.cancel()
    token.cancel()
    assert token.is_cancelled() is True


# ------------------------------------------------------------- 132: pausa


def test_new_token_is_not_paused_and_checkpoint_passes() -> None:
    token = CancellationToken()
    assert token.is_paused() is False
    assert token.checkpoint() is True


def test_checkpoint_returns_false_when_already_cancelled() -> None:
    token = CancellationToken()
    token.cancel()
    assert token.checkpoint() is False


def test_pause_blocks_checkpoint_until_resume() -> None:
    token = CancellationToken()
    token.pause()
    assert token.is_paused() is True
    results: list[bool] = []
    started = threading.Event()

    def waiter() -> None:
        started.set()
        results.append(token.checkpoint())

    t = threading.Thread(target=waiter)
    t.start()
    started.wait(1.0)
    t.join(0.2)
    assert t.is_alive(), "checkpoint() debe bloquear mientras el token está pausado"
    assert results == []
    token.resume()
    t.join(2.0)
    assert results == [True]
    assert token.is_paused() is False


def test_cancel_wakes_a_paused_waiter_with_false() -> None:
    token = CancellationToken()
    token.pause()
    results: list[bool] = []
    t = threading.Thread(target=lambda: results.append(token.checkpoint()))
    t.start()
    t.join(0.2)
    assert t.is_alive()
    token.cancel()
    t.join(2.0)
    assert results == [False]
    assert token.is_cancelled() is True


def test_pause_and_resume_are_idempotent() -> None:
    token = CancellationToken()
    token.pause()
    token.pause()
    assert token.is_paused() is True
    token.resume()
    token.resume()
    assert token.is_paused() is False
    assert token.checkpoint() is True


def test_pause_after_cancel_does_not_block() -> None:
    token = CancellationToken()
    token.cancel()
    token.pause()
    assert token.checkpoint() is False
