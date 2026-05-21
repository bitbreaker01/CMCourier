> [← Volver al índice](../INDEX.md) · [Tutoriales](README.md)

# 01 — El YAML de Configuración

CMCourier se controla con un único archivo YAML. Todo lo que hace una corrida — qué documentos elegir, contra qué CMIS subirlos, cómo resolver metadatos, cuántos workers usar, qué loguear — vive ahí adentro. En este tutorial recorrés el archivo de afuera hacia adentro: arrancamos con un YAML mínimo de tres secciones y vamos sumando.

La fuente de verdad del schema es `src/cmcourier/config/schema.py`. Todos los modelos son Pydantic v2 con `frozen=True, extra="forbid"` — un typo en una key te explota al cargar, no en runtime. Eso es a propósito.

> Si querés ver TODAS las opciones en un solo archivo anotado, leé [`docs/reference/config-reference.yaml`](../reference/config-reference.yaml). Acá tomamos un camino didáctico, no exhaustivo.

---

## El YAML mínimo

El config más chico que carga tiene tres secciones obligatorias: `trigger`, `cmis`, `tracking` — y por consecuencia también `indexing`, `mapping`, `metadata`, `assembly` (todos required en `PipelineConfig`). Veámoslo entero antes de descomponerlo.

```yaml
trigger:
  kind: csv
  csv_path: /data/triggers.csv

indexing:
  source:
    kind: csv
    csv_path: /data/rvabrep.csv

mapping:
  csv_path: /data/MapeoRVI_CM.csv

metadata:
  field_aliases: {}
  field_sources: {}
  sources: []

assembly:
  source_root: /mnt/banking-images
  temp_dir: /tmp/cmcourier
  image_type_map:
    B: image/tiff
    O: application/pdf
    C: image/jpeg

cmis:
  base_url: http://localhost:8080/alfresco/api/-default-/public/cmis/versions/1.1/browser
  repo_id: ""

tracking:
  db_path: /var/cmcourier/tracking.sqlite
```

Si las rutas existen y CMIS responde, esto ya levanta. No usa AS400, no usa metadatos resueltos por fuente, no usa lanes, no usa AIMD — el mínimo defendible para una corrida de prueba. Ahora bajamos sección por sección y agregamos potencia.

---

## `trigger` — quién decide qué se sube

Discriminated union por `kind`. Es S0 del pipeline: cómo se descubren los documentos a migrar.

| `kind` | Para qué |
|--------|----------|
| `csv` | Una lista externa te dice qué procesar (shortname + CIF + system_id por fila) |
| `rvabrep` | Querés todo lo que matchea filtros sobre la tabla RVABREP |
| `local_scan` | Los archivos ya están extraídos a un directorio local; cada uno se cruza contra RVABREP |
| `single_doc` | Diagnóstico — un solo documento, parámetros por CLI |

```yaml
# CSV-triggered: el caso clásico, banca te pasa un Excel con la lista
trigger:
  kind: csv
  csv_path: /data/lote-marzo.csv
  shortname_column: ShortName              # default "ShortName"
  cif_column: CIF                          # default "CIF"
  system_id_column: SystemID               # default "SystemID"
```

```yaml
# RVABREP-direct: scanear la tabla maestra con filtros
trigger:
  kind: rvabrep
  filters:
    systems: ["1", "3"]                    # solo system_ids 1 y 3
    document_types: ["CC03", "FF17"]       # solo estos códigos RVI
```

```yaml
# Local scan: los archivos ya están en disco
trigger:
  kind: local_scan
  scan_path: /mnt/extracted-docs
```

`single_doc` no lleva campos en el YAML — los `--shortname / --system / --cif` los pasás por CLI. Es la pipeline diagnóstica, no producción.

> Para entender cuándo elegir cada una, saltá al [tutorial 02](02-pipelines-and-how-to-use-them.md).

---

## `indexing` — la tabla maestra RVABREP

S1 mira RVABREP para enriquecer cada trigger. La fuente puede ser un CSV (testing/staging) o el AS400 real (producción).

```yaml
# Variante CSV
indexing:
  source:
    kind: csv
    csv_path: /data/rvabrep-snapshot.csv
  batch_size: 50                           # default 50, ≥ 1
```

