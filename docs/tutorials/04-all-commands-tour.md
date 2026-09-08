> [← Volver al índice](../INDEX.md) · [Tutoriales](README.md)

# 04 — Tour de Todos los Comandos

El CLI de CMCourier tiene 16 entry points entre comandos top-level y grupos con subcomandos. En este tutorial los recorremos uno a uno: para qué sirve, las flags más usadas y un ejemplo. Para el detalle exhaustivo (todos los flags, todos los exit codes) leé [`docs/reference/cli.md`](../reference/cli.md) — acá te damos el mapa.

> Si arrancás desde cero como operador, el atajo es `cmcourier console`: junta doctor, launcher, monitor, batches y sync en una sola pantalla. Está más abajo, y es probablemente lo único que vas a usar a diario.

> Para listar todo en vivo: `cmcourier --help`. Para ayuda de un comando: `cmcourier <comando> --help`. Para un subcomando: `cmcourier <grupo> <subcomando> --help`.

---

## Comandos de pipeline

### `csv-trigger-pipeline run`

Corre la pipeline con trigger CSV de punta a punta (S0 → S6).

**Flags más usadas:** `--config` (required), `--batch-id`, `--from-stage`, `--batch-size`, `--triggers`, `--skip-doctor`, `--resume`, `--tui/--no-tui`, `--batches-in-flight`, `--total`, `--max-duration`, `--log-level`.

```bash
cmcourier csv-trigger-pipeline run \
  --config prod.yaml \
  --batch-id marzo-2026 \
  --total 5000

# ventana de mantenimiento: cortar solo a las 2 horas, con drain ordenado (103)
cmcourier csv-trigger-pipeline run --config prod.yaml --max-duration 2h
```

`--max-duration` (`30m`, `2h`, `1h30m`) no mata el proceso: dispara el mismo drain cooperativo que un cancel, los uploads en vuelo terminan, el batch queda reanudable y el exit code es `0`.

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

## `console` — la consola de operación (123–135)

Todo lo de arriba y lo de abajo, en una sola pantalla. En vez de encadenar `doctor` → `<pipeline> run` → `batch show` → `sync status` a mano, abrís la consola y hacés el recorrido completo sin salir.

```bash
cmcourier console --config prod.yaml
```

Sólo dos flags: `--config` (required) y `--log-level` (default `WARNING`; la TUI ocupa la terminal). Necesita un TTY real — por un pipe no arranca.

Arriba vas a ver la barra con el badge de entorno (verde `STAGING`, o rojo `⚠ PRODUCCIÓN` si el YAML dice `environment: prd`), y nueve pestañas que se cambian con `1`–`9` o `F1`–`F9`. En cualquier momento `?` abre la ayuda con todas las teclas y `q` sale.

### El recorrido corto

**`2` CREDENCIALES.** Una tarjeta por conexión que tu YAML realmente usa, más CMIS. Escribís usuario y contraseña, `↵` prueba la conexión y el chip pasa a `ok · NN ms`. Las credenciales viven en la sesión: nunca tocan el disco, y al salir se descartan. Si ya tenías `CMIS_USERNAME` / `<ALIAS>_USERNAME` exportadas, vienen pre-cargadas. `n` (o el botón "nueva conexión") da de alta un alias nuevo en el registro sin salir de la consola (138); cada tarjeta trae **editar** / **quitar**, y la conexión `as400` inline se puede **mover al registro** desde su propia tarjeta.

**`4` DOCTOR.** El selector tiene tres niveles: `all`, un grupo (`connections`, `metadata`, …) o un check individual. `d` lo corre en un worker — la UI no se congela. `↑↓` navega y `↵` expande el detalle; los FAIL y WARN se expanden solos. Valida la config **efectiva**: YAML + overrides aplicados + el pipeline elegido en `[5]`.

**`3` CONFIG.** Overrides de sesión sobre el YAML: `mode`, `prep_workers`, `bucket_size`, `cmis.workers`, AIMD on/off, `max_bandwidth_mbps`, `unmask_pii`. Campo vacío = "usar el YAML". Acá hay una trampa útil: lo que tipeás es un **borrador** y no cuenta hasta que apretás `a`. Si vas a `[5]` con el borrador sin aplicar, el resumen te avisa que hay overrides sin guardar y corre con el valor del YAML. `a` valida antes de aceptar — un `workers` de 999 se rechaza ahí, no a mitad de corrida.

