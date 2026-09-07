# 100 — Refresh de documentación: config + comandos al día

> **Continuado en [136](../136-docs-refresh-console/spec.md).** Esta spec dejó
> las referencias al día hasta la 0.100.0. Las 35 specs siguientes (101–135)
> volvieron a abrir la brecha: la consola de operación (`cmcourier console`,
> 123–135), el registro de conexiones `connections:` (129), la fuente de
> metadata MSSQL (130), `--max-duration` (103) y el instalador offline (101)
> quedaron sin documentar. La 136 los cubre y sube el header de
> `config-reference.yaml` a `0.111.0`. También resuelve dos de los tres
> pendientes de "Notas — qué quedó fuera" (ver el final de este archivo).

## Por qué

Auditoría previa al handoff: la documentación de referencia quedó
**desactualizada por ~48 versiones**. `docs/reference/config-reference.yaml`
declaraba "version 0.52.0" — le faltaba prácticamente todo lo
incorporado entre 053 y 100.

Verificado contra el código (`grep` de claves en `docs/`):

* `processing.mode` / `streaming` (063) → ausente.
* `processing.s4_use_processes` / `s4_max_processes` (066) → ausente.
* `processing.s4_smart_routing` (094) → ausente.
* `cmis.http2` (089), `cmis.upload_chunk_bytes` (090) → ausentes.
* `cmis.auto_tune.growth_factor` / `halve_factor` /
  `halve_threshold_ratio` (068) → ausentes.
* `assembly.keep_staged_files` (085) → ausente.
* `tracking.as400_sync.mode` / `periodic` / `interval_minutes` (096)
  → ausentes.
* trigger `local_scan.recursive` (088) → ausente.
* Comando `cmcourier diagnose` (092) → no documentado en `cli.md`.
* `cmcourier sync recover` (099) → no documentado.

El operador es la audiencia de estas referencias — un config doc
incompleto lo hace tropezar con perillas que existen pero no figuran.

## Qué

Poner al día la documentación de config y comandos — las referencias
**y** los tutoriales que el operador consulta:

### `docs/reference/config-reference.yaml`

El YAML de referencia anotado — la fuente para "todas las
configuraciones posibles". Se agregaron, contra `config/schema.py`:

* `processing.mode` (`batched` | `streaming`) + bloque `streaming`
  (`bucket_size`).
* `processing.s4_use_processes`, `s4_max_processes`, `s4_smart_routing`.
* `cmis.http2`, `cmis.upload_chunk_bytes`.
* `cmis.auto_tune.growth_factor`, `halve_factor`, `halve_threshold_ratio`.
* `assembly.keep_staged_files`.
* `tracking.as400_sync.mode` (`claim` | `periodic`) + bloque
  `periodic` (`interval_minutes`), con el tradeoff del modo periódico
  anotado.
* trigger `local_scan.recursive`.
* Header: `version 0.52.0` → `0.100.0`.

Cada campo con su default y constraints; el archivo sigue parseando
como YAML válido.

### `docs/reference/cli.md`

* Sección nueva **`diagnose`** (092) — flags `--config`, `--batch`,
  `--latest`, `--list`.
* Sub-sección nueva **`sync recover`** (099) — flags `--config`,
  `--apply`, `--batch-id`; nota de dry-run-por-defecto.

### `docs/reference/config-schema.md`

Mismo set de campos que `config-reference.yaml`: `local_scan.recursive`
(088), `keep_staged_files` (085), `cmis.http2` / `upload_chunk_bytes`
(089/090), `as400_sync.mode` + nueva sección `PeriodicSyncConfig`
(096), `s4_smart_routing` (094).

### `docs/tutorials/01-the-yaml-config.md`

Walkthrough del YAML al día: `cmis.http2` / `upload_chunk_bytes`,
`as400_sync.mode` + bloque `periodic` (con el tradeoff del modo
periódico), `processing.s4_smart_routing` + su fila en "Campos
críticos".

### `docs/tutorials/04-all-commands-tour.md`

Sección nueva **`diagnose`**; sub-sección nueva **`sync recover`**;
corregida la descripción equivocada de `sync resolve` (decía lo de
`sync status`).

## Criterios de aceptación

1. `config-reference.yaml` cubre **todas** las claves de
   `config/schema.py` y parsea como YAML válido.
2. `cli.md` lista **todos** los comandos registrados — incluidos
   `diagnose` y `sync recover`.
3. Sin cambios de código — es un refresh de docs.

## Notas — qué quedó fuera (incompleto, NO incorrecto)

Auditado; lo que sigue **no está mal** — solo no cubre los conceptos
nuevos. Queda para un cambio de docs futuro:

* `docs/diagrams/hexagonal-layers.md` — conceptualmente correcto (la
  arquitectura sigue siendo la misma); lista servicios representativos,
  no un inventario.
* `docs/diagrams/file-imports-map.md` — incompleto: no incluye el
  subsistema de sync AS400 (`idempotency`, `reconciler`, `recovery`).
  **Parcialmente resuelto en 136**: `docs/diagrams/sync-subsystem.md`
  cubre el subsistema (claim / periodic / recover) a nivel conceptual;
  el mapa de imports en sí sigue sin actualizar.
* `docs/explanation/` — los archivos existentes describen
  comportamiento real y vigente; faltan explicaciones nuevas para los
  conceptos 095-099 (connection pool, modo periódico, cancelación
  cooperativa). **Parcialmente resuelto en 136**:
  `docs/explanation/operations-console.md` cubre la cancelación
  cooperativa (pausa / drain / re-auth 401); el connection pool
  (`ThreadLocalConnectionPool`, 106/119) sigue sin explanation propia.
