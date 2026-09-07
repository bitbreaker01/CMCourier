> [← Volver al índice](../INDEX.md) · [Reference](README.md)

# CLI reference

Toda la superficie del comando `cmcourier`. Cada flag y cada subcomando salen del código fuente en `src/cmcourier/cli/`. Si algo acá no coincide con `--help`, gana `--help` — abrí un issue.

## Exit codes (compartidos por todos los comandos `*-pipeline run` y `single-doc run`)

| Code | Meaning |
|------|---------|
| `0` | Success — el pipeline corrió sin fallas (`s5_failed == 0`). |
| `1` | Pipeline ran but with stage failures (`s5_failed > 0` o algún upstream). |
| `2` | Configuration error (YAML inválido, env var faltante, `trigger.kind` desalineado). |
| `3` | Unhandled exception dentro de `pipeline.run` o crash inesperado. |
| `75` | `background` only — otro lock está activo (`EX_TEMPFAIL`, cron-friendly). |

---

## Grupo raíz

```
cmcourier --version
cmcourier --help
```

Group: `main` (`cli/app.py:65`). Subcommands se listan abajo.

---

## Pipeline commands

### `csv-trigger-pipeline run`

Corre el pipeline end-to-end con triggers desde un CSV.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` | Path (required) | — | Pipeline YAML. Debe existir. |
| `--batch-id` | str | `None` | Identifier del batch. Si se omite, se autogenera. |
| `--from-stage` | int (1–5) | `1` | Resume desde el stage N. |
| `--batch-size` | int (≥ 1) | `None` | Override de `batch_size` del YAML. |
| `--triggers` | Path | `None` | Override del CSV de triggers (sólo `csv` trigger kind). |
| `--skip-doctor` | flag | `False` | Bypass del auto-doctor pre-flight. |
| `--resume` | flag | `False` | Detecta `from-stage` leyendo el estado del batch. Requiere `--batch-id`. |
| `--tui` / `--no-tui` | bool | `True` | Live TUI. Auto-off en headless si no es TTY. |
| `--batches-in-flight` | int (1–2) | YAML | Override de `processing.batches_in_flight`. |
| `--total` | int (≥ 1) | `None` | Procesar a lo sumo N triggers (smoke runs). |
| `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` | Verbosidad. |

Source: `cli/app.py:90-168`.

### `rvabrep-pipeline run`

Igual que `csv-trigger-pipeline run` pero el `trigger.kind` del YAML debe ser `"rvabrep"`. No acepta `--triggers` (no hay CSV de triggers en este modo).

Source: `cli/app.py:181-234`.

### `local-scan-pipeline run`

Igual que `rvabrep-pipeline run`. El `trigger.kind` debe ser `"local_scan"`.

Source: `cli/app.py:247-300`.

### `single-doc run`

Pipeline one-shot para un único documento. Diagnóstico, no productivo.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` | Path (required) | — | YAML con `trigger.kind: single_doc`. |
| `--shortname` | str (required) | — | Shortname del documento target. |
| `--system` | str (required) | — | System identifier (SystemID). |
| `--cif` | str | `None` | CIF opcional. Si está vacío, se auto-resuelve. |
| `--batch-id` | str | `None` | — |
| `--from-stage` | int (1–5) | `1` | — |
| `--batch-size` | int (≥ 1) | `None` | — |
| `--skip-doctor` | flag | `False` | — |
| `--resume` | flag | `False` | — |
| `--tui` / `--no-tui` | bool | `True` | — |
| `--batches-in-flight` | int (1–2) | YAML | — |
| `--total` | int (≥ 1) | `None` | — |
| `--log-level` | choice | `INFO` | — |

Source: `cli/app.py:313-421`.

---

## `doctor`

