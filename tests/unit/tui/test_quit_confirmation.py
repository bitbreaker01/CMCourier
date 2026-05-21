"""Tests del flujo de confirmación al cancelar con "q" (097)."""

from __future__ import annotations

import asyncio

import pytest

from cmcourier.services.cancellation import CancellationToken
from cmcourier.tui.app import CMCourierTUI
from cmcourier.tui.confirm_screen import ConfirmCancelScreen
from cmcourier.tui.data_provider import TUISnapshot

pytestmark = pytest.mark.unit


class _FakeProvider:
    """Provider mínimo con ``is_complete`` configurable."""

    def __init__(self, *, is_complete: bool) -> None:
        self._is_complete = is_complete

    def snapshot(self) -> TUISnapshot:
        return TUISnapshot(
            pipeline="rvabrep-trigger",
            batch_id="B0",
            elapsed_s=1.0,
            throughput_docs_per_s=0.0,
            is_complete=self._is_complete,
            chunks_state=(),
        )

    def docs_for_batch(self, batch_id: str) -> list:  # noqa: ARG002
        return []


# ---------------------------------------------------------------------------
# quit_needs_confirmation — lógica pura
# ---------------------------------------------------------------------------


class TestQuitNeedsConfirmation:
    def test_no_token_never_confirms(self) -> None:
        app = CMCourierTUI(_FakeProvider(is_complete=False))  # type: ignore[arg-type]
        assert app.quit_needs_confirmation() is False

    def test_run_in_progress_with_token_confirms(self) -> None:
        app = CMCourierTUI(
            _FakeProvider(is_complete=False),  # type: ignore[arg-type]
            cancel_token=CancellationToken(),
        )
        assert app.quit_needs_confirmation() is True

    def test_run_complete_does_not_confirm(self) -> None:
        app = CMCourierTUI(
            _FakeProvider(is_complete=True),  # type: ignore[arg-type]
            cancel_token=CancellationToken(),
        )
        assert app.quit_needs_confirmation() is False

    def test_already_cancelled_does_not_confirm(self) -> None:
        token = CancellationToken()
        token.cancel()
        app = CMCourierTUI(
            _FakeProvider(is_complete=False),  # type: ignore[arg-type]
            cancel_token=token,
        )
        assert app.quit_needs_confirmation() is False


# ---------------------------------------------------------------------------
# Flujo end-to-end con el pilot de textual
# ---------------------------------------------------------------------------


class TestQuitConfirmationFlow:
    def test_q_in_progress_opens_confirm_modal(self) -> None:
        async def _run() -> None:
            token = CancellationToken()
            app = CMCourierTUI(
                _FakeProvider(is_complete=False),  # type: ignore[arg-type]
                cancel_token=token,
            )
            async with app.run_test() as pilot:
                await pilot.press("q")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmCancelScreen)
                assert token.is_cancelled() is False  # todavía no se confirmó

        asyncio.run(_run())

    def test_confirm_cancels_token_and_exits(self) -> None:
        async def _run() -> None:
            token = CancellationToken()
            app = CMCourierTUI(
                _FakeProvider(is_complete=False),  # type: ignore[arg-type]
                cancel_token=token,
            )
            async with app.run_test() as pilot:
                await pilot.press("q")
                await pilot.pause()
                await pilot.press("s")  # confirma
                await pilot.pause()
            assert token.is_cancelled() is True

        asyncio.run(_run())

    def test_decline_keeps_running_and_does_not_cancel(self) -> None:
        async def _run() -> None:
            token = CancellationToken()
            app = CMCourierTUI(
                _FakeProvider(is_complete=False),  # type: ignore[arg-type]
                cancel_token=token,
            )
            async with app.run_test() as pilot:
                await pilot.press("q")
                await pilot.pause()
                await pilot.press("n")  # rechaza
                await pilot.pause()
                # El modal se cerró, la app sigue viva, el token intacto.
                assert not isinstance(app.screen, ConfirmCancelScreen)
                assert token.is_cancelled() is False
                assert app.is_running

        asyncio.run(_run())

    def test_q_when_complete_exits_without_modal(self) -> None:
        async def _run() -> None:
            token = CancellationToken()
            app = CMCourierTUI(
                _FakeProvider(is_complete=True),  # type: ignore[arg-type]
                cancel_token=token,
            )
            async with app.run_test() as pilot:
                await pilot.press("q")
                await pilot.pause()
            # Salió directo: nunca se abrió el modal ni se canceló nada.
            assert token.is_cancelled() is False

        asyncio.run(_run())
