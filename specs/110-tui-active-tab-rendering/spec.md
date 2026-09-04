# 110 — TUI: renderizar solo el tab activo

## Por qué

`_refresh_panels` (`tui/app.py:205-217`) renderiza **los cinco tabs en
cada tick** de 0.25 s, sin mirar cuál está visible:

* `render_prep`, `render_upload`, `render_chunks`, `render_bucket` —
  formateo Rich completo de paneles que nadie está viendo.
* `render_detail(*self._resolve_detail(snap))` — lo peor: con un chunk
  seleccionado, `_resolve_detail` dispara `docs_for_batch` →
  `list_docs_for_batch` (una query SQL de hasta `batch_size` filas, 4
  veces por segundo) y después formatea hasta 2000 líneas… que se
  descartan si el tab DETAIL no está visible.

Post-107 la query usa índice y no bloquea a los workers, pero el
trabajo de formateo (×5 tabs ×4 Hz) sigue siendo CPU del thread
principal del TUI tirada a la basura.

## Qué

### Requisitos

**REQ-001 — Solo el tab activo se renderiza en el tick.** El refresh
periódico consulta `TabbedContent.active` y actualiza únicamente el
body de ese tab (más status bar y subtítulo, que son globales).

**REQ-002 — Cambiar de tab renderiza inmediato.** Las acciones
`action_show_*` refrescan el panel al cambiar (nada de mirar contenido
viejo hasta 250 ms). Ídem las acciones de cursor `[` / `]`: refrescan el
DETAIL al moverse (los tests pilot existentes dependen de esto).

**REQ-003 — DETAIL a 1 Hz.** Aun activo, el panel DETAIL se
re-renderiza cada 4 ticks (1 Hz) — es una tabla de drill-down, no un
gauge en vivo. Un movimiento de cursor o el cambio de tab fuerzan el
render inmediato.

**REQ-004 — Sin cambio visual.** El contenido de cada panel es idéntico;
solo cambia cuándo se computa.

### Fuera de alcance

* La cadencia global del TUI (0.25 s queda igual).
* Cambios en los renderers.

## Escenarios

**E1 — Tab PREP activo: los otros renderers no corren.**
Dado el TUI con PREP activo,
cuando pasa un tick,
entonces solo `render_prep` se ejecuta (además del status bar).

**E2 — DETAIL de fondo no consulta SQLite.**
Dado un chunk seleccionado y el tab UPLOAD activo,
cuando pasan N ticks,
entonces `docs_for_batch` no se llama ni una vez.

**E3 — El cambio de tab pinta al instante.**
Dado el TUI en PREP,
cuando el operador presiona `u`,
entonces el panel UPLOAD se renderiza en esa misma acción.

**E4 — Cursor de chunk refresca el DETAIL.**
Dado el tab DETAIL activo,
cuando el operador presiona `]`,
entonces el detalle del chunk nuevo aparece sin esperar al tick.