Pre-flight validation. No corre el pipeline.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` | Path (required) | — | Pipeline YAML. |
| `--check` | choice | `all` | Un grupo (`connections`, `mapping`, `metadata`, `cm-types`, `cm-targets`, `all`) o un check individual por nombre (126). |
| `--log-level` | choice | `INFO` | — |

Checks individuales (`CHECK_NAMES`, en orden de ejecución):

| Check | Grupo | Qué prueba |
|-------|-------|------------|
| `log_dir_writable` | connections | `observability.log_dir` se crea y admite escritura. |
| `cmis_connectivity` | connections | `repositoryInfo` del CMIS. |
| `as400_connectivity` | connections | Cada conexión `as400` del registro (129): credenciales presentes + `SELECT 1 FROM SYSIBM.SYSDUMMY1`. SKIP si no hay ninguna. |
| `mssql_connectivity` | connections | Cada conexión `mssql` del registro (130): credenciales presentes + `SELECT 1`. SKIP si no hay ninguna. |
| `tracking_openable` | connections | La SQLite de tracking abre en WAL. |
| `as400_sync` | connections | Conexión + tabla NIARVILOG cuando `tracking.as400_sync.enabled`. SKIP si está off. |
| `mapping_completeness` | mapping | El Modelo Documental tiene ≥1 fila. |
| `metadata_sources` | metadata | Cada fuente de `metadata.sources` (csv / as400 / mssql) devuelve ≥1 fila. |
| `cm_type_alignment` | cm-types, cm-targets | Cada `cm_object_type` resuelve por `getTypeDefinition`. |
| `cmis_folders_exist` | cm-targets | Cada `CMISFolder` declarado en MapeoRVI_CM existe en el repositorio. |
| `cmis_properties_alignment` | cm-targets | Cada par `(CMISType, CMISPropertyId)` de MetadatosCM existe en la definición del tipo. |
| `sample_dry_run` | metadata | S1→S4 sobre el primer documento, sin upload. |

Exit codes: `0` si todos los checks pasan, `1` si alguno falla, `2` si la config no carga, `3` si el doctor crashea.

Source: `cli/app.py:429-468`, `cli/doctor.py`.

---

## `batch` — lifecycle introspection

Group: `cli/commands/batch.py`. Todos los subcomandos requieren `--config`.

### `batch list`

Enumera batches con estado y contadores (más nuevos primero).

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` / `-c` | Path (required) | — | — |
| `--status` | `in_progress`/`completed` | `None` | Filtro por estado. |

### `batch show <batch_id>`

Detalle por etapa (DONE / FAILED / PENDING) + records fallados.

| Arg / Flag | Type | Default |
|------|------|---------|
| `batch_id` | str (positional, required) | — |
| `--config` / `-c` | Path (required) | — |

### `batch retry-failed`

Resetea filas `*_FAILED` a `*_PENDING` para reintento.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` / `-c` | Path (required) | — | — |
| `--batch` | str (required) | — | Batch ID. |
| `--stage` | `S1`/`S2`/`S3`/`S4`/`S5` | `None` | Resetear sólo esta etapa. |

### `batch export-report`

Vuelca el estado completo del batch a CSV o JSON para análisis offline.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` / `-c` | Path (required) | — | — |
| `--batch` | str (required) | — | — |
| `--format` | `csv`/`json` (required) | — | — |
| `--output` | Path | `None` (stdout) | Destino del reporte. |

---

## `inspect` — read-only previews

Group: `cli/commands/inspect.py`.

### `inspect rvabrep <shortname> <system_id>`

Imprime las filas RVABREP que S1 produciría para el trigger.

| Flag | Type | Default |
|------|------|---------|
| `--config` / `-c` | Path (required) | — |

### `inspect mapping <id_rvi>`

Imprime el mapping de CM (folder, type, fields requeridos) para un ID RVI.

| Flag | Type | Default |
|------|------|---------|
| `--config` / `-c` | Path (required) | — |

### `inspect mapping-stats`

Resumen estructurado del Modelo Documental (totales, clases, folders, types).

| Flag | Type | Default |
|------|------|---------|
| `--config` / `-c` | Path (required) | — |

### `inspect trigger`

