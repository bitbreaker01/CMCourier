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
| `--total` | int (≥ 1) | `None` | Procesar a lo sumo N triggers (smoke runs). También es lo que habilita la ETA de corrida en la consola (134). |
| `--max-duration` | str | `None` | Corta la corrida pasado ese wall-clock: `30m`, `2h`, `1h30m` (103). Drain ordenado, exit code `0`. |
| `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` | Verbosidad. |

`--max-duration` (103) no aborta: dispara el mismo `CancellationToken` que `x` en la consola, así que los uploads en vuelo terminan y el batch queda reanudable. El corte se registra como `pipeline_stopped_by_deadline` en los metrics logs.

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
| `--max-duration` | str | `None` | `30m` / `2h` / `1h30m` (103). |
| `--log-level` | choice | `INFO` | — |

Source: `cli/app.py:313-421`.

---

## `console` — consola de operación (123–135)

Abre la TUI de operación: credenciales, doctor, overrides, launcher, monitor, batches y sync en una sola pantalla. Es el reemplazo interactivo de encadenar `doctor` + `<pipeline> run` + `batch` + `sync` a mano.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--config` | Path (required) | — | Pipeline YAML. Se valida al abrir; si no carga, la consola no arranca. |
| `--log-level` | `debug`/`info`/`warning`/`error` | `WARNING` | Verbosidad de la consola y de toda corrida lanzada desde `[5]`. Default `WARNING` — la TUI ocupa la terminal, un `INFO` a stdout la ensuciaría. |

Necesita un TTY real — no funciona por un pipe ni en un editor sin terminal integrada; sin TTY sale con exit code `2`.

> Con la TUI activa los logs van al archivo de `observability`, no a stdout: un `--log-level debug` es seguro para la pantalla y útil para diagnosticar una corrida lanzada desde la consola.

### Pestañas

`1`–`9` o `F1`–`F9`. Los `F`-keys tienen `priority=True`: funcionan **aun con el foco dentro de un campo de texto**, los dígitos no.

| # | Pestaña | Qué hace |
|---|---------|----------|
| `1` | INICIO | Config, entorno, `trigger.kind`, `processing.mode`, estado por conexión, veredicto del doctor y la lista "siguiente paso". |
| `2` | CREDENCIALES | Una tarjeta por conexión de la config efectiva (131) más `cmis`. Prueba de conexión por alias. Alta / edición / baja de conexiones del registro y "mover al registro" una inline (138). Las credenciales viven en la sesión — nunca tocan disco. |
| `3` | CONFIG | Overrides de sesión sobre el YAML, modelo *draft → applied* (124) y escritura al YAML con `w` (135). |
| `4` | DOCTOR | El mismo `run_doctor` que el comando, con selector `all` / grupo / check individual (126). Corre en un worker, no congela la UI. |
| `5` | CORRER | Launcher: elección de pipeline (127), parámetros por kind, `nueva` vs `reanudar`, `total` y `max-duration`. |
| `6` | MONITOR | Corrida en vivo a 4 Hz: cabecera, PREP/UPLOAD, y en `streaming` también el bucket. |
| `7` | BATCHES | Tabla de batches con su auditoría; detalle, retry y export. |
| `8` | SYNC | La versión interactiva de `sync status` / `recover` / `resolve` (128). |
| `9` | YAML | Formulario generado del schema sobre el YAML completo (137–139): `v` valida, `w` escribe (con backup). `connections` se administra en `[2]`, acá es de sólo lectura. |

### Teclas

Copiadas de `HelpScreen.HELP` (`cli/console/app.py`) — la ayuda `?` dentro de la consola es la misma tabla.

| Tecla | Ámbito | Acción |
|-------|--------|--------|
| `1`–`9` / `F1`–`F9` | global | Cambiar de pantalla. |
| `?` | global | Ayuda (teclas + leyenda S0–S7 + lista de checks del doctor). |
| `q` | global | Salir. Confirma si hay corrida — salir **no** la cancela, sigue en background. |
| `Esc` | global | Cerrar modal / soltar el foco de un campo. |
| `↵` | `[2]` | Probar la conexión de la tarjeta enfocada. |
| `n` | `[2]` | Nueva conexión del registro (138) — editar / quitar quedan en cada tarjeta. |
| `a` | `[3]` | Aplicar el borrador de overrides (draft → applied). |
| `w` | `[3]` | Escribir los overrides **aplicados** al YAML (135). |
| `d` | `[4]` | Correr la selección del doctor. |
| `↑` `↓` | `[4]` `[7]` | Navegar la lista. |
| `↵` | `[4]` | Expandir / colapsar el detalle del check. |
| `r` | `[5]` | Lanzar la corrida. |
| `x` | `[6]` | Cancelar con drain (confirma). |
| `p` | `[6]` | Pausar la corrida (confirma) — 132. |
| `r` | `[6]` | Reanudar la corrida pausada — 132. |
| `+` / `=` / `-` | `[6]` | Mover el techo manual de workers en caliente, sin confirmación — 133. |
| `↵` | `[7]` | Detalle del batch. |
| `R` | `[7]` | Reintentar los fallidos (te lleva al launcher en modo reanudar). |
| `E` | `[7]` | Exportar el reporte del batch. |
| `s` | `[8]` | Estado del sync. |
| `v` | `[9]` | Validar el YAML editado contra el schema — pinta los errores por fila (139). |
| `w` | `[9]` | Escribir el formulario editado al YAML completo, con confirmación y backup (139). |

Ojo con `r`: en `[5]` lanza y en `[6]` reanuda. Es la misma acción (`action_launch`) ruteada por la pestaña activa.

### Overrides de sesión vs. `w`

`[3]` mantiene **dos** niveles. El *borrador* es lo que tipeás; lo *aplicado* es lo que la corrida y el doctor van a usar. `a` promueve borrador → aplicado (validando); nada llega a una corrida sin pasar por esa validación. Un borrador sin aplicar hace que `[5]` avise "overrides SIN GUARDAR" y use igual el valor del YAML.

`w` es el tercer nivel, opcional: escribe lo **aplicado** al archivo. Sólo los siete escalares:

| Override | Clave YAML |
|----------|-----------|
| `mode` | `processing.mode` |
| `prep_workers` | `processing.prep_workers` |
| `bucket_size` | `processing.streaming.bucket_size` |
| `workers` | `cmis.workers` |
| `auto_tune_enabled` | `cmis.auto_tune.enabled` |
| `max_bandwidth_mbps` | `cmis.max_bandwidth_mbps` |
| `unmask_pii` | `observability.unmask_pii` |

El pipeline elegido en `[5]` (127) **no** se persiste: es una elección por corrida, no de configuración.

El parche es sobre el texto, línea a línea — comentarios y formato quedan byte-idénticos. Antes de tocar el original la consola recarga el resultado con `load_config` y lo compara contra lo que debería quedar; si no coincide (flow style, anchors, claves duplicadas) se niega con un `PersistError` y el archivo queda intacto, sin backup a medias. Si pasa, hace `config.yaml.bak-YYYYmmdd-HHMMSS` al lado y reemplaza de forma atómica.

### Conexiones del registro (`[2]`)

`n` (o el botón "nueva conexión") abre el modal de alta (138). Cada tarjeta de una conexión del registro trae **editar** y **quitar**; la tarjeta de una conexión `as400` inline (la del alias implícito `as400` en `indexing.source` / `metadata.sources[]` / `tracking.as400_sync`) sólo trae **mover al registro…** — no se puede editar ni quitar sin moverla primero.

El modal:

- **alias**: `^[a-z][a-z0-9_]{0,31}$` (minúscula inicial, después minúsculas/dígitos/`_`, máx. 32); `cmis` está reservado. Al editar, alias y `kind` quedan bloqueados — cambiar de `kind` es borrar y crear de nuevo.
- un `Input` por campo de la `kind` elegida (`as400`: `host`, `port`, `database`, `driver`, `table`; `mssql`: `host`, `port`, `database`, `driver`, `encrypt`, `trust_server_certificate`), con el default del modelo como placeholder.
- un checkbox por sitio de la config que acepta esa `kind` ("usar esta conexión en:"). Un sitio que HOY usa este alias no se puede destildar acá: hay que apuntarlo a otra conexión desde su propio editor.
- se escribe **`kind` + los campos no vacíos**, ya tipados (`port` a `int`; `encrypt` / `trust_server_certificate` a `bool` desde `true/false/sí/si/no/1/0`). Un campo vacío se omite (o se borra, si estabas editando): el default del modelo lo cubre en memoria, pero no se escribe al YAML.
- **editar** un alias existente escribe **campo por campo** (`connections.<alias>.<campo>`), no reemplaza el mapping entero — así el estilo (por ejemplo un bloque en flow style) y los comentarios de los demás campos sobreviven.
- **quitar** se bloquea si algún sitio referencia el alias (el mensaje lista cuáles); si no, borra `connections.<alias>` con confirmación y backup.
- **mover al registro…** arma un alias nuevo con los campos y los sitios de la conexión inline pre-marcados, y en el mismo golpe crea `connections.<alias>` y apunta el sitio inline al alias por nombre.

Las credenciales de sesión (usuario/contraseña de la tarjeta) no forman parte de este modal — viven aparte, en memoria — pero editar el registro invalida la prueba anterior del alias tocado y deja el doctor desactualizado.

### YAML completo (`[9]`)

El formulario sale del schema (`build_form` sobre `PipelineConfig`) contra un dict plano leído del disco; cada cambio en un campo actualiza ese dict en memoria, no el archivo. `connections` se muestra (alias · kind) pero es de **sólo lectura** acá — se administra desde `[2]`; un sitio con una conexión inline aparece como `(inline — editar en [2])`.

- **`v`** valida como lo haría `load_config` (con los defaults de `kind` inyectados) y pinta cada error en la fila más específica que matchea; un error a nivel de modelo sin fila propia se lista aparte.
- **`w`** primero valida solo: sin cambios pendientes avisa y no hace nada; con errores de validación se niega; con una corrida activa avisa y tampoco escribe. Si pasa, confirma con la cantidad de cambios y escribe **sólo el diff** entre lo original y lo editado vía `YamlDocument` — nunca re-serializa el dict entero.
- Al entrar a la pestaña (o cuando `[2]`/`[3]` escriben), si no hay ediciones pendientes recarga del disco solo; con ediciones sin guardar no pisa lo que estás tipeando, y el encabezado avisa "⚠ el archivo cambió en disco" si además cambió por fuera.
- Qué preserva el round-trip (137): comentarios, orden de las claves, comillas, CRLF, la indentación detectada del archivo.
- Qué **no** preserva: un ítem de lista reemplazado (p. ej. cambiar el `kind` de una fuente de metadata) pierde los comentarios que tenía adentro; un mapping en flow style pierde el espaciado interno (`{ a: 1 }` pasa a `{a: 1}`); borrar el último hijo de un mapping puede dejarlo en `{}` en la misma línea (p. ej. `connections: {}` si se vaciara el registro — aunque `connections` en la práctica se edita en `[2]`, no acá).

Diferencia con `[3]`: `[3]` sólo toca siete escalares de rendimiento/observabilidad, con el modelo *borrador → aplicado* de sesión (`a` / `w`). `[9]` edita el archivo completo contra el schema — cualquier clave, no sólo esos siete —, sin ese nivel intermedio: lo que ves en el formulario es lo que `w` va a escribir. Las dos pestañas comparten el mismo escritor (`YamlDocument`, con su backup) pero son caminos independientes.

### Archivos que escribe la consola

Toda escritura al YAML desde la consola —overrides de `[3]` (135), conexiones de `[2]` (138) o el formulario de `[9]` (139)— pasa por el mismo escritor (`config/yaml_doc.py`, 137):

1. Vuelca el documento editado a un temporal en el mismo directorio: `.<nombre-del-yaml>.<pid>.tmp`.
2. Verifica ese temporal recargándolo con `load_config` (en `[3]`, comparando además contra el resultado esperado de los overrides). Si falla, el temporal se borra y el original queda intacto — sin backup.
3. Si pasa, copia el original a `<nombre-del-yaml>.bak-YYYYmmdd-HHMMSS` (sufijo `-N` si dos escrituras caen en el mismo segundo) al lado del archivo.
4. Reemplaza el original por el temporal de forma atómica (mismo directorio, permisos copiados del original).

Ninguno de los tres caminos deja el archivo a medias: si la verificación falla, no hay ni backup ni cambio.

### Lock de config

`[5]` toma el **mismo lock** que `background`: `acquire_config_lock(config_path)` sobre `<runtime_dir>/cmcourier/<sha256(path)[:12]>.lock` (`$XDG_RUNTIME_DIR` o `/tmp` en POSIX, `tempfile.gettempdir()` en Windows), con `fcntl.flock(LOCK_EX | LOCK_NB)` o `msvcrt.locking(LK_NBLCK)`. Es **por config y por estación**: dos consolas sobre el mismo YAML colisionan y la segunda muestra el modal "Config bloqueada en esta estación"; el lock no ve otras máquinas — para eso está `tracking.as400_sync`.

### Interlock de producción

Con `environment: prd` en el YAML la consola muestra un badge rojo permanente y `[5]` exige tipear `PRD` antes de lanzar. `sync recover --apply` desde `[8]` pide la misma confirmación tipeada. Los comandos headless ignoran `environment` por completo.

Source: `cli/app.py`, `cli/console/`. Guía paso a paso: [`how-to/probar-la-consola.md`](../how-to/probar-la-consola.md). El porqué de cada decisión: [`explanation/operations-console.md`](../explanation/operations-console.md).

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
