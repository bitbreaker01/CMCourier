# 144 — Sync AS400 con progreso y batcheo; cierre de corrida visible

## Por qué

Dos observaciones del operador (2026-09-09, corrida `streaming` de 3000
docs en 760 s; `[8] SYNC` recover sobre 2000 registros con 1000
faltantes):

1. **El cierre de la corrida es mudo y lento.** Tras el último upload el
   monitor queda en "corriendo" con `3000/3030 docs · 30 pending ·
   idle 30` durante minutos. Los 30 "docs" son las `_POISON` de los
   consumers contadas por `qsize()` (`streaming.py:293-307`) y nunca
   republicadas al retirarlas (`:706`). El tiempo real lo consume la
   pasada FINAL del reconciliador (`PeriodicReconciler.stop()` →
   `run_pass`, `reconciler.py:169-186`): **un write + commit por doc, en
   serie, en un solo hilo** contra AS400, sin ningún reporte. La consola
   marca "completada" recién cuando `orchestrator.run()` retorna
   (`runner.py:224`).
2. **`sync recover` es un N+1 sin señal de vida.** La existencia en
   NIARVILOG ya es batcheada (118), pero por cada doc faltante
   `_recover_missing` (`recovery.py:127,136`) hace un `SELECT` sobre
   `(query RVABREP) AS T` (~120 ms) y en apply un `INSERT` + commit
   (~180 ms): 1000 faltantes = 2 min dry-run / 5 min apply. Ni el
   servicio, ni `sync_ops`, ni `SyncPane` ni el CLI emiten progreso.

## Requisitos

- **REQ-001 Recover batcheado.** `IndexingService.find_documents_by_txns(txns)
  -> dict[str, RVABREPDocument]` usa `IDataSource.get_by_fields_in`
  (IN chunkeado, 113). `As400Recovery.recover` lee los RVABREP de TODOS
  los faltantes en esa llamada (⌈N/1000⌉ viajes) y nunca llama
  `find_document_by_txn` por doc. Los INSERT de apply corren en
  paralelo con un pool acotado (`write_workers`, default 8; cada hilo
  tiene su conexión vía `ThreadLocalConnectionPool`), preservando la
  semántica por fila (IntegrityError → `RecoveryItem`, un txn malo no
  aborta el resto).
- **REQ-002 Progreso del recover.** `As400Recovery.recover(...,
  on_progress: Callable[[SyncProgress], None] | None = None)` con
  `SyncProgress(phase: str, done: int, total: int)` (frozen dataclass en
  `services/recovery.py`). Fases: `"leyendo tracking"`, `"consultando
  NIARVILOG"`, `"consultando RVABREP"`, `"insertando"` (sólo apply).
  Se emite al empezar cada fase y, en `insertando`, cada 50 docs y al
  final. Un callback que levanta no aborta el recover (se loguea).
  `sync_ops.sync_recover` lo pasa tal cual.
- **REQ-003 Progreso en consola y CLI.** `SyncPane` muestra en su log
  una línea por evento (`⋯ insertando 250/1000`), marshalada al hilo
  UI con `ConsoleApp._apply_on_ui`; la línea de la misma fase se
  reemplaza (no se acumulan 20 líneas por fase). `cmcourier sync
  recover` imprime el progreso en stderr (una línea por evento).
- **REQ-004 Cierre de corrida visible.** `WorkerPoolStats` gana una
  fase de cierre (`set_closing(label, done, total)` / `clear_closing()`;
  `WorkerPoolStatsSnapshot.closing: ClosingPhase | None`). El
  orquestador (streaming, batched, multi_batch) la setea alrededor de
  `periodic_reconciler.stop(on_progress=...)`: el reconciliador
  reporta `SyncProgress("sincronizando AS400", k, N)` cada 50 items y
  al final. El monitor (`tui/` + `monitor_pane`) muestra `cerrando ·
  sincronizando AS400 k/N` en lugar de "corriendo" mientras `closing`
  no sea `None`.
- **REQ-005 Pasada del reconciliador en paralelo.** `As400Reconciler.run_pass`
  propaga los items con el mismo pool acotado (`write_workers`, default
  8), preservando por item: `stop_event` cooperativo (lo no despachado
  va a `requeued`), aislamiento de fallas (`requeued` + `_log_failure`),
  conflictos, y el read batcheado previo. El orden de `synced_to_as400`
  no importa.
- **REQ-006 Cola sin píldoras.** `_publish_pending_count` no cuenta
  `_POISON`: el consumer republica al retirar la píldora, y el total
  publicado descuenta las píldoras encoladas. Tras el último doc el
  monitor muestra `3000/3000 · 0 pending`.
- **REQ-007** `docs/reference/cli.md` (sync recover con progreso),
  `docs/explanation/operations-console.md` (fase de cierre),
  `docs/reference/config-reference.yaml` sólo si se agrega config;
  CHANGELOG `[Unreleased]`.
