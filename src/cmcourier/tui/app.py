"""App `Textual` de cuatro tabs (025 fase 3 + 052).

El TUI corre en el `thread` principal; la pipeline corre en un `thread`
`worker`. La comunicación es unidireccional (el TUI hace `poll` del
provider cada ~250 ms). Al completarse el batch, el orchestrator llama
a ``TUIDataProvider.mark_batch_complete`` y la app congela el estado
final en pantalla hasta que el operador presiona ``[Q]``.

052 agrega un tab DETAIL: ``[`` / ``]`` mueven un cursor de `chunk`,
``d`` salta al tab, y el detalle por-doc del `chunk` seleccionado se
lee del `tracking store` bajo demanda.
"""

from __future__ import annotations

__all__ = ["CMCourierTUI"]

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from cmcourier.domain.models import DocDetail
from cmcourier.services.cancellation import CancellationToken
from cmcourier.tui.bucket_tab import render_bucket
from cmcourier.tui.chunks_tab import render_chunks
from cmcourier.tui.confirm_screen import ConfirmCancelScreen
from cmcourier.tui.data_provider import TUIDataProvider, TUISnapshot
from cmcourier.tui.detail_tab import render_detail
from cmcourier.tui.prep_tab import render_prep
from cmcourier.tui.upload_tab import render_upload

_REFRESH_INTERVAL_S: float = 0.25
_FOOTER_TEMPLATE = (
    "throughput {tps:.2f} docs/sec  elapsed {elapsed:02d}:{minutes:02d}:{seconds:02d}"
)


