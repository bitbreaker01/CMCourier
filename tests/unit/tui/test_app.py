"""Tests `pilot` (`run_test()`) para ``CMCourierTUI`` — panel DETAIL + cursor
de `chunk` (052)."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from textual.widgets import Static

from cmcourier.domain.models import DocDetail
from cmcourier.tui.app import CMCourierTUI
from cmcourier.tui.data_provider import TUISnapshot

pytestmark = pytest.mark.unit


class _FakeProvider:
    """Provider mínimo para tests `pilot` de la app — dos `chunk`s, detalle
    por-doc fijo indexado por `batch_id`."""

    def __init__(self) -> None:
        self._docs = {
            "B0": [DocDetail("T0", "T0.001", "S5_DONE", "", 100)],
            "B1": [DocDetail("T1", "T1.001", "S5_FAILED", "boom", 200)],
        }

    def snapshot(self) -> TUISnapshot:
        return TUISnapshot(
            pipeline="rvabrep-trigger",
            batch_id="B0",
            elapsed_s=1.0,
            throughput_docs_per_s=0.0,
            is_complete=False,
            chunks_state=(
                {"chunk_idx": 0, "batch_id": "B0", "status": "DONE"},
                {"chunk_idx": 1, "batch_id": "B1", "status": "UPLOAD"},
            ),
        )

    def docs_for_batch(self, batch_id: str) -> list[DocDetail]:
        return self._docs.get(batch_id, [])


def _detail_text(app: CMCourierTUI) -> str:
    return str(app.query_one("#detail_body", Static).renderable)


class TestDetailPaneSelection:
    def test_cursor_moves_and_detail_renders_selected_chunk(self) -> None:
        async def _run() -> None:
            app = CMCourierTUI(_FakeProvider())  # type: ignore[arg-type]
            async with app.run_test() as pilot:
                # Nada seleccionado todavía → el panel DETAIL muestra el prompt.
                assert "no chunk selected" in _detail_text(app)

                # ] selecciona el `chunk` 0.
                await pilot.press("]")
                await pilot.pause()
                assert app._selected_chunk_idx == 0  # noqa: SLF001
                app._refresh_panels()  # noqa: SLF001 — renderizado determinístico
                body = _detail_text(app)
                assert "chunk 0" in body
                assert "T0" in body

                # ] otra vez → `chunk` 1, con su doc fallido + razón.
                await pilot.press("]")
                await pilot.pause()
                assert app._selected_chunk_idx == 1  # noqa: SLF001
                app._refresh_panels()  # noqa: SLF001
                body = _detail_text(app)
                assert "T1" in body
                assert "boom" in body

                # ] en el último `chunk` se clampa — no se va más allá del final.
                await pilot.press("]")
                await pilot.pause()
                assert app._selected_chunk_idx == 1  # noqa: SLF001

                # [ vuelve al `chunk` 0.
                await pilot.press("[")
                await pilot.pause()
                assert app._selected_chunk_idx == 0  # noqa: SLF001

        asyncio.run(_run())

    def test_d_key_switches_to_detail_tab(self) -> None:
        async def _run() -> None:
            from textual.widgets import TabbedContent

            app = CMCourierTUI(_FakeProvider())  # type: ignore[arg-type]
            async with app.run_test() as pilot:
                await pilot.press("d")
                await pilot.pause()
                assert app.query_one(TabbedContent).active == "detail"

        asyncio.run(_run())


class TestSubTitleClosing144:
    """144: el header del TUI batched refleja la fase de cierre."""

    @staticmethod
    def _snap(**kw: object) -> TUISnapshot:
        return TUISnapshot(
            pipeline="p",
            batch_id="B0",
            elapsed_s=1.0,
            throughput_docs_per_s=0.0,
            is_complete=False,
            pool_in_use=2,
            pool_capacity=8,
            **kw,  # type: ignore[arg-type]
        )

    def test_sub_title_shows_closing_phase(self) -> None:
        from cmcourier.services.worker_pool_stats import ClosingPhase
        from cmcourier.tui.app import _sub_title

        snap = self._snap(closing=ClosingPhase("sincronizando AS400", 50, 120))
        assert _sub_title(snap) == "cerrando · sincronizando AS400 50/120"

    def test_sub_title_without_closing_keeps_workers_busy(self) -> None:
        from cmcourier.tui.app import _sub_title

        assert _sub_title(self._snap()) == "2/8 workers busy"

    def test_sub_title_complete_wins_over_closing(self) -> None:
        from cmcourier.services.worker_pool_stats import ClosingPhase
        from cmcourier.tui.app import _sub_title

        snap = self._snap(closing=ClosingPhase("sincronizando AS400", 1, 1))
        snap = dataclasses.replace(snap, is_complete=True)
        assert _sub_title(snap).startswith("RUN COMPLETE")


class TestDetailPaneScroll058:
    def test_detail_body_is_inside_a_vertical_scroll(self) -> None:
        # 058: el panel DETAIL es el único que puede producir más
        # contenido del que cabe en pantalla. Su cuerpo debe vivir
        # dentro de un ``VerticalScroll`` para que el operador pueda
        # leer más allá del fold.
        async def _run() -> None:
            from textual.containers import VerticalScroll

            app = CMCourierTUI(_FakeProvider())  # type: ignore[arg-type]
            async with app.run_test():
                detail_body = app.query_one("#detail_body", Static)
                assert isinstance(detail_body.parent, VerticalScroll)

        asyncio.run(_run())
