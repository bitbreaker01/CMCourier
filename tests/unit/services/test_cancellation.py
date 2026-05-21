"""Tests de :class:`CancellationToken` (097)."""

from __future__ import annotations

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
