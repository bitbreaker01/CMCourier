> [← Volver al índice](../INDEX.md) · [Tutoriales](README.md)

# 04 — Tour de Todos los Comandos

El CLI de CMCourier tiene 14 entry points entre comandos top-level y grupos con subcomandos. En este tutorial los recorremos uno a uno: para qué sirve, las flags más usadas y un ejemplo. Para el detalle exhaustivo (todos los flags, todos los exit codes) leé `docs/reference/cli.md` cuando esté shippeado — acá te damos el mapa.

> Para listar todo en vivo: `cmcourier --help`. Para ayuda de un comando: `cmcourier <comando> --help`. Para un subcomando: `cmcourier <grupo> <subcomando> --help`.

---

## Comandos de pipeline

### `csv-trigger-pipeline run`

Corre la pipeline con trigger CSV de punta a punta (S0 → S6).

**Flags más usadas:** `--config` (required), `--batch-id`, `--from-stage`, `--batch-size`, `--triggers`, `--skip-doctor`, `--resume`, `--tui/--no-tui`, `--batches-in-flight`, `--total`, `--log-level`.

```bash
cmcourier csv-trigger-pipeline run \
  --config prod.yaml \
  --batch-id marzo-2026 \
  --total 5000
```

### `rvabrep-pipeline run`

Igual que csv-trigger pero scanea RVABREP con filtros (no toma `--triggers`).

```bash
cmcourier rvabrep-pipeline run --config prod.yaml --batch-id rva-2026q1
```

### `local-scan-pipeline run`

Cruza archivos extraídos contra RVABREP. Misma forma de flags que rvabrep-pipeline.

```bash
cmcourier local-scan-pipeline run --config prod.yaml
```

### `single-doc run`

Pipeline diagnóstica para un solo doc. Toma `--shortname`, `--system`, opcional `--cif`.

```bash
cmcourier single-doc run \
  --config prod.yaml \
  --shortname JUAN_PEREZ \
  --system 1
```

> Detalle de las 4 pipelines en el [tutorial 02](02-pipelines-and-how-to-use-them.md).

---

## `doctor`

Pre-flight validation **sin** correr la pipeline. Chequea config, conectividad, mapping completeness, metadata sources, alineación de tipos CM, existencia de folders, propiedades CMIS.

**Flags:** `--config` (required), `--check` (default `all`: `connections | mapping | metadata | cm-types | cm-targets | all`), `--log-level`.

```bash
cmcourier doctor --config prod.yaml --check all
cmcourier doctor --config prod.yaml --check connections     # solo conectividad
```

**Exit codes:** 0 todos pasan, 1 alguno falla.

> Profundizamos en el [tutorial 05](05-doctor-deep-dive.md).

---

## Grupo `batch` — gestión de batches

Cuatro subcomandos para introspectar y operar sobre batches ya ejecutados.

### `batch list`

Lista los batches conocidos por el tracking DB, opcionalmente filtrando por status.

```bash
cmcourier batch list --config prod.yaml
cmcourier batch list --config prod.yaml --status failed
```

### `batch show`

Detalle de un batch puntual: contadores por stage, fallos, timing.

```bash
cmcourier batch show --config prod.yaml --batch-id marzo-2026
```

### `batch retry-failed`

Re-corre los docs que quedaron `S{N}_FAILED` en un batch. Opcionalmente filtrar por stage.

```bash
cmcourier batch retry-failed --config prod.yaml --batch-id marzo-2026
cmcourier batch retry-failed --config prod.yaml --batch-id marzo-2026 --stage 5
```

### `batch export-report`

Exporta el detalle del batch a CSV o JSON.

```bash
cmcourier batch export-report --config prod.yaml --batch-id marzo-2026 --format csv
```

---

## Grupo `inspect` — introspección del tracking DB

### `inspect rvabrep`

Resuelve cómo se enriquecería un trigger contra RVABREP. Útil para validar que el shortname + system_id matchee algo.