```yaml
# Variante AS400 — query libre, output con forma RVABREP
indexing:
  source:
    kind: as400
    connection:
      host: as400.banco.example
      port: 446                            # default 446
      database: RVILIB                     # default "RVILIB"
      driver: "iSeries Access ODBC Driver" # default
    query: "SELECT * FROM RVILIB.RVABREP WHERE ABABCD = ?"
  batch_size: 50
```

La sub-key `columns` mapea nombres lógicos a físicos. Los defaults son los nombres canónicos del AS400 (`ABABCD`, `ABAACD`, etc.); solo la tocás si tu instalación los renombró.

> El `batch_size` de `indexing` (default 50) es **distinto** del `batch_size` top-level (default 1000). El primero es cuántas filas RVABREP traer por viaje a la DB; el segundo es cuántos triggers entran a un chunk del orquestador. No los confundas.

---

## `mapping` — RVI → Content Manager

S2 traduce el código de tipo RVI a la clase documental de Content Manager (carpeta destino, tipo de objeto, columnas obligatorias). Hay dos modos — elegís uno.

```yaml
# Modo consolidado (un solo CSV, formato viejo, usado en tests)
mapping:
  csv_path: /data/MapeoRVI_CM.csv
```

```yaml
# Modo split (dos CSVs, formato de producción desde 035)
mapping:
  rvi_cm_csv_path: /data/MapeoRVI_CM.csv
  metadatos_csv_path: /data/MetadatosCM.csv
```

El modo split separa el mapeo (`MapeoRVI_CM.csv`: código RVI → CMIS folder + type) del catálogo de metadatos por clase (`MetadatosCM.csv`: por clase CM, qué propiedades existen, cuáles son obligatorias, tipos). Los samples viven en [`reference-data/csv/`](../../reference-data/csv/).

---

## `metadata` — la cadena de fallback

S3 resuelve el valor de cada propiedad CMIS recorriendo las fuentes en orden hasta que alguna devuelva algo. Esta es la sección con más juego.

```yaml
metadata:
  # alias lógico → nombre físico de columna en la fuente
  field_aliases:
    nombre_cliente: NombreCompleto
    fecha_alta: FechaAlta

  # para cada propiedad CMIS, dónde buscar el valor
  field_sources:
    cm:nombre_cliente:
      sources:
        - source_type: trigger              # primero, el propio trigger
          field: shortname
        - source_type: "csv:clientes"       # después, el CSV "clientes"
          alias: nombre_cliente
      default_value: "(sin nombre)"
    cm:cif:
      sources:
        - source_type: trigger
          field: cif

  # las fuentes nombradas (referenciadas con "csv:nombre" o "as400:nombre")
  sources:
    - name: clientes
      kind: csv
      path: /data/clientes.csv
      key_columns: ["CIF"]
    - name: cuentas
      kind: as400
      connection: { host: as400.banco.example }
      query: "SELECT * FROM RVILIB.CUENTAS WHERE CIF = ?"
      key_columns: ["CIF"]

  prefetch_enabled: true                    # default true
  cache:
    enabled: false                          # default false
    ttl_minutes: 60                         # default 60, rango 1..43200
```

`source_type` puede ser `"trigger"`, `"rvabrep"`, `"csv:<nombre>"` o `"as400:<nombre>"`. Si la cadena entera no resuelve y hay `default_value`, se usa ese; si no hay default, S3 falla con `DefaultValidationFailedError`.

### Campos que vale la pena entender

| Campo | Qué hace |
|-------|----------|
| `prefetch_enabled: true` | Pre-carga las fuentes en memoria al inicio del chunk — más rápido si vas a iterar miles de docs |
| `cache.enabled: true` | Habilita el `document_cache` cross-batch (037) — guarda `(txn_num, fields_hash) → properties_json` para saltar S3 en docs que ya viste |
| `cache.ttl_minutes` | Cuánto vive una entrada del cache antes de re-resolverla |

Si movés `prefetch_enabled` a `false`, cada `get_by_fields` golpea la fuente — útil cuando la fuente es enorme y la mayoría de los docs no la van a tocar.

---