Si además querés que el cambio sobreviva a la sesión, `w` escribe los overrides aplicados al YAML (135), con backup `config.yaml.bak-<fecha>` al lado. El pipeline elegido en `[5]` no se escribe nunca: es una decisión por corrida.

**`5` CORRER.** Elegís el pipeline acá — no hace falta editar el YAML (127). Arranca en el del YAML y podés moverlo a `rvabrep`, `local_scan` o `single_doc`; cada uno muestra sus propios parámetros. Ponés `total` (y con eso habilitás la ETA del monitor) y opcionalmente un `max-duration`. Mirás el bloque "Config efectiva" y apretás `r`. Si el doctor no está aprobado te pregunta; si el entorno es `prd`, además te hace tipear `PRD`.

**`6` MONITOR.** La corrida en vivo. Cabecera con subidos / fallidos (con desglose por tipo, 104) / elapsed, dos tasas — la acumulada y la de los últimos 60 s — y la ETA si pusiste `total`. Abajo, PREP y UPLOAD.

Las teclas del monitor son las que importan cuando algo se pone feo:

| Tecla | Qué hace |
|-------|----------|
| `x` | Cancela con drain: lo que está en vuelo termina, el batch queda reanudable. |
| `p` / `r` | Pausa y reanuda (132). Cooperativo, no mata nada. |
| `+` / `-` | Mueve el techo de workers en caliente (133), sin confirmación. |

Si la sesión CMIS expira a mitad de corrida, la consola **no quema documentos**: pausa sola, te avisa y te lleva a `[2]`. Cargás la credencial nueva, volvés a `[6]`, apretás `r` y el documento que comió el 401 se reintenta él mismo.

**`7` BATCHES** te da la tabla con la auditoría de cada corrida (quién, dónde, con qué config), `↵` para el detalle, `R` para reintentar los fallidos y `E` para exportar. **`8` SYNC** es `sync status | recover | resolve` con botones.

**`9` YAML.** El archivo completo, como un formulario generado del schema (137–139): `v` valida, `w` escribe con confirmación y backup, `u` descarta el borrador. `connections` sigue siendo cosa de `[2]` — acá se ve, no se edita.

