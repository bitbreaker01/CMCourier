# 128 — Consola: pestaña SYNC (SQLite ↔ AS400 NIARVILOG)

## Por qué

El operador no encontraba dónde correr a mano la sincronización del
tracking SQLite con la tabla NIARVILOG del AS400. Existe como CLI
(`cmcourier sync status | resolve | recover`, 034/099) pero no en la
consola, y la lógica vive pegada a click (`sys.exit`, `click.echo`),
así que no se podía reutilizar desde Textual sin duplicarla.

## Qué

**REQ-001 — Operaciones puras.** Nuevo módulo `cli/sync_ops.py` con la
lógica de negocio del sync sin click: `sync_unavailable_reason(config,
secrets) -> str | None` (sync deshabilitado / sin `connection` / sin
credenciales AS400), `build_sync_stores`, `sync_status` (cleanup de
in_progress vencidos + conectividad), `sync_recover(batch_id=,
apply=)` y `sync_resolve(txn=, prefer=, cm_object_id=)`. Los errores de
uso levantan `SyncOpError` (mensaje para el operador); los de AS400
propagan `As400CoordinationError`. Los stores se cierran siempre.
El CLI `sync` pasa a usar estas funciones — mismos mensajes y exit
codes (2 config, 1 uso, 3 AS400).

**REQ-002 — Pestaña `8·SYNC`** (tecla `8` / `F8`). Al activarla se
re-evalúa la disponibilidad: si el YAML no tiene
`tracking.as400_sync.enabled: true` (o falta `connection`), la
pantalla lo dice en claro y los controles quedan deshabilitados; si
faltan las credenciales AS400 de sesión, manda a [2]. Tres bloques:

| bloque | controles | efecto |
| --- | --- | --- |
| estado | botón `estado (s)` | `sync_status` en worker thread; muestra `stale_cleaned=N` y la conectividad |
| recuperar | `batch_id` (opcional), `simular` , `aplicar` | `simular` = dry-run (siempre primero); `aplicar` solo se habilita tras un dry-run del MISMO batch_id con filas a recuperar, y pide confirmación (modal danger; en `prd` exige tipear `PRD`) |
| resolver | `txn`, preferencia (`as400` / `local`), `cm_object_id` (solo local), botón `resolver` | `local` escribe en AS400 → confirmación; `as400` es read-only |

Todo corre en worker thread (la UI no se congela); los resultados y
errores se acumulan en un panel de salida (`markup=False`).

**REQ-003 — Ayuda y guía.** La ayuda (`?`) lista la pestaña 8 y sus
teclas. La guía `docs/how-to/probar-la-consola.md` documenta la
pestaña (y que en el entorno local, sin AS400, aparece deshabilitada
con el motivo).

## Escenarios

**E1 —** `sync_unavailable_reason` devuelve motivo con sync
deshabilitado, con `connection` faltante y con credenciales vacías;
`None` cuando todo está.
**E2 —** `sync_resolve(prefer="local")` sin `cm_object_id` levanta
`SyncOpError`; con txn inexistente en AS400 levanta `SyncOpError`; con
fila presente llama `mark_uploaded_by_txn` y devuelve el mensaje.
**E3 —** Con el YAML local (sync deshabilitado) la pestaña 8 muestra el
motivo y los botones están deshabilitados.
**E4 —** Con sync habilitado y credenciales cargadas, `aplicar` está
deshabilitado hasta que un dry-run del mismo batch_id reporta filas;
entonces pide confirmación y, al confirmar, llama `sync_recover(apply=True)`.
**E5 —** El CLI `sync` conserva sus exit codes y mensajes (tests
existentes en verde).