## `assembly` — ensamblado de PDF

S4 toma los archivos de imagen del archivo bancario y los convierte a PDF (o passthrough si ya son PDF).

```yaml
assembly:
  source_root: /mnt/banking-images          # raíz del archivo de imágenes
  temp_dir: /tmp/cmcourier                  # se crea en runtime
  image_type_map:
    B: image/tiff                           # B → TIFF (se convierte a PDF)
    O: application/pdf                      # O → PDF nativo (passthrough)
    C: image/jpeg                           # C → JPEG (se convierte)
```

`image_type_map` mapea la columna `image_type` de RVABREP al MIME type real del archivo en disco. El assembler elige el camino según el MIME: `img2pdf` para el fast path, `Pillow` cuando el TIFF está en LZW, `PyPDF2` para mergeo.

---

## `cmis` — destino y red

```yaml
cmis:
  base_url: http://alfresco.banco.example:8080/alfresco/api/-default-/public/cmis/versions/1.1/browser
  repo_id: ""                               # Alfresco usa "" (singleton)
  timeout_seconds: 300.0                    # default 300, > 0
  verify_ssl: false                         # default false
  max_bandwidth_mbps: 0.0                   # 0 = sin límite, default 0
  retry_max_attempts: 3                     # default 3, ≥ 1
  retry_base_delay_s: 2.0                   # default 2.0, ≥ 0
  workers: 4                                # default 4, ≥ 1 — tamaño inicial del pool S5
  http2: true                               # default true (089) — false fuerza HTTP/1.1
  upload_chunk_bytes: 1048576               # default 1 MiB (090) — chunk del multipart encoder
  auto_tune:                                # AIMD
    enabled: false                          # default false
    min_threads: 2                          # default 2
    max_threads: 50                         # default 50 — el techo absoluto
    target_p95_ms: 5000.0                   # default 5000
    adjustment_interval_s: 30               # default 30
    warmup_seconds: 60                      # default 60
    min_samples: 20                         # default 20 (061)
    growth_factor: 1.25                     # default 1.25 (068)
    halve_factor: 0.75                      # default 0.75 (068)
    halve_threshold_ratio: 1.5              # default 1.5 (068)
```

### Campos críticos — qué pasa si los movés

| Campo | Si lo subís | Si lo bajás |
|-------|-------------|-------------|
| `workers` | Más uploads concurrentes al inicio. Si el server CMIS no banca, vas a ver 5xx y el circuit breaker disparándose. Con AIMD encendido es solo el punto de partida — el pool se redimensiona solo. |
| `max_bandwidth_mbps` | Token bucket compartido entre workers. Útil cuando la red del banco no quiere que satures. `0` = sin límite. |
| `auto_tune.max_threads` | El techo real del pool durante el run. Si subís este sin subir `workers`, AIMD escala desde el inicial hasta acá según latencia. |
| `auto_tune.growth_factor` | Crecimiento multiplicativo cuando el p95 está debajo del target. `1.25` = +25% por tick. Subirlo (a `1.5` p.ej.) llega al techo más rápido pero arriesga overshoot. |
| `auto_tune.halve_threshold_ratio` | A qué múltiplo del target reacciona el halve. `1.5` = halvear cuando el p95 supera 1.5× target. Bajarlo te hace más reactivo (más cauteloso); subirlo te hace más estable contra outliers. |

> Para el detalle del algoritmo AIMD ver la sección 10 del [dossier](../_internal/dossier.md) y el [tutorial 06](06-first-streaming-run.md).

---

## `tracking` — la SQLite de idempotencia

```yaml
tracking:
  db_path: /var/cmcourier/tracking.sqlite
  as400_sync:
    enabled: false                          # default false
    mode: claim                             # "claim" (default) o "periodic" (096)
    # connection: { host: ... }            # required si enabled
    # library: RVILIB                       # default
    # table: NIARVILOG                      # default
    # columns: { ... }                      # mapeo de 15 columnas NIARVILOG
    # stale_in_progress_minutes: 30         # default 30, rango 1..1440
    # retry_attempts: 3                     # default 3
    # retry_base_delay_s: 5.0               # default 5.0
    # periodic:                             # required si mode: periodic
    #   interval_minutes: 5                 # default 5, rango 1..1440
```

