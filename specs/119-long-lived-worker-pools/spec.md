# 119 — Pools de threads de vida larga en el StagedPipeline

## Por qué

El hallazgo P20 de la auditoría — el churn de `ThreadPoolExecutor`:

* `_run_prep_stage` (`staged.py:764-768`) crea y destruye un pool de
  `prep_workers` threads **por cada stage (S2, S3, S4) de cada chunk**.
* `_stage_5_single` (`:1039-1042`) crea y destruye un pool de
  `_pool_ceiling()` threads (hasta 50) **por chunk**.
* `_stage_5_dual` (`:1104-1112`) crea y destruye DOS pools de ceiling
  **por chunk**.

Con `batch_size: 1000` y un corpus de 20M de docs son 20 000 chunks ×
(3 pools de prep + 1-2 de S5) = **~80 000 ciclos de creación/join de
threads** en una corrida. Cada ciclo paga el spawn de hasta 50-100
threads del SO y su teardown.

Este churn fue además la **causa raíz** de la fuga de conexiones ODBC
(106): los threads morían por chunk y sus conexiones thread-local
quedaban huérfanas. 106 lo mitigó con poda desde el adapter; 119
elimina la causa — con threads persistentes, las conexiones
thread-local viven la corrida entera y la poda pasa a ser una red de
seguridad.

## Qué

### Requisitos

**REQ-001 — Pools lazy y persistentes.** El `StagedPipeline` es dueño
de hasta cuatro executors creados en el primer uso y reutilizados por
todos los chunks/stages de la corrida: prep (`prep_workers`), S5 single
(`ceiling`), S5 heavy y S5 light (`ceiling` cada uno). La creación es
thread-safe (lock).

**REQ-002 — `shutdown_worker_pools()`.** Método idempotente que cierra
(wait=True) y limpia los pools creados. Lo llaman en `finally`:
`StagedPipeline.run()` (path monolítico/resume),
`MultiBatchOrchestrator.run()` y `StreamingOrchestrator.run()`
(inofensivo si nunca se crearon). Tras el shutdown, un uso posterior
recrea los pools (el pipeline sigue siendo reutilizable).

**REQ-003 — Semántica de dispatch intacta.** `pool.map` en prep
(orden preservado), `submit` + `as_completed` en S5, el semáforo/AIMD
acotando la concurrencia real, y los contadores de stats no cambian.
El dimensionamiento tampoco (mismo `max_workers` que hoy).

### Fuera de alcance

* El start/stop del thread de rebalance del `LaneController` por chunk
  (un thread por chunk — ruido menor frente a los 50-100 de los pools).
* El `ThreadPoolExecutor` efímero de `warm_connection_pool` (2 usos por
  corrida).

## Escenarios

**E1 — Una corrida crea a lo sumo un pool por rol.** Un run con
`prep_workers: 2` construye exactamente 2 executors (prep + S5), no 4
(pre-119: S2, S3, S4 y S5 por separado).

**E2 — Reuso cross-chunk.** Dos invocaciones de `_run_prep_stage`
reutilizan el mismo executor.

**E3 — Shutdown limpio.** Tras `run()`, los pools quedan cerrados y
limpios; un uso posterior recrea.

**E4 — Resultados idénticos.** El happy path de 2 docs produce el mismo
RunReport que pre-119.