```bash
cmcourier inspect rvabrep --config prod.yaml --shortname JUAN_PEREZ --system 1
```

### `inspect mapping`

Muestra el mapeo RVI → CM para un código de tipo RVI.

```bash
cmcourier inspect mapping --config prod.yaml --doc-type CC03
```

### `inspect mapping-stats`

Estadísticas del archivo de mapping: cuántos códigos RVI, cuántos están mapeados, cuáles huérfanos.

```bash
cmcourier inspect mapping-stats --config prod.yaml
```

### `inspect trigger`

Vista completa de un trigger desde el punto de vista del pipeline (RVABREP + mapping + metadata resuelta).

```bash
cmcourier inspect trigger --config prod.yaml --shortname JUAN_PEREZ --system 1
```

---

## `as400-query`

Passthrough para correr cualquier SELECT contra AS400 desde la línea de comandos. Útil para validar conectividad o probar queries antes de meterlas en el config.

```bash
cmcourier as400-query \
  --config prod.yaml \
  --query "SELECT COUNT(*) FROM RVILIB.RVABREP WHERE ABABCD LIKE 'JUAN%'"
```

> El config se usa solo para sacar la conexión AS400. Los credenciales vienen de las env vars.

---

## `background`

Punto de entrada cron-friendly. Toma un lock fcntl por config (un run a la vez por archivo de config) y dispara una pipeline en background con logs a archivo.

```bash
cmcourier background --config prod.yaml --pipeline csv-trigger
```

El lock vive en un archivo derivado del path del config. Si otro proceso ya tiene el lock, el segundo termina con error inmediato — útil para crontabs que se solapan.

---

## Grupo `analyze` — análisis offline de logs

Tres subcomandos que leen los `app-*.jsonl` y `system-*.jsonl` de `logs/` para sacar conclusiones después del run.

### `analyze batch`

Analiza un batch puntual. Desde 053 incluye breakdown por stage con clasificador de bottleneck (`upload-bound` vs `assembly-bound` vs `metadata-bound` etc.).

```bash
cmcourier analyze batch --config prod.yaml --batch-id marzo-2026
```

### `analyze compare`

Compara dos batches lado a lado — qué stage cambió, throughput delta.

```bash
cmcourier analyze compare --config prod.yaml --batch-id-a feb-2026 --batch-id-b marzo-2026
```

### `analyze trends`

Tendencias a lo largo de varios batches.

```bash
cmcourier analyze trends --config prod.yaml
```

---

## `diagnose` (092)

Analiza los logs JSONL de un batch y reporta el cuello de botella por stage, con sugerencias automáticas. No depende de SQLite — funciona aunque la tracking DB se haya perdido.

```bash
cmcourier diagnose --config prod.yaml --latest          # el batch más reciente
cmcourier diagnose --config prod.yaml --batch marzo-2026
cmcourier diagnose --config prod.yaml --list            # lista los batches disponibles
```

---

## `completion`

Imprime el script de autocompletion para tu shell.

```bash
cmcourier completion bash       # o zsh, fish
cmcourier completion bash >> ~/.bashrc
```

Seguí las instrucciones que imprime para enchufarlo.

---

## Grupo `sync` — sincronización NIARVILOG (AS400)

Idempotencia distribuida sobre la tabla NIARVILOG en AS400 (spec 034).

### `sync status`

Estado de la sincronización: cuántos docs in-progress, cuántos stale, etc.

```bash
cmcourier sync status --config prod.yaml
```

### `sync resolve`

Resuelve una divergencia AS400/SQLite para un `TRNNUM` puntual — exactamente uno de `--prefer-as400` (AS400 es la fuente de verdad) o `--prefer-local` (SQLite manda; requiere `--cm-object-id`).

```bash
cmcourier sync resolve 0001234 --config prod.yaml --prefer-as400
```

### `sync recover` (099)