La SQLite mantiene la state machine por documento: `S0_PENDING → S0_DONE → S1_PENDING → ... → S5_DONE | S5_FAILED`. Es la fuente de verdad para idempotencia: si `is_uploaded(txn_num)` devuelve `True`, el próximo run lo skipea con `S1_SKIPPED` (062).

`as400_sync` es para idempotencia distribuida (034) — cuando múltiples instancias de CMCourier corren en paralelo contra el mismo CMIS, sincronizan estado vía la tabla NIARVILOG en AS400. **`mode`** (096) elige cómo: `claim` usa un claim atómico por-documento en S5 (previene doble-upload); `periodic` saca AS400 del hot path — S5 escribe solo SQLite y un reconciliador de fondo propaga cada `periodic.interval_minutes`. Ojo: `periodic` NO previene doble-upload (los conflictos se detectan post-hoc) — usalo solo si no hay un migrador competidor.

---

## `observability` — logs, métricas, PII

```yaml
observability:
  enabled: true                             # default true
  pipeline_metrics: true                    # default true
  network_metrics: true                     # default true
  system_metrics:
    enabled: true                           # default true
    sample_interval_s: 5.0                  # default 5, rango 1..60
  log_dir: ./logs                           # default "./logs"
  log_format: json                          # default "json" (alt: "text")
  rotation_mb: 100                          # default 100, ≥ 1
  retention_days: 30                        # default 30, ≥ 1
  slow_op_threshold_ms: 5000                # default 5000
  slow_op_top_n: 20                         # default 20
  unmask_pii: false                         # default false — TRUE prende warning en doctor
```

Los logs JSON van a `{log_dir}/app-{date}.jsonl` y se rotan por tamaño. El `SystemSample` (Tier 5) escribe a `system-{date}.jsonl` cada `sample_interval_s` con CPU/RAM/disk/net del proceso. Las ops sobre `slow_op_threshold_ms` se agregan y el Top-N se vuelca a `slow-ops-{date}.jsonl` al cierre del batch.

`unmask_pii: true` quita el masking de los shortnames y CIFs en logs. Útil para debugging local, **prohibido en producción** — el `doctor` te lanza un warning.

---

## `processing` — modo y paralelismo

Esta sección controla cómo se ejecuta el pipeline. Es donde decidís batched vs streaming, cuántos workers de prep usás, si activás lanes, y si S4 usa procesos.

```yaml
processing:
  mode: batched                             # "batched" (default) o "streaming"
  batches_in_flight: 2                      # 1..2, default 2 — ignorado en streaming
  prep_workers: 1                           # default 1, ≥ 1 — S2/S3/S4
  s4_use_processes: true                    # default true (066)
  s4_max_processes: null                    # null = os.cpu_count()
  s4_smart_routing: false                   # default false (094)
  streaming:
    bucket_size: 100                        # default 100 — solo aplica en mode: streaming
  heavy_light_lanes:
    enabled: false                          # default false
    heavy_threshold_bytes: 10485760         # default 10 MB
    heavy_lane_min_batch: 50                # default 50, mínimo para activar
    heavy_initial_ratio: 0.2                # default 0.2
    rebalance_interval_s: 10.0              # default 10
    idle_threshold_s: 15.0                  # default 15
```

### Campos críticos

| Campo | Qué pasa si lo movés |
|-------|----------------------|
| `mode: streaming` | Cambia el orquestador completo. Memoria peak colapsa a ~`bucket_size`. Pero pierde resume (rechaza `from_stage > 1`). |
| `bucket_size` | Cola bounded entre prep y upload. Más grande = más buffer (mejor para amortizar pausas), menos elasticidad. Default 100 está bien para la mayoría. |
| `prep_workers` | Threads en S2/S3/S4. `1` es serial — byte-idéntico al pre-056. Subilo si tu cuello de botella es resolución de metadatos (S3) o ensamblado en TIFF pesado (S4). |
| `s4_use_processes: true` | Default desde 066. Saltea el GIL para `img2pdf`/`PIL`/`PyPDF2`. Si lo apagás volvés a serializar el ensamblado contra el GIL — solo apagalo si tenés sospecha de fork bugs. |
| `s4_smart_routing: true` | (094) Con el process pool activo, rutea los PDF nativos inline (su trabajo es `shutil.copy2`, I/O-bound, libera el GIL) y manda solo los paginados TIFF/JPEG al pool. Evita el overhead de pickle/IPC/spawn — clave en Windows. Default `false`. |
| `batches_in_flight: 2` | Solo en batched. N=2 = overlap (mientras chunk K sube, K+1 prepara). Bajarlo a 1 desactiva el overlap pero es más predecible. |
| `heavy_light_lanes.enabled: true` | Activa lanes adaptativos en S5 — separa docs ≥ 10 MB del resto. Para que valga la pena necesitás `heavy_lane_min_batch` (default 50) docs por chunk. |

