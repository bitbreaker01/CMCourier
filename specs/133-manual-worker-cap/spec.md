# 133 — Techo manual de workers en caliente desde el monitor

## Por qué

La ayuda de la consola anuncia `+/- workers` en `[6] MONITOR` desde 125,
pero nunca se implementó: `SessionOverrides.workers` es de lanzamiento y
el único que mueve el pool a mitad de corrida es el AIMD (025/036). Cuando
el operador ve que el CM está sufriendo (p95 de S5 subiendo, 503 en el
desglose) su única palanca es cancelar. Y cuando el AIMD está apagado no
hay palanca alguna.

## Qué

**REQ-001 — Techo del pipeline.** `StagedPipeline.set_worker_cap(cap)`
fija un techo manual (`None` lo quita) y `adjust_worker_cap(delta)` lo
mueve de a uno partiendo del presupuesto EFECTIVO actual. El presupuesto
que ve el pool es `min(aimd_total, user_cap)`, acotado a
`[1, _pool_ceiling()]`: `_on_pool_resize` (AIMD) ya no escribe al
semáforo/lane controller directo sino que actualiza `aimd_total` y
re-aplica el `min`. Sin AIMD, `aimd_total == cmis.workers`, así que el
techo sólo puede bajar (el `ThreadPoolExecutor` de S5 no tiene más
threads que `_pool_ceiling()`). Ambos devuelven el presupuesto efectivo
resultante; `worker_cap` lo expone.

**REQ-002 — Runner y teclas.** `ConsoleRunManager.adjust_workers(delta)
-> int | None` delega al pipeline (None sin corrida). En `[6]`, `+`
(también `=`) sube y `-` baja, sólo con corrida activa; la consola
notifica `workers: N (techo manual N · AIMD quería M)` o `workers: N ·
techo del pool` cuando pega contra `_pool_ceiling()`. Sin confirmación:
es reversible y acotado.

**REQ-003 — Monitor.** La cabecera de `[6]` muestra `workers <en uso>/<cap
efectivo>` y, si hay techo manual, ` · techo manual N`. La ayuda (`?`)
describe `+/-` como está en el código. La viñeta se va de "Qué NO hace
todavía" en la guía.

## Escenarios

**E1 —** Pipeline con `workers=4` sin AIMD: `adjust_worker_cap(-1)` →
3 y `concurrency_limit.capacity == 3`; `adjust_worker_cap(+5)` → 4 (techo
del pool). `set_worker_cap(None)` → vuelve a 4.

**E2 —** Con AIMD (`max_threads=8`, `workers=4`): `set_worker_cap(2)`; el
AIMD llama `_on_pool_resize(6)` → la capacidad efectiva sigue en 2;
`set_worker_cap(None)` → 6.

**E3 —** Modo dual-lane: el techo pasa por `LaneController.set_total_budget`
(no por el semáforo único).

**E4 —** Consola con corrida activa en `[6]`: `-` llama
`run_manager.adjust_workers(-1)` y la cabecera dice `techo manual 3`;
`+` fuera de `[6]` o sin corrida no hace nada.

## Notas de implementación

- El "presupuesto efectivo" se calcula en UN solo lugar
  (`_apply_worker_budget`) para que AIMD y operador nunca se pisen.
- `adjust_worker_cap` parte del efectivo actual, no del cap anterior: si el
  AIMD bajó a 3 y el operador aprieta `-`, espera 2, no `cap-1`.
- `_current_total_workers` (lo que lee el AIMD) sigue devolviendo el
  efectivo: con un techo bajo el AIMD "cree" que el pool es chico y
  propone `cap+1`; cuando el techo se levanta, el pool sube a eso. Es
  aceptable y acotado — documentado en el docstring.
- Dual-lane: `LaneController.set_total_budget` clampea a 2 (un slot por
  carril), así que el piso del efectivo ahí es 2, no 1. El efectivo que
  devuelven `set_worker_cap`/`adjust_worker_cap` refleja ese piso.
- La cabecera de `[6]` toma el cap efectivo de
  `pipeline.effective_workers` (vía `run_manager`) y no de
  `snap.pool_capacity`: este último lee el semáforo único, que en
  dual-lane no es el que manda (gap heredado de 036, también en la TUI
  clásica). `pool_in_use` sí es correcto en ambos modos (viene del pool).