Vista previa de los primeros N triggers desde un source configurado o ad-hoc.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` / `-c` | Path (required) | — | — |
| `--source` | str | `None` | Override (`csv:<path>` o `single_doc:SHORT,SYS[,CIF]`). |
| `--limit` | int (≥ 1) | `10` | Cuántos triggers mostrar. |

---

## `as400-query`

Ejecuta SQL crudo contra AS400 — solo debug. Requiere `AS400_USERNAME` y `AS400_PASSWORD` en el environment.

| Arg / Flag | Type | Default | Description |
|------|------|---------|-------------|
| `sql` | str (positional, required) | — | SQL a ejecutar. |
| `--config` / `-c` | Path (required) | — | YAML con una conexión AS400 (en `indexing.source` o `metadata.sources`). |

Las celdas se truncan a 80 chars. PII responsibility = operador.

---

## `background`

Runner cron/systemd friendly. Lock por config (POSIX `fcntl.flock` o Windows `msvcrt.locking`). Salida silenciosa en éxito.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--pipeline` | `csv-trigger`/`rvabrep`/`as400-trigger`/`local-scan` (required) | — | Pipeline productivo a correr. |
| `--config` / `-c` | Path (required) | — | — |
| `--batch-id` | str | `None` | — |
| `--from-stage` | int (1–5) | `1` | — |
| `--batch-size` | int (≥ 1) | `None` | — |
| `--skip-doctor` | flag | `False` | — |
| `--resume` | flag | `False` | — |
| `--log-level` | choice | `WARNING` | Default WARNING — cron stays quiet on success. |

Exit codes especiales:
- `75` (`EX_TEMPFAIL`) si hay otra instancia con el lock tomado.

---

## `analyze` — offline log analysis (027)

Group: `cli/commands/analyze.py`.

### `analyze batch <batch_id>`

Reporte completo de un batch terminado.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` | Path | `None` | YAML — para derivar `log_dir` + techo CMIS. |
| `--log-dir` | Path | `None` | Override directo. Una de las dos es obligatoria. |
| `--format` | `text`/`json` | `text` | Salida. |

### `analyze compare <batch_a> <batch_b>`

Delta entre dos batches.

| Flag | Type | Default |
|------|------|---------|
| `--config` | Path | `None` |
| `--log-dir` | Path | `None` |
| `--format` | `text`/`json` | `text` |

### `analyze trends`

Serie temporal sobre los últimos N batches.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` | Path | `None` | — |
| `--log-dir` | Path | `None` | — |
| `--last` | int (≥ 1) | `10` | Cuántos batches. |
| `--pipeline` | str | `None` | Filtro por nombre de pipeline. |
| `--format` | `text`/`json` | `text` | — |

---

## `diagnose` (092)

Analiza los logs JSONL de un batch y reporta el cuello de botella por stage, con sugerencias automáticas. Lee `observability.log_dir/metrics-*.jsonl` — **no depende de SQLite**, así que el diagnóstico funciona aunque la tracking DB se haya perdido.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` | Path (required) | — | YAML del pipeline — se usa para ubicar `observability.log_dir`. |
| `--batch` | str | `None` | `batch_id` puntual a analizar. Mutuamente excluyente con `--latest`. |
| `--latest` | flag | `False` | Analiza el `batch_summary` más reciente de los metrics logs. |
| `--list` | flag | `False` | Lista los `batch_summary` disponibles y sale. |

---

## `completion <shell>`

Emite el script de shell-completion (032).

| Arg | Type | Values |
|------|------|--------|
| `shell` | choice (required) | `bash`, `zsh`, `fish` |

Instalación canónica (ejemplo bash):
```
eval "$(cmcourier completion bash)"
```

---

## `sync` — AS400 NIARVILOG reconciliation (034)

Reconcilia divergencias entre el SQLite local y `RVILIB.NIARVILOG`. Requiere `tracking.as400_sync.enabled: true` + credenciales AS400 en el environment.

### `sync status`

Pre-flight cleanup + reporte. Read-only.

| Flag | Type | Default |
|------|------|---------|
| `--config` | Path (required) | — |

### `sync resolve <txn>`

Resuelve una divergencia para un `TRNNUM`.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `txn` | str (positional, required) | — | TRNNUM a resolver. |
| `--config` | Path (required) | — | — |
| `--prefer-as400` | flag | `False` | AS400 es la fuente de verdad — pull a SQLite. |
| `--prefer-local` | flag | `False` | SQLite es la fuente — push `cm_object_id` a AS400. |
| `--cm-object-id` | str | `None` | Required cuando `--prefer-local`. |

Exactamente uno de `--prefer-as400` / `--prefer-local`.

### `sync recover` (099)

Recupera filas faltantes en `NIARVILOG` para documentos ya subidos a CM (`S5_DONE` en SQLite pero sin fila en AS400) — repara el daño del bug del modo `periodic`. Re-deriva los campos que SQLite no almacena (`DOCFRM`/`IMGTIP` desde RVABREP, `IDNBAC`/`TIPIDN` desde el mapping) e inserta las filas terminales. Idempotente: re-correr saltea las ya presentes.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` | Path (required) | — | — |
| `--apply` | flag | `False` | Sin el flag = **dry-run** (reporta el plan, no escribe). Con `--apply`, ejecuta los INSERT en AS400. |
| `--batch-id` | str | `None` | Acota la recuperación a un `batch_id`. Default: todo el tracking. |