> Para el detalle de modos, leé el [tutorial 03](03-execution-modes-batched-vs-streaming.md).

---

## `batch_size` (top-level)

```yaml
batch_size: 1000                            # default 1000, ≥ 1
```

Cuántos triggers entran a un chunk del orquestador batched. Es el tamaño "natural" del batch desde el punto de vista del operador (la unidad de reporte, la unidad de resume). En streaming se sigue usando para sembrar las métricas, pero el bucket es el que manda.

---

## Un YAML productivo, real

Juntando todo, así se ve un config de producción razonable:

```yaml
trigger:
  kind: csv
  csv_path: /data/lote-marzo.csv

indexing:
  source:
    kind: as400
    connection:
      host: as400.banco.example
      database: RVILIB
    query: "SELECT * FROM RVILIB.RVABREP"
  batch_size: 50

mapping:
  rvi_cm_csv_path: /data/MapeoRVI_CM.csv
  metadatos_csv_path: /data/MetadatosCM.csv

metadata:
  field_aliases:
    nombre_cliente: NombreCompleto
  field_sources:
    cm:nombre_cliente:
      sources:
        - { source_type: trigger, field: shortname }
        - { source_type: "csv:clientes", alias: nombre_cliente }
      default_value: "(sin nombre)"
  sources:
    - { name: clientes, kind: csv, path: /data/clientes.csv, key_columns: [CIF] }
  cache:
    enabled: true
    ttl_minutes: 360

assembly:
  source_root: /mnt/banking-images
  temp_dir: /tmp/cmcourier
  image_type_map: { B: image/tiff, O: application/pdf, C: image/jpeg }

cmis:
  base_url: https://cm.banco.example/cmis/.../browser
  repo_id: ""
  max_bandwidth_mbps: 200
  workers: 4
  auto_tune:
    enabled: true
    max_threads: 32

tracking:
  db_path: /var/cmcourier/tracking.sqlite
  as400_sync:
    enabled: true
    connection: { host: as400.banco.example, database: RVILIB }

observability:
  log_dir: /var/log/cmcourier
  retention_days: 90

processing:
  mode: streaming
  prep_workers: 8
  streaming:
    bucket_size: 200
  heavy_light_lanes:
    enabled: true

batch_size: 5000
```

Streaming + lanes + AIMD + cache + AS400 sync. Esto es lo que un dry-run productivo termina pareciendo.

---

## Validar un config sin correr el pipeline

Antes de disparar nada, pasalo por `doctor`:

```bash
cmcourier doctor --config /etc/cmcourier/config.yaml
```

Eso valida Pydantic, conectividad CMIS/AS400, completeness del mapping, sources de metadata, alineación de tipos CM, existencia de folders, y propiedades. Si algo falla, no movés el lote — fixeás primero. Detalle completo en el [tutorial 05](05-doctor-deep-dive.md).

---

## Siguientes pasos

- [02 — Pipelines y cuándo usarlas](02-pipelines-and-how-to-use-them.md): ahora que tenés un config, cuál pipeline lanzar
- [03 — Batched vs streaming](03-execution-modes-batched-vs-streaming.md): por qué elegir uno u otro
- [05 — `doctor` en profundidad](05-doctor-deep-dive.md): validar el config antes de correr
- [`docs/reference/config-reference.yaml`](../reference/config-reference.yaml): el archivo con TODAS las opciones anotadas