> El paso a paso completo, con un Alfresco en Docker y qué apretar en cada pantalla, está en [`how-to/probar-la-consola.md`](../how-to/probar-la-consola.md). La referencia seca de teclas y flags, en [`reference/cli.md`](../reference/cli.md#console--consola-de-operación-123135). El porqué de cada decisión de diseño, en [`explanation/operations-console.md`](../explanation/operations-console.md).

---

## `doctor`

Pre-flight validation **sin** correr la pipeline. Chequea config, conectividad, mapping completeness, metadata sources, alineación de tipos CM, existencia de folders, propiedades CMIS.

**Flags:** `--config` (required), `--check` (default `all`), `--log-level`.

`--check` acepta tres niveles (126): `all`, un **grupo** (`connections | mapping | metadata | cm-types | cm-targets`) o el **nombre de un check individual**.

```bash
cmcourier doctor --config prod.yaml --check all
cmcourier doctor --config prod.yaml --check connections          # solo conectividad
cmcourier doctor --config prod.yaml --check mssql_connectivity   # un solo check (130)
```

Los doce checks con su grupo están en [`reference/cli.md`](../reference/cli.md#doctor); dentro de la consola, `?` te los lista.

**Exit codes:** 0 todos pasan, 1 alguno falla, 2 la config no carga, 3 el doctor crasheó.

> Profundizamos en el [tutorial 05](05-doctor-deep-dive.md).

---

## Grupo `batch` — gestión de batches

Cuatro subcomandos para introspectar y operar sobre batches ya ejecutados.

### `batch list`

Lista los batches conocidos por el tracking DB, opcionalmente filtrando por status.

```bash
cmcourier batch list --config prod.yaml
cmcourier batch list --config prod.yaml --status in_progress   # o completed
```

### `batch show`

Detalle de un batch puntual: contadores por stage, fallos, timing. El `batch_id` es **posicional**.

```bash
cmcourier batch show marzo-2026 --config prod.yaml
```

### `batch retry-failed`

Re-corre los docs que quedaron `S{N}_FAILED` en un batch. Opcionalmente filtrar por stage. Ojo: la flag es `--batch`, no `--batch-id`.

```bash
cmcourier batch retry-failed --config prod.yaml --batch marzo-2026
cmcourier batch retry-failed --config prod.yaml --batch marzo-2026 --stage S5
```

### `batch export-report`

Exporta el detalle del batch a CSV o JSON. `--format` es obligatorio; sin `--output` va a stdout.

```bash
cmcourier batch export-report --config prod.yaml --batch marzo-2026 --format csv
```

---

## Grupo `inspect` — introspección del tracking DB

### `inspect rvabrep`

Resuelve cómo se enriquecería un trigger contra RVABREP. Útil para validar que el shortname + system_id matchee algo.

```bash
cmcourier inspect rvabrep JUAN_PEREZ 1 --config prod.yaml
```

### `inspect mapping`

Muestra el mapeo RVI → CM (folder, type, fields requeridos) para un `ID RVI`. El id va posicional.

```bash
cmcourier inspect mapping CC03 --config prod.yaml
```

### `inspect mapping-stats`

Estadísticas del archivo de mapping: cuántos códigos RVI, cuántos están mapeados, cuáles huérfanos.

```bash
cmcourier inspect mapping-stats --config prod.yaml
```

### `inspect trigger`

Vista previa de los primeros N triggers que el source produciría. Con `--source` podés overridear el origen sin tocar el YAML.

```bash
cmcourier inspect trigger --config prod.yaml --limit 20
cmcourier inspect trigger --config prod.yaml --source "csv:/tmp/lote.csv"
cmcourier inspect trigger --config prod.yaml --source "single_doc:JUAN_PEREZ,1"
```

---

## `as400-query`

Passthrough para correr cualquier SELECT contra AS400 desde la línea de comandos. Útil para validar conectividad o probar queries antes de meterlas en el config.

El SQL va **posicional**, no en una flag.

```bash
cmcourier as400-query \
  "SELECT COUNT(*) FROM RVILIB.RVABREP WHERE ABABCD LIKE 'JUAN%'" \
  --config prod.yaml
```

> El config se usa solo para sacar la conexión AS400. Las credenciales vienen de `AS400_USERNAME` / `AS400_PASSWORD`. Las celdas se truncan a 80 chars, y lo que salga por pantalla es responsabilidad tuya: este comando no enmascara PII.

---

## `background`

Punto de entrada cron-friendly. Toma un lock fcntl por config (un run a la vez por archivo de config) y dispara una pipeline en background con logs a archivo.

```bash
cmcourier background --config prod.yaml --pipeline csv-trigger
```

El lock vive en un archivo derivado del path del config (`<runtime_dir>/cmcourier/<hash>.lock`). Si otro proceso ya lo tiene, el segundo sale con **exit code 75** (`EX_TEMPFAIL`) — cron-friendly: reintenta en la próxima ventana sin mandarte un mail de error.

Es el **mismo lock** que toma la consola al lanzar desde `[5]`: un `background` en curso bloquea un lanzamiento desde la consola sobre esa config, y viceversa. Es por config y por estación — no ve otras máquinas.

`background` no acepta `--total` ni `--max-duration`; para acotar una corrida desatendida por tiempo usá el comando de pipeline directo con `--no-tui`.

---

## Grupo `analyze` — análisis offline de logs

Tres subcomandos que leen los `app-*.jsonl` y `system-*.jsonl` de `logs/` para sacar conclusiones después del run.

### `analyze batch`

Analiza un batch puntual. Desde 053 incluye breakdown por stage con clasificador de bottleneck (`upload-bound` vs `assembly-bound` vs `metadata-bound` etc.).

```bash
cmcourier analyze batch marzo-2026 --config prod.yaml
```

### `analyze compare`

Compara dos batches lado a lado — qué stage cambió, throughput delta. Los dos ids van posicionales.

```bash
cmcourier analyze compare feb-2026 marzo-2026 --config prod.yaml
```

### `analyze trends`

Tendencias a lo largo de los últimos N batches.

```bash
cmcourier analyze trends --config prod.yaml --last 20
```

> Los tres aceptan `--log-dir` en vez de `--config` (útil cuando tenés los JSONL pero perdiste el YAML) y `--format json` para pipear a `jq`.

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

Los cuatro rangos de tamaño son obligatorios (aceptan sufijos `kb`/`mb`/`gb`), y el destino es `--root`:

```bash
cmcourier mock generate \
  --rvabrep-csv /tmp/rvabrep-fake.csv \
  --root /tmp/synthetic-pool \
  --pdf-min 10kb --pdf-max 2mb \
  --img-min 5kb --img-max 500kb \
  --limit 1000 --seed 42
```

`--dry-run` imprime el plan sin escribir nada — usalo siempre antes de materializar unos cuantos GB.

### `mock rvabrep`

Genera un CSV con forma RVABREP determinista por semilla. Escribe a `--output`, no a stdout.

```bash
cmcourier mock rvabrep --rows 100000 --seed 42 --output /tmp/rvabrep-fake.csv
```

Encadenable con `mock generate`: `mock rvabrep` te da el RVABREP, `mock generate` te da los archivos en disco, y `local-scan-pipeline` te da una corrida completa de extremo a extremo sin tocar AS400 ni el file server real.

> Para una prueba de stress donde ni siquiera querés materializar los archivos, existe el camino contrario: `assembly.synthetic_content.enabled: true` en el YAML (102) hace que S4 genere el PDF en memoria por cada documento. Nunca lo prendas contra una migración real — los bytes que suben son sintéticos.

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

Limpia el cache. Exactamente uno de `--txn`, `--all` o `--older-than <minutos>`.

```bash
cmcourier cache clear --config prod.yaml --all
cmcourier cache clear --config prod.yaml --older-than 1440   # más viejas que 24 h
cmcourier cache clear --config prod.yaml --txn 0001234
```

---

## Cheat sheet — qué comando para qué problema

| Tarea | Comando |
|-------|---------|
| Operar una migración de punta a punta | `console` |
| Correr una pipeline | `<pipeline-name> run` |
| Validar el config antes de correr | `doctor` |
| Probar un solo check del doctor | `doctor --check <nombre>` |
| Ver el estado de un batch viejo | `batch show <batch_id>` |
| Re-correr los fallos | `batch retry-failed --batch <batch_id>` |
| Diagnosticar un doc puntual | `single-doc run` o `inspect trigger` |
| Validar conectividad AS400 | `as400-query "SELECT 1 FROM SYSIBM.SYSDUMMY1"` |
| Cron de migración | `background` |
| Acotar una corrida por tiempo | `<pipeline-name> run --max-duration 2h` |
| Ver bottlenecks de un run | `analyze batch <batch_id>` o `diagnose --latest` |
| Limpiar cache de metadata | `cache clear --older-than <minutos>` |
| Generar inputs para staging | `mock rvabrep` + `mock generate` |
| Reparar NIARVILOG tras un `periodic` | `sync recover` (dry-run) → `--apply` |
| Setear shell autocompletion | `completion <shell>` |

---

## Convenciones comunes a (casi) todos los comandos

- `--config <path>` es casi siempre **required**. El YAML define adónde escribir, contra qué conectar, qué credenciales pedir al entorno. En los grupos `batch`, `inspect` y `cache` también responde al alias corto `-c`.
- Los **ids van posicionales**, no en flags: `batch show <batch_id>`, `inspect mapping <id_rvi>`, `analyze batch <batch_id>`, `sync resolve <txn>`, `as400-query <sql>`. La excepción es `batch retry-failed`, que usa `--batch`.
- `--log-level` toma `DEBUG | INFO | WARNING | ERROR` (default `INFO`; `WARNING` en `background` y en `console`).
- Los comandos de pipeline aceptan `--tui / --no-tui`. En CI siempre `--no-tui`.
- Exit code 0 = éxito; 1 = ran con failures; 2 = error de config; 3 = excepción no manejada; 75 = `background` con el lock tomado.

---

## Siguientes pasos

- [05 — `doctor` en profundidad](05-doctor-deep-dive.md): el comando que más vas a usar antes de cada corrida
- [06 — Tu primera corrida streaming](06-first-streaming-run.md): correr de verdad con la TUI
- [07 — Debugging de un batch fallido](07-debugging-a-failed-batch.md): usando `inspect`, `analyze`, `batch retry-failed`
- [`docs/how-to/probar-la-consola.md`](../how-to/probar-la-consola.md): la consola paso a paso contra un Alfresco local
- [`docs/explanation/operations-console.md`](../explanation/operations-console.md): por qué la consola es como es
- [`docs/how-to/log-analysis.md`](../how-to/log-analysis.md): integrar `analyze` en CI