Recupera filas faltantes en NIARVILOG para documentos ya subidos a CM (`S5_DONE` en SQLite sin fila en AS400) — repara el daño del bug del modo `periodic`. **Dry-run por defecto**; `--apply` ejecuta los INSERT.

```bash
cmcourier sync recover --config prod.yaml             # dry-run: reporta el plan
cmcourier sync recover --config prod.yaml --apply     # ejecuta los INSERT
```

---

## Grupo `mock` — generadores sintéticos

Para testing y staging — generar inputs sin tener producción a mano.

### `mock generate`

Materializa un árbol de archivos sintéticos (TIFFs/PDFs) en un directorio. Útil para correr `local-scan-pipeline` contra un Alfresco de staging sin tener acceso al archivo bancario real.

```bash
cmcourier mock generate --output /tmp/synthetic-pool --count 1000 --seed 42
```

### `mock rvabrep`

Streamea un CSV con forma RVABREP determinista por semilla.

```bash
cmcourier mock rvabrep --count 100000 --seed 42 > /tmp/rvabrep-fake.csv
```

Encadenable con `mock generate`: `mock rvabrep` te da el RVABREP, `mock generate` te da los archivos en disco, y `local-scan-pipeline` te da una corrida completa de extremo a extremo sin tocar AS400 ni el file server real.

> Ver `docs/how-to/mock-rvabrep-generator.md` y `docs/how-to/local-staging-simulation.md`.

---

## Grupo `cache` — document cache cross-batch

El `document_cache` (037) guarda `(txn_num, fields_hash) → properties_json` para saltar S3 en docs ya resueltos. Default off, prendelo con `metadata.cache.enabled: true`.

### `cache stats`

Cuántas entradas, antigüedad, hit rate (si lo tenés instrumentado).

```bash
cmcourier cache stats --config prod.yaml
```

### `cache clear`

Limpia el cache (todo, o expirado por TTL).

```bash
cmcourier cache clear --config prod.yaml
cmcourier cache clear --config prod.yaml --expired-only
```

---

## Cheat sheet — qué comando para qué problema

| Tarea | Comando |
|-------|---------|
| Correr una pipeline | `<pipeline-name> run` |
| Validar el config antes de correr | `doctor` |
| Ver el estado de un batch viejo | `batch show` |
| Re-correr los fallos | `batch retry-failed` |
| Diagnosticar un doc puntual | `single-doc run` o `inspect trigger` |
| Validar conectividad AS400 | `as400-query` con un `SELECT 1 FROM SYSIBM.SYSDUMMY1` |
| Cron de migración | `background` |
| Ver bottlenecks de un run | `analyze batch` |
| Limpiar cache de metadata | `cache clear --expired-only` |
| Generar inputs para staging | `mock rvabrep` + `mock generate` |
| Setear shell autocompletion | `completion <shell>` |

---

## Convenciones comunes a (casi) todos los comandos

- `--config <path>` es siempre la primera flag y casi siempre **required**. El YAML define adónde escribir, contra qué conectar, qué credenciales pedir al entorno.
- `--log-level` toma `DEBUG | INFO | WARNING | ERROR` (default `INFO`).
- Los comandos de pipeline aceptan `--tui / --no-tui`. En CI siempre `--no-tui`.
- Exit code 0 = éxito; 1 = ran con failures; 2 = error de config; 3 = excepción no manejada.

---

## Siguientes pasos

- [05 — `doctor` en profundidad](05-doctor-deep-dive.md): el comando que más vas a usar antes de cada corrida
- [06 — Tu primera corrida streaming](06-first-streaming-run.md): correr de verdad con la TUI
- [07 — Debugging de un batch fallido](07-debugging-a-failed-batch.md): usando `inspect`, `analyze`, `batch retry-failed`
- [`docs/how-to/log-analysis.md`](../how-to/log-analysis.md): integrar `analyze` en CI
