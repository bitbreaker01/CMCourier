# 111 — Dieta de logging en el hot path

## Por qué

Dos emisores de logs por-documento/por-MiB identificados por la
auditoría, ambos en el camino crítico:

### A. Un record de logging por MiB transmitido

`_PROGRESS_THRESHOLD_BYTES = 1_048_576` (`cmis_uploader.py:77`): el
callback de progreso emite un evento `cmis_upload_progress` a
`cmcourier.metrics.network` **por cada MiB**. Cada evento paga:
`json.dumps` + escritura sincrónica a `network-{date}.jsonl` +
`_BandwidthHandler.emit` (lock del sampler + limpieza de ventana) +
`_SlowOpHandler.emit`. A 100 MB/s agregados son ~100 escrituras a disco
por segundo **desde los worker threads de S5**, bajo GIL.

El propósito del evento (077) es que el chart de bandwidth del TUI se
mueva durante uploads largos. Granularidad de 1 MiB es ~8× más fina de
lo que un chart de 60 buckets de 1 s necesita.

### B. Tres variantes de log INFO por consulta del document cache

`document_cache.py:108-143` loguea a INFO cada `try_get` (hit, miss
absent, miss expired) — uno por documento por corrida con el cache
activo. Cada record atraviesa el filtro PII (itera el `__dict__`
completo), el JsonFormatter (~44 campos de whitelist) y el
RotatingFileHandler, más el `_SlowOpHandler` enganchado a `cmcourier`.
Es telemetría de depuración, no operativa.

### Lo que NO se toca

`stage_complete` (metrics.py, `StageTimer.__exit__`) queda a INFO: es
la fuente del agregador de slow-ops (T4) y del análisis por stage.
Bajarlo rompería `diagnose` y el top-N de operaciones lentas.

## Qué

**REQ-001 — Threshold de progreso a 8 MiB.**
`_PROGRESS_THRESHOLD_BYTES` pasa de 1 MiB a 8 MiB: 8× menos eventos,
mismas curvas en el chart (60 buckets de 1 s; el evento de completion
sigue acreditando el remanente). Un upload de 5 MB pasa de ~5 eventos a
1 (el completion) — su ancho de banda se acredita igual, distribuido
sobre la ventana `[started, completed]` como siempre.

**REQ-002 — Logs del document cache a DEBUG.** Los tres logs por
consulta (`hit`, `miss absent`, `miss expired`) bajan a DEBUG. Las
estadísticas agregadas del cache (`cmcourier cache stats`) no dependen
de estos records.

## Escenarios

**E1 — Un upload de 100 MiB emite ~12 eventos de progreso, no ~100.**

**E2 — El bandwidth total acreditado no cambia.** La suma de bytes
(progress + completion) es idéntica pre/post-111.

**E3 — `cache stats` sigue funcionando** con los logs en DEBUG.