class CMCourierTUI(App[None]):
    """Dashboard live de cuatro tabs para una corrida de pipeline en vuelo."""

    TITLE = "CMCourier"
    BINDINGS = [
        Binding("p", "show_prep", "PREP"),
        Binding("u", "show_upload", "UPLOAD"),
        Binding("c", "show_chunks", "CHUNKS"),
        Binding("b", "show_bucket", "BUCKET"),
        Binding("d", "show_detail", "DETAIL"),
        Binding("[", "select_prev_chunk", "◀chunk"),
        Binding("]", "select_next_chunk", "chunk▶"),
        Binding("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    #status_bar {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    Static.tab_body {
        height: 1fr;
        padding: 0 1;
    }
    #detail_body {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        data_provider: TUIDataProvider,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        super().__init__()
        self._provider = data_provider
        # 097: token de cancelación cooperativa compartido con el
        # pipeline. ``None`` en tests o corridas sin token → ``"q"``
        # sale directo (comportamiento pre-097).
        self._cancel_token = cancel_token
        # 052: cursor de `chunk` para el tab DETAIL. ``None`` hasta que
        # el operador lo mueve con ``[`` / ``]``. ``_last_chunk_count`` se
        # refresca cada tick para que las acciones del cursor clampeen bien.
        self._selected_chunk_idx: int | None = None
        self._last_chunk_count = 0
        # 110: contador de ticks para la cadencia reducida del DETAIL, y
        # flag que fuerza su re-render inmediato (cursor movido / tab
        # recién activado).
        self._tick = 0
        self._detail_dirty = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        # 064: el tab BUCKET se monta incondicionalmente; el renderer
        # imprime un stub de una línea en modo `batched` apuntando a CHUNKS.
        # Mantener la lista de tabs estática entre modos evita re-componer
        # al cambiar de modo (algo que `Textual` no soporta post-mount).
        with TabbedContent(initial="prep"):
            with TabPane("PREP", id="prep"):
                yield Container(Static(id="prep_body", classes="tab_body"))
            with TabPane("UPLOAD", id="upload"):
                yield Container(Static(id="upload_body", classes="tab_body"))
            with TabPane("CHUNKS", id="chunks"):
                yield Container(Static(id="chunks_body", classes="tab_body"))
            with TabPane("BUCKET", id="bucket"):
                yield Container(Static(id="bucket_body", classes="tab_body"))
            with TabPane("DETAIL", id="detail"):
                # 058: `VerticalScroll` para que los `chunk`s más grandes
                # que el alto visible sean scrolleables. ``#detail_body``
                # tiene ``height: auto`` (ver CSS) para que el `Static`
                # interno crezca con su contenido y el padre haga scroll.
                yield VerticalScroll(Static(id="detail_body"))
        yield Static(id="status_bar")
        yield Footer()

    def on_mount(self) -> None:
        # 110: el mount pinta TODOS los tabs una vez (estado inicial);
        # de ahí en más, cada tick renderiza solo el activo.
        self._render_all_panels()
        self.set_interval(_REFRESH_INTERVAL_S, self._refresh_panels)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """110: cambiar de tab (tecla o click) pinta el panel al instante."""
        self._detail_dirty = True
        self._refresh_panels()

    def quit_needs_confirmation(self) -> bool:
        """097: True si ``"q"`` debe abrir el modal de confirmación.

        Solo cuando hay un token de cancelación, la corrida sigue en
        progreso, y todavía no se canceló. En cualquier otro caso
        ``"q"`` sale directo (corrida completa, sin token, o ya
        cancelada y drenando)."""
        if self._cancel_token is None or self._cancel_token.is_cancelled():
            return False
        return not self._provider.snapshot().is_complete

    async def action_quit(self) -> None:
        """097: ``"q"`` con la corrida en progreso pide confirmación
        antes de cancelar; en cualquier otro caso sale directo.

        ``async`` para matchear la firma de ``App.action_quit``."""
        if not self.quit_needs_confirmation():
            self.exit()
            return

        def _after_confirm(confirmed: bool | None) -> None:
            if confirmed:
                # El operador confirmó: prende el token (el pipeline
                # drena) y cierra el TUI — el runner espera el drain.
                assert self._cancel_token is not None
                self._cancel_token.cancel()
                self.exit()

        self.push_screen(ConfirmCancelScreen(), _after_confirm)

    def action_show_prep(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "prep"

    def action_show_upload(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "upload"

    def action_show_chunks(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "chunks"

    def action_show_bucket(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "bucket"

    def action_show_detail(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "detail"

    def action_select_prev_chunk(self) -> None:
        """052: mueve el cursor de `chunk` un paso hacia el primer `chunk`."""
        if self._last_chunk_count == 0:
            return
        if self._selected_chunk_idx is None:
            self._selected_chunk_idx = 0
        else:
            self._selected_chunk_idx = max(0, self._selected_chunk_idx - 1)
        self._render_detail(self._provider.snapshot())

    def action_select_next_chunk(self) -> None:
        """052: mueve el cursor de `chunk` un paso hacia el último `chunk`."""
        if self._last_chunk_count == 0:
            return
        if self._selected_chunk_idx is None:
            self._selected_chunk_idx = 0
        else:
            self._selected_chunk_idx = min(self._last_chunk_count - 1, self._selected_chunk_idx + 1)
        self._render_detail(self._provider.snapshot())

    def _resolve_detail(
        self, snap: TUISnapshot
    ) -> tuple[dict[str, object] | None, list[DocDetail]]:
        """052: resuelve el `chunk` seleccionado + su detalle por-doc para el
        panel DETAIL. Devuelve ``(None, [])`` cuando no hay `chunk` seleccionado."""
        if self._selected_chunk_idx is None:
            return None, []
        chunk = next(
            (
                c
                for c in snap.chunks_state
                if isinstance(c.get("chunk_idx"), int)
                and c["chunk_idx"] == self._selected_chunk_idx
            ),
            None,
        )
        if chunk is None:
            return None, []
        return chunk, self._provider.docs_for_batch(str(chunk.get("batch_id", "")))

    def _refresh_panels(self) -> None:
        """110: el tick periódico renderiza SOLO el tab activo (más el
        status bar y el subtítulo, que son globales). El DETAIL, aun
        activo, refresca a cadencia reducida (1 de cada 4 ticks) salvo
        que el cursor lo haya ensuciado — es un drill-down, no un gauge."""
        snap = self._provider.snapshot()
        self._tick += 1
        self._last_chunk_count = len(snap.chunks_state)
        active = self.query_one(TabbedContent).active
        if active == "prep":
            self.query_one("#prep_body", Static).update(render_prep(snap))
        elif active == "upload":
            self.query_one("#upload_body", Static).update(render_upload(snap))
        elif active == "chunks":
            self.query_one("#chunks_body", Static).update(render_chunks(snap))
        elif active == "bucket":
            self.query_one("#bucket_body", Static).update(render_bucket(snap))
        elif active == "detail" and (self._detail_dirty or self._tick % 4 == 1):
            self._render_detail(snap)
        self._render_status(snap)

    def _render_all_panels(self) -> None:
        """110: pintura inicial de todos los tabs en el mount."""
        snap = self._provider.snapshot()
        self._last_chunk_count = len(snap.chunks_state)
        self.query_one("#prep_body", Static).update(render_prep(snap))
        self.query_one("#upload_body", Static).update(render_upload(snap))
        self.query_one("#chunks_body", Static).update(render_chunks(snap))
        self.query_one("#bucket_body", Static).update(render_bucket(snap))
        self._render_detail(snap)
        self._render_status(snap)

    def _render_detail(self, snap: TUISnapshot) -> None:
        self.query_one("#detail_body", Static).update(render_detail(*self._resolve_detail(snap)))
        self._detail_dirty = False

    def _render_status(self, snap: TUISnapshot) -> None:
        status = self.query_one("#status_bar", Static)
        total = int(snap.elapsed_s)
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        status.update(
            f"batch {snap.batch_id or '—'}  pipeline {snap.pipeline}  "
            + _FOOTER_TEMPLATE.format(
                tps=snap.throughput_docs_per_s,
                elapsed=hours,
                minutes=minutes,
                seconds=seconds,
            )
        )

        # Actualiza ``App.sub_title`` con el estado de la corrida para que
        # aparezca en el header — le da al operador una vista de un
        # vistazo aunque esté enfocado en el cuerpo de un tab.
        self.sub_title = _sub_title(snap)


def _sub_title(snap: TUISnapshot) -> str:
    """Estado de un vistazo para ``App.sub_title``.

    144: durante la pasada final del reconciler (``snap.closing``) muestra
    ``cerrando · <label> k/N`` en lugar del conteo de workers.
    """
    if snap.is_complete:
        return "RUN COMPLETE — press Q to exit"
    closing = snap.closing
    if closing is not None:
        return f"cerrando · {closing.label} {closing.done}/{closing.total}"
    return f"{snap.pool_in_use}/{snap.pool_capacity} workers busy"
