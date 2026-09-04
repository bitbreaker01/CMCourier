"""Tests del renderizado por tab activo (110).

Pre-110 ``_refresh_panels`` renderizaba los CINCO tabs en cada tick de
0.25 s — incluido el DETAIL, que con un chunk seleccionado disparaba
``docs_for_batch`` (query SQL) 4 veces por segundo para formatear hasta
2000 líneas que nadie estaba viendo.

110: el tick renderiza solo el tab activo; cambiar de tab o mover el
cursor de chunk pinta inmediato; el DETAIL activo refresca a 1 Hz.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static, TabbedContent

from cmcourier.domain.models import DocDetail
from cmcourier.tui.app import CMCourierTUI
from cmcourier.tui.data_provider import TUISnapshot

pytestmark = pytest.mark.unit


class _CountingProvider:
    """Provider que cuenta las llamadas a docs_for_batch (la query SQL)."""

    def __init__(self) -> None:
        self.docs_calls = 0

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
        self.docs_calls += 1
        return [DocDetail("T0", "T0.001", "S5_DONE", "", 100)]


class TestActiveTabOnly:
    def test_background_detail_does_not_query_sqlite(self) -> None:
        """E2: chunk seleccionado + tab UPLOAD activo → cero queries."""

        async def _run() -> None:
            provider = _CountingProvider()
            app = CMCourierTUI(provider)  # type: ignore[arg-type]
            async with app.run_test() as pilot:
                await pilot.press("]")  # selecciona chunk 0 (renderiza detail)
                await pilot.press("u")  # cambia a UPLOAD
                await pilot.pause()
                provider.docs_calls = 0
                for _ in range(8):
                    app._refresh_panels()  # noqa: SLF001
                assert provider.docs_calls == 0, "el DETAIL de fondo no debe consultar SQLite"

        asyncio.run(_run())

    def test_inactive_tab_renderers_do_not_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """E1: con PREP activo, los renderers de los otros tabs no corren."""

        async def _run() -> None:
            import cmcourier.tui.app as app_module

            calls: list[str] = []
            for name in ("render_prep", "render_upload", "render_chunks", "render_bucket"):
                real = getattr(app_module, name)

                def spy(snap: object, *, _n: str = name, _r: object = real) -> object:
                    calls.append(_n)
                    return _r(snap)  # type: ignore[operator]

                monkeypatch.setattr(app_module, name, spy)

            app = CMCourierTUI(_CountingProvider())  # type: ignore[arg-type]
            async with app.run_test() as pilot:
                await pilot.pause()
                calls.clear()
                for _ in range(4):
                    app._refresh_panels()  # noqa: SLF001
                assert calls == ["render_prep"] * 4, (
                    f"solo el tab activo debe renderizarse: {calls}"
                )

        asyncio.run(_run())

    def test_switching_tab_renders_immediately(self) -> None:
        """E3: presionar `u` pinta el panel UPLOAD sin esperar al tick."""

        async def _run() -> None:
            app = CMCourierTUI(_CountingProvider())  # type: ignore[arg-type]
            async with app.run_test() as pilot:
                await pilot.press("u")
                await pilot.pause()
                body = str(app.query_one("#upload_body", Static).renderable)
                assert body, "el panel UPLOAD debe pintarse al activarse"
                assert app.query_one(TabbedContent).active == "upload"

        asyncio.run(_run())

    def test_active_detail_refreshes_at_reduced_cadence(self) -> None:
        """REQ-003: DETAIL activo refresca cada 4 ticks, no en todos."""

        async def _run() -> None:
            provider = _CountingProvider()
            app = CMCourierTUI(provider)  # type: ignore[arg-type]
            async with app.run_test() as pilot:
                await pilot.press("]")  # selecciona chunk
                await pilot.press("d")  # activa DETAIL
                await pilot.pause()
                provider.docs_calls = 0
                for _ in range(8):
                    app._refresh_panels()  # noqa: SLF001
                assert 1 <= provider.docs_calls <= 3, (
                    f"DETAIL activo debe refrescar ~1 de cada 4 ticks, "
                    f"hizo {provider.docs_calls} queries en 8 ticks"
                )

        asyncio.run(_run())
