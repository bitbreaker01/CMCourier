> [← Volver al índice](../INDEX.md) · [Tutoriales](README.md)

# 07 — Debugging de un Batch Fallido

En este tutorial provocás un fallo a propósito, vas a las herramientas que tiene CMCourier para diagnosticarlo, identificás el documento puntual, fixeás el problema y reanudás. Cuando termines vas a tener la rutina de operador en la cabeza.

Lo que vamos a usar: el tracking DB SQLite, los logs JSON, `cmcourier inspect`, `cmcourier analyze batch`, `cmcourier batch show` y `cmcourier batch retry-failed`.

---

## La state machine que tenés que entender

Cada documento avanza por estados en `migration_log`. La fuente de verdad está en `src/cmcourier/domain/`:

```
S0_PENDING → S0_DONE
S1_PENDING → S1_DONE / S1_SKIPPED / S1_FILTERED
S2_PENDING → S2_DONE / S2_FAILED
S3_PENDING → S3_DONE / S3_FAILED
S4_PENDING → S4_DONE / S4_FAILED
S5_PENDING → S5_DONE / S5_FAILED
```

Tres estados terminales que **no son fallos**:

| Estado | Significado |
|--------|-------------|
| `S5_DONE` | El doc se subió al CMIS, tiene `cm_object_id` |
| `S1_SKIPPED` | Ya estaba subido en un batch anterior (idempotencia, 062) |
| `S1_FILTERED` | RVABREP marca al doc con código de borrado — no se sube por diseño (051) |

Los demás son fallos por stage. Cada uno te dice **dónde** falló, no por qué — para el por qué tenés los logs y el `error_message` en la fila.

---

## Paso 1 — Provocar un fallo controlado

Hay dos formas fáciles de provocar fallos en staging para practicar:

### Opción A: apagar CMIS

Si tenés Alfresco en Docker:

```bash
docker stop cmcourier-alfresco
```

Cualquier upload va a fallar con `CMISServerError` (5xx) → retries agotados → `RetriesExhaustedError` → `S5_FAILED`.

### Opción B: borrar archivos source

Antes de correr, remové un par de archivos del `assembly.source_root`:

```bash
rm /tmp/synthetic/pool/doc_003.tiff
rm /tmp/synthetic/pool/doc_017.tiff
```

S4 va a fallar con `SourceFileMissingError` cuando intente ensamblar esos docs. Resto pasa normal.

Ahora corré la pipeline. Para esta práctica usá `mode: batched` (porque vamos a hacer resume después y streaming no lo soporta):

```yaml
processing:
  mode: batched
batch_size: 100
```

```bash
cmcourier csv-trigger-pipeline run \
  --config staging.yaml \
  --batch-id debugging-tutorial \
  --no-tui
```

Termina con exit code 1. La consola te muestra el resumen con conteos de done/failed.

---

## Paso 2 — Mirá el resumen del batch

```bash
cmcourier batch show --config staging.yaml --batch-id debugging-tutorial
```

Output esperado:

```
Batch: debugging-tutorial
  total_records : 500
  started_at    : 2026-05-15T14:23:11
  completed_at  : 2026-05-15T14:24:47

Status breakdown:
  S5_DONE       : 498
  S4_FAILED     : 2
  S1_SKIPPED    : 0
  S1_FILTERED   : 0
```

Te dice que 2 docs murieron en S4. Ahora hay que verlos puntualmente.

Si querés exportar el detalle a CSV para abrirlo en Excel/pandas:

```bash
cmcourier batch export-report \
  --config staging.yaml \
  --batch-id debugging-tutorial \
  --format csv > /tmp/report.csv
```

---

## Paso 3 — Encontrar los txn_num afectados

`migration_log` guarda el `rvabrep_txn_num` por fila. Una consulta SQL directa va al grano:

```bash
sqlite3 /tmp/cmcourier-staging.sqlite "
  SELECT rvabrep_txn_num, rvabrep_file_name, error_message
  FROM migration_log
  WHERE batch_id = 'debugging-tutorial' AND status LIKE 'S%_FAILED'
"
```

Output:

```
TXN0003|doc_003.tiff|Source file missing: /tmp/synthetic/pool/doc_003.tiff
TXN0017|doc_017.tiff|Source file missing: /tmp/synthetic/pool/doc_017.tiff
```

Listo — sabés los dos `txn_num` (`TXN0003`, `TXN0017`), el archivo afectado, y el mensaje de error. La excepción es `SourceFileMissingError` (S4).

> El esquema completo de `migration_log` está documentado en la sección 12 del dossier. Los índices clave son `UNIQUE (rvabrep_txn_num, batch_id)` y `INDEX ON (rvabrep_txn_num) WHERE status='S5_DONE'`.

---

## Paso 4 — Leer los logs JSON

Los logs viven en `observability.log_dir` (ejemplo `/tmp/cmcourier-logs/`). Estructura:

```
logs/
├── app-2026-05-15.jsonl              # log principal (todos los eventos)
├── system-2026-05-15.jsonl           # samples psutil (CPU/RAM/disk/net)
└── slow-ops-2026-05-15.jsonl         # ops sobre slow_op_threshold_ms
```

Buscar los eventos del doc problemático:

```bash
jq 'select(.txn_num == "TXN0003")' /tmp/cmcourier-logs/app-*.jsonl
```

O sin `jq`:

```bash
rg '"txn_num":"TXN0003"' /tmp/cmcourier-logs/app-*.jsonl
```

Vas a ver la secuencia de eventos: trigger acquired, indexed, mapped, resolved, y después un evento de error en S4 con el stack trace de `SourceFileMissingError`.

> En modo JSON cada línea es un evento atómico — fácil de pipear a `jq`. En modo `text` los logs son line-based humanos. Para producción siempre `json` (es el default).

---

## Paso 5 — Inspeccionar el trigger problemático

Antes de fixear, querés confirmar lo que ya sabés. `inspect trigger` te muestra cómo se ve el doc según CMCourier:

```bash
cmcourier inspect trigger \
  --config staging.yaml \
  --shortname JUAN_PEREZ \
  --system 1
```

Output:

```
Trigger:
  shortname  : JUAN_PEREZ
  cif        : 20123456789 (resolved from RVABREP)
  system_id  : 1

RVABREP row:
  txn_num    : TXN0003
  file_name  : doc_003.tiff
  doc_type   : CC03
  delete_code: (empty)

Mapping:
  doc_type   : CC03
  cm_folder  : /Bank/Clients/JUAN_PEREZ
  cm_type    : cm:document
  properties : (4 mapped)

Metadata:
  cm:name        → JUAN_PEREZ (from trigger.shortname)
  cm:cif         → 20123456789 (from trigger.cif)
  cm:doc_type    → CC03 (from rvabrep.doc_type)

Assembly:
  source_path: /tmp/synthetic/pool/doc_003.tiff  ← NOT FOUND
```

Te muestra exactamente dónde se cae. La línea `NOT FOUND` te confirma que el problema es de filesystem, no de config.

---

## Paso 6 — Análisis con `analyze batch`

Para ver el contexto más amplio del run (no solo el doc puntual):

```bash
cmcourier analyze batch \
  --config staging.yaml \
  --batch-id debugging-tutorial
```

Desde 053, este comando lidera con breakdown por stage y clasifica el bottleneck. Algo así:

```
Batch debugging-tutorial · 500 docs · elapsed 1m36s

Stage breakdown (% of total stage time):
  S0_trigger     :  2.1%
  S1_indexing    :  8.7%
  S2_mapping     :  3.2%
  S3_metadata    : 12.4%
  S4_assembly    : 18.6%
  S5_upload      : 55.0%   ← bottleneck

Verdict: upload-bound (S5 holds ≥ 45% of stage time)
Network summary: cmis_request avg=240ms p95=520ms, 0 retries

Failed docs: 2 (S4_FAILED)
  TXN0003 — SourceFileMissingError
  TXN0017 — SourceFileMissingError
```

Para el operador esto es oro: te dice si la corrida fue limitada por upload, por assembly, o por metadata. Es la diferencia entre "tunear AIMD" y "tunear S4 process pool".

---

## Paso 7 — Fixear el problema

En nuestro caso, el fix es triviall — restaurar los archivos:

```bash
cmcourier mock generate \
  --output /tmp/synthetic/pool \
  --count 500 \
  --seed 42 \
  --regenerate doc_003.tiff,doc_017.tiff
```

O si fue la opción A (CMIS apagado), reiniciar Alfresco:

```bash
docker start cmcourier-alfresco
sleep 10
cmcourier doctor --config staging.yaml --check connections
```

---

## Paso 8 — Reanudar el batch con `retry-failed`

Ahora corremos solo los docs fallados — no re-procesamos los 498 que ya están `S5_DONE`:

```bash
cmcourier batch retry-failed \
  --config staging.yaml \
  --batch-id debugging-tutorial
```

Opcionalmente, filtrá por stage:

```bash
cmcourier batch retry-failed \
  --config staging.yaml \
  --batch-id debugging-tutorial \
  --stage 4
```

`retry-failed` busca todas las filas del batch con estado `S{N}_FAILED` (o un stage específico si lo pasás), las re-procesa desde la stage que falló, y actualiza la SQLite.

> `retry-failed` **no es** un re-run completo. Es una pasada quirúrgica. Para re-correr el batch entero (raro, casi nunca lo querés), usás `csv-trigger-pipeline run` con el mismo `--batch-id` y `--from-stage 1`. Los `S5_DONE` se respetan vía `is_uploaded()` y se marcan `S1_SKIPPED`.

---

## Paso 9 — Confirmar que el batch cerró

Después del retry:

```bash
cmcourier batch show --config staging.yaml --batch-id debugging-tutorial
```

Esperás:

```
Status breakdown:
  S5_DONE       : 500
  S4_FAILED     : 0
```

Todo verde. El batch quedó cerrado, idempotente, auditable.

---

## Patrones de error y qué los causa

| Excepción | Stage | Causa típica | Primer movimiento |
|-----------|-------|--------------|-------------------|
| `RVABREPNotFoundError` | S1 | El shortname + system_id no matchea ninguna fila en RVABREP | `inspect rvabrep` para verificar |
| `RVABREPDeletedError` | S1 | Doc tiene código de borrado — no es fallo, va a `S1_FILTERED` | Es por diseño; nada que hacer |
| `RVABREPDuplicateError` | S1 | Múltiples filas matchean | Limpiar RVABREP o filtrar por más columnas |
| `IDRViNotMappedError` | S2 | El código RVI no está en el mapping CSV | Agregarlo al `MapeoRVI_CM.csv` |
| `SourceFailedError` | S3 | Toda la cadena de una propiedad falló y no hay `default_value` | Agregar default o arreglar la cadena |
| `DefaultValidationFailedError` | S3 | **Deprecada (149)**: el runtime ya no la levanta — el `default_value` no se valida | Nada. Si el default no matchea los patrones de sus fuentes, `types check` lo dice como INFO |
| `SourceFileMissingError` | S4 | El archivo no existe en `source_root` | Restaurar el archivo o ajustar el path |
| `PDFAssemblyFailedError` | S4 | `img2pdf`/`Pillow`/`PyPDF2` rompió | Logs te dan el detalle; TIFFs rotos típicamente |
| `CMISClientError` | S5 | 4xx del CMIS (auth, permisos, tipo inexistente) | `doctor --check cm-targets`; chequear credenciales |
| `CMISServerError` | S5 | 5xx del CMIS — se retenta automático | Si llega a `RetriesExhaustedError`, server caído o sobrecargado |
| `RetriesExhaustedError` | S5 | Agotó `retry_max_attempts` | Esperar, chequear server, o subir `retry_max_attempts` |
| `TrackingError` | S6 | Escritura a SQLite falló (no bloquea pipeline) | Chequear permisos / espacio en disco |

Todas heredan de `CMCourierError`. Las definiciones están en `src/cmcourier/domain/exceptions.py`.

---

## Cuándo NO hacer retry, y qué hacer en su lugar

| Situación | Qué hacer |
|-----------|-----------|
| Falló `S5_FAILED` con `CMISClientError 401` | **No retry.** Las credenciales están mal. Fixealas y `retry-failed` después. |
| Falló todo el batch porque el config tenía un typo en `mapping.csv_path` | **No retry.** El config está roto. Fixealo, re-corré con `--from-stage 1` (los docs `S5_DONE` se respetan). |
| Falló `S4_FAILED` con `PDFAssemblyFailedError` pero el archivo está bien | Probá ensamblarlo a mano (`single-doc run` con `--shortname X`). Si reproduce, el archivo está corrupto. |
| `S5_DONE` en SQLite pero el objeto no está en CMIS | Bug raro — `is_uploaded()` mintió. Revisar logs S5 con `jq`. |

---

## Idempotencia distribuida

Si tenés `tracking.as400_sync.enabled: true`, hay otra capa: NIARVILOG en AS400. Múltiples instancias de CMCourier (varios procesos, varios hosts) sincronizan estado vía esa tabla. Si una instancia se cuelga in-progress, después de `stale_in_progress_minutes` (default 30) otra instancia puede tomar el doc.

Para resolver stale entries:

```bash
cmcourier sync status --config prod.yaml
cmcourier sync resolve --config prod.yaml
```

Ver `docs/how-to/as400-sync.md` para detalle.

---

## Rutina del operador en 5 pasos

Cuando un batch falla, esta es la secuencia:

1. **`cmcourier batch show --batch-id X`** — cuántos failed y en qué stage.
2. **Query SQL al `migration_log`** — qué `txn_num` específicos y por qué.
3. **`cmcourier analyze batch --batch-id X`** — bottleneck classification para ver si es un patrón o un caso aislado.
4. **Fix de raíz** — archivos, config, conectividad, lo que sea.
5. **`cmcourier batch retry-failed --batch-id X`** — re-corre los fallidos, deja los buenos en paz.

Memorizá los cinco. Cuando los tengas internalizados, debuggear un batch te lleva 10 minutos.

---

## Siguientes pasos

- [`docs/how-to/as400-sync.md`](../how-to/as400-sync.md): idempotencia distribuida
- [`docs/how-to/document-cache.md`](../how-to/document-cache.md): cache cross-batch (037)
- [`docs/how-to/log-analysis.md`](../how-to/log-analysis.md): integrar `analyze` en pipelines de CI
- [Volver al índice de tutoriales](README.md)