Un `txn` sin fila RVABREP o con id RVI no mapeado se reporta como `unrecoverable` — nunca se inserta a ciegas.

---

## `mock` — synthetic file tree (031, 039)

Group: `cli/commands/mock.py`.

### `mock generate`

Materializa un file tree mock válido desde un source RVABREP.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--rvabrep-csv` | Path | `None` | CSV con filas RVABREP. |
| `--rvabrep-as400` | flag | `False` | Leer RVABREP de AS400 (requiere `--config`). |
| `--config` | Path | `None` | YAML (requerido para `--rvabrep-as400`). |
| `--root` | Path (required) | — | Directorio raíz donde se materializa el árbol. |
| `--pdf-min` | str (required) | — | Tamaño mínimo PDF, ej. `10kb`. |
| `--pdf-max` | str (required) | — | Tamaño máximo PDF, ej. `2mb`. |
| `--img-min` | str (required) | — | Tamaño mínimo imagen. |
| `--img-max` | str (required) | — | Tamaño máximo imagen. |
| `--limit` | int | `None` | Cap de archivos planeados. |
| `--system` | str (multiple) | — | Filtro repetible por `ABAACD`. |
| `--document-type` | str (multiple) | — | Filtro repetible por `ABAHCD`. |
| `--seed` | int | `None` | Seed determinístico. |
| `--dry-run` | flag | `False` | Imprime el plan; no escribe. |
| `--force` | flag | `False` | Sobreescribir existentes. |
| `--include-deleted` | flag | `False` | Incluir filas con `ABACST` no vacío. |

### `mock rvabrep`

Genera un CSV RVABREP sintético consumible por `mock generate`.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--rows` | int (≥ 1) | `50000` | Filas a generar. |
| `--output` | Path (required) | — | Destino CSV. |
| `--seed` | int | `None` | PRNG seed. Default = `--rows`. |
| `--idrvi-source` | Path | `reference-data/csv/MapeoRVI_CM.csv` | CSV con columna `IDRVI`. |
| `--idrvi-top` | int (≥ 1) | `20` | Top-N IDRVIs distintos. |
| `--image-mix` | str | `tiff:60,pdf:20,jpeg:20` | Pesos. |
| `--date-from` | str | `2024-01-01` | ISO YYYY-MM-DD. |
| `--date-to` | str | `2025-12-31` | ISO YYYY-MM-DD. |
| `--clients` | int (≥ 1) | `5000` | Cardinalidad del shortname pool. |
| `--delete-rate` | float (0–1) | `0.05` | Fracción de filas borradas. |
| `--cif-rate` | float (0–1) | `0.95` | Fracción de filas con CIF. |

---

## `cache` — document cache (037)

Group: `cli/commands/cache.py`. Inspecciona o limpia el `document_cache` cross-batch.

### `cache stats`

| Flag | Type | Default |
|------|------|---------|
| `--config` / `-c` | Path (required) | — |
| `--format` | `text`/`json` | `text` |

### `cache clear`

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` / `-c` | Path (required) | — | — |
| `--txn` | str | `None` | Borrar un único `txn_num`. |
| `--all` | flag | `False` | Truncar la tabla entera. |
| `--older-than` | int | `None` | Borrar entradas más viejas que N minutos. |

Exactamente uno de `--txn`, `--all`, `--older-than`.

---

## Ver también

- [`config-schema.md`](config-schema.md) — qué keys YAML acepta cada `--config`.
- [`error-codes.md`](error-codes.md) — qué significa cada exit code 1 o 2.
- [How-to: validation checklist](../how-to/validation-checklist.md) — usar `doctor` en una secuencia de pre-flight completa.
- [How-to: multi-batch](../how-to/multi-batch.md) — cuándo usar `--batches-in-flight 2`.
