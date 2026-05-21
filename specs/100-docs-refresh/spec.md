# 100 — Refresh de documentación: config + comandos al día

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

Poner al día las dos referencias canónicas — sin reescribir todo
`docs/`, foco en lo que el operador consulta:

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

## Criterios de aceptación

1. `config-reference.yaml` cubre **todas** las claves de
   `config/schema.py` y parsea como YAML válido.
2. `cli.md` lista **todos** los comandos registrados — incluidos
   `diagnose` y `sync recover`.
3. Sin cambios de código — es un refresh de docs.

## Notas — qué más quedó viejo (NO incluido acá)

El refresh se acotó a las dos referencias canónicas (Q2 del operador:
"config + comandos a fondo"). Auditado pero **diferido** a un cambio
futuro de docs:

* `docs/reference/config-schema.md` — representación secundaria del
  schema; también desactualizada.
* `docs/tutorials/01-the-yaml-config.md` y `04-all-commands-tour.md`
  — walkthroughs; no mencionan streaming, el process pool de S4,
  `diagnose` ni `sync recover`.
* `docs/diagrams/` (`file-imports-map.md`, `hexagonal-layers.md`) —
  no reflejan los módulos nuevos (`reconciler`, `recovery`,
  `cancellation`).
* `docs/explanation/` — barrido pendiente de menciones a
  comportamiento viejo.
