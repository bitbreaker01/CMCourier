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

## `connections` — el registro de conexiones (129)

Antes de las secciones que las usan, conviene entender de dónde salen las conexiones a bases de datos. Hay dos formas y una sola es la recomendada.

La forma **inline** es la histórica: escribís el objeto de conexión ahí donde se necesita.

```yaml
indexing:
  source:
    kind: as400
    connection:                            # objeto inline
      host: as400.banco.example
      database: RVILIB
    query: "SELECT * FROM RVILIB.RVABREP"
```

La forma **por registro** (129) declara las conexiones una vez arriba de todo, con un **alias**, y después las referencia por nombre:

```yaml
connections:
  rvi:                                     # alias
    kind: as400
    host: as400.banco.example
    port: 446                              # default 446
    database: RVILIB                       # default "RVILIB"
    driver: "iSeries Access ODBC Driver"   # default
    table: null                            # opcional
  clientes_sql:
    kind: mssql                            # 130 — SQL Server por ODBC
    host: 127.0.0.1
    port: 1433                             # default 1433
    database: cmcourier
    driver: "ODBC Driver 18 for SQL Server"  # default (msodbcsql18)
    encrypt: true                          # default true
    trust_server_certificate: false        # default false

indexing:
  source:
    kind: as400
    connection: rvi                        # ← el alias, no el objeto
    query: "SELECT * FROM RVILIB.RVABREP"
```

Tres lugares aceptan alias: `indexing.source.connection`, `metadata.sources[].as400_connection` y `tracking.as400_sync.connection`. El alias tiene que existir y su `kind` tiene que coincidir con lo que el sitio espera — un `source` AS400 apuntando a una conexión `mssql` es un error de carga, no una sorpresa en runtime.

El alias matchea `^[a-z][a-z0-9_]{0,31}$` y `cmis` está reservado (el destino tiene su propio bloque).

### De dónde salen las credenciales

**Nunca del YAML.** Cada alias lee su par de variables de entorno, en mayúsculas:

| Alias | Env vars |
|-------|----------|
| `cmis` | `CMIS_USERNAME` / `CMIS_PASSWORD` — siempre requeridas |
| `rvi` | `RVI_USERNAME` / `RVI_PASSWORD` |
| `clientes_sql` | `CLIENTES_SQL_USERNAME` / `CLIENTES_SQL_PASSWORD` |
| conexión **inline** | `AS400_USERNAME` / `AS400_PASSWORD` (alias implícito `as400`) |

Por eso vale la pena el registro: con dos AS400 distintos (el de RVABREP y el del sync NIARVILOG, por ejemplo) la forma inline te obliga a compartir un único par `AS400_*`; con alias, cada uno tiene el suyo.

> `doctor --check as400_connectivity` y `--check mssql_connectivity` prueban **cada** conexión del registro por separado, nombrando el alias y las env vars que falten.

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
    connection: rvi                        # alias del registro (129) …
    # connection:                          # … o el objeto inline de siempre
    #   host: as400.banco.example
    #   port: 446                          # default 446
    #   database: RVILIB                   # default "RVILIB"
    #   driver: "iSeries Access ODBC Driver" # default
    query: "SELECT * FROM RVILIB.RVABREP WHERE ABABCD = ?"
```

Sólo hay dos variantes de `source`: `csv` y `as400`. **No existe `indexing.source.kind: mssql`** — SQL Server (130) entró como fuente de *metadata*, no como origen de la tabla RVABREP.

La sub-key `columns` mapea nombres lógicos a físicos. Los defaults son los nombres canónicos del AS400 (`ABABCD`, `ABAACD`, etc.); solo la tocás si tu instalación los renombró.

> `indexing.batch_size` (default 50) es una **perilla muerta desde 121**: alimentaba el lookup batcheado que se eliminó, y hoy no la lee nadie. Sigue aceptada para que los YAML existentes carguen. El `batch_size` **top-level** (default 1000) sí está vivo: es cuántos triggers entran a un chunk del orquestador.

---

## `mapping` — RVI → Content Manager

S2 traduce el código de tipo RVI a la clase documental de Content Manager (carpeta destino, tipo de objeto, columnas obligatorias). Hay tres modos — elegís uno.

```yaml
# Modo consolidado (un solo CSV, formato viejo, usado en tests)
mapping:
  csv_path: /data/MapeoRVI_CM.csv
```

```yaml
# Modo manifest (145, RECOMENDADO para instalaciones nuevas) — MapeoRVI_CM.csv
# reducido a IDSistema,IDRVI,IDCM + un manifest JSON descargado del servidor.
mapping:
  rvi_cm_csv_path: /data/MapeoRVI_CM.csv
  type_manifest_path: /data/cm-type-manifest.json
```

```yaml
# Modo split (dos CSVs, formato de producción desde 035 — DEPRECADO por 145,
# sigue funcionando pero no es el recomendado para instalaciones nuevas)
mapping:
  rvi_cm_csv_path: /data/MapeoRVI_CM.csv
  metadatos_csv_path: /data/MetadatosCM.csv
```

**Modo manifest (145, recomendado).** `MapeoRVI_CM.csv` sólo aporta lo que Content Manager NO sabe — qué código RVI (por sistema; `IDSistema` es opcional, vacío = comodín) va a qué clase (`IDCM`). Todo lo demás — tipo CMIS, carpeta, qué propiedades existen y cuáles son obligatorias — sale de un manifest JSON que bajás del servidor con `cmcourier types discover` y mantenés al día con `types diff` / `types update`. Nadie copia a mano lo que `getTypeDefinition` ya publica. Guía completa: [`../how-to/cm-type-manifest.md`](../how-to/cm-type-manifest.md).

**Modo split (035, deprecado).** Separa el mapeo (`MapeoRVI_CM.csv`: código RVI → CMIS folder + type) de un catálogo de metadatos por clase, `MetadatosCM.csv` — **no** de "tipos": esa tabla lista qué propiedades existen para cada `IDCorto`, cuáles son obligatorias (`Requerido`) y su `CMISPropertyId` a nivel wire; no declara el tipo CMIS del objeto. El catálogo se mantiene a mano y, en instalaciones reales, se desincroniza del servidor con el tiempo — la razón de ser del modo manifest. Los samples viven en [`reference-data/csv/`](../../reference-data/csv/).

---

## `metadata` — la cadena de fallback

S3 resuelve el valor de cada propiedad CMIS recorriendo las fuentes en orden hasta que alguna devuelva algo. Esta es la sección con más juego.

```yaml
metadata:
  # alias lógico → nombre de la propiedad CMIS destino
  field_aliases:
    CIF: BAC_CIF
    Nombre_Cliente: BAC_Nombre_Cliente

  # para cada propiedad CMIS, dónde buscar el valor (en orden)
  field_sources:
    BAC_CIF:
      sources:
        - source_type: trigger              # primero, el propio trigger
          lookup_value_column: cif
        - source_type: rvabrep              # después, la fila RVABREP
          lookup_value_column: index2
      default_value: "000000"               # si nada resolvió
    BAC_Nombre_Cliente:
      sources:
        - source_type: "csv:clientes"       # la fuente nombrada "clientes"
          lookup_value_column: Nombre_Cliente   # qué columna devolver
          lookup_key_column: CIF                # por qué columna buscar
          validation:
            allowed_pattern: "^[A-Za-z0-9 ]+$"  # opcional
      default_value: "UNKNOWN_CLIENT"

  # las fuentes nombradas, referenciadas por "<kind>:<alias>" arriba
  sources:
    - kind: csv
      alias: clientes                       # ← alias, no "name"
      csv_path: /data/clientes.csv          # ← csv_path, no "path"
    - kind: as400
      alias: cuentas
      as400_connection: rvi                 # alias del registro (u objeto inline)
      query: "SELECT * FROM RVILIB.CUENTAS"   # exactamente uno de query / table
    - kind: mssql                           # 130
      alias: clientes_sql_src
      connection: clientes_sql              # SIEMPRE alias — no hay forma inline
      table: dbo.clientes                   # exactamente uno de table / query

  prefetch_enabled: true                    # default true
  cache:
    enabled: false                          # default false
    ttl_minutes: 60                         # default 60, rango 1..43200
```

Tres cosas que se confunden seguido, y que el schema te va a rebotar al cargar porque todos los modelos son `extra="forbid"`:

1. **Los items de `sources` dentro de `field_sources` no llevan `field` ni `alias`.** Llevan `source_type` + `lookup_value_column` (la columna cuyo valor se devuelve) y, para lookups, `lookup_key_column` (la columna por la que se busca).
2. **Las fuentes de `metadata.sources` se identifican con `alias`, no con `name`,** y el CSV va en `csv_path`, no en `path`. No existe `key_columns`.
3. **El prefijo tiene que coincidir con el `kind` de la fuente.** Si declarás `clientes` con `kind: csv`, `source_type: "mssql:clientes"` es error de carga — el schema lo valida (130), porque la resolución es por alias y un prefijo mentiroso no avisaría nunca en runtime.

`source_type` puede ser `"trigger"`, `"rvabrep"`, `"csv:<alias>"`, `"as400:<alias>"` o `"mssql:<alias>"`. Si la cadena entera no resuelve y hay `default_value`, se usa ese —se le aplica el `format` de campo (146) y **no se lo valida contra nada** (149)—; si no hay default, S3 falla con `SourceFailedError`. Si tu default es a propósito distinto de los datos reales (un marcador tipo `000000`), `cmcourier types check` te lo informa con un INFO, no te lo bloquea.

### `lookup_value_source` — de dónde sale la clave de búsqueda (084)

Para las fuentes con prefijo (`csv:` / `as400:` / `mssql:`), por default el valor con el que se busca es el **CIF del trigger**. Cuando necesitás otra cosa, `lookup_value_source` lo dice explícito:

```yaml
    BAC_Numero_Cuenta:
      sources:
        - source_type: "csv:clientes"
          lookup_value_column: Numero_Cuenta
          lookup_key_column: TXN
          lookup_value_source: "rvabrep.txn_num"   # default: "trigger.cif"
```

La sintaxis es `"<scope>.<attr>"` con scope `trigger` (`cif`, `shortname`, `system_id`) o `rvabrep` (`txn_num`, `index1`..`index7`, etc.). Para `source_type: trigger` y `source_type: rvabrep` se ignora — esos caminos leen directo, sin lookup.

### Campos que vale la pena entender

| Campo | Qué hace |
|-------|----------|
| `prefetch_enabled: true` | Pre-carga las fuentes en memoria al inicio del chunk — más rápido si vas a iterar miles de docs |
| `cache.enabled: true` | Habilita el `document_cache` cross-batch (037) — guarda `(txn_num, fields_hash) → properties_json` para saltar S3 en docs que ya viste |
| `cache.ttl_minutes` | Cuánto vive una entrada del cache antes de re-resolverla |

Si movés `prefetch_enabled` a `false`, cada lookup golpea la fuente — útil cuando la fuente es enorme y la mayoría de los docs no la van a tocar.

> Para probar la variante SQL Server en local (Docker + `msodbcsql18` + tabla sembrada) está la sección 5 de [`how-to/probar-la-consola.md`](../how-to/probar-la-consola.md).

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

Hay dos campos más, ambos default-off:

```yaml
assembly:
  keep_staged_files: false                  # default false (085)
  synthetic_content:                        # default off (102) — SOLO stress
    enabled: false
    seed: 0
    size_mix: []                            # vacío = 60/30/10 built-in
```

`keep_staged_files: true` preserva el PDF ensamblado bajo `temp_dir` después de un `S5_DONE` exitoso — útil para inspeccionar qué se subió realmente.

`synthetic_content.enabled: true` (102) hace que S4 **ignore los archivos reales** de `source_root` y arme un PDF de una página en memoria, con el tamaño sorteado de una distribución ponderada. Existe para correr pruebas de stress de varios TB sin materializar varios TB en disco; el mismo `txn_num` produce siempre el mismo PDF. **Nunca lo prendas contra una migración real**: los bytes que llegan a Content Manager son sintéticos, no los documentos del banco.

```yaml
    size_mix:                               # sufijos binarios: b / kb / mb / gb
      - { name: small,  weight: 60.0, min: 50kb, max: 1mb }
      - { name: medium, weight: 30.0, min: 1mb,  max: 10mb }
      - { name: large,  weight: 10.0, min: 10mb, max: 40mb }
```

Los pesos son **relativos**, no porcentajes: sólo tienen que ser ≥ 0 y no todos cero.

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
    # connection: rvi                       # alias del registro (129), u objeto inline;
    #                                       #   required si enabled
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
  s4_smart_routing: true                    # default true (114)
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
| `s4_smart_routing: true` | (094) Con el process pool activo, rutea los PDF nativos inline (su trabajo es `shutil.copy2`, I/O-bound, libera el GIL) y manda solo los paginados TIFF/JPEG al pool. Evita el overhead de pickle/IPC/spawn — clave en Windows. Default `true` desde 114 (opt-out con `false`). |
| `batches_in_flight: 2` | Solo en batched. N=2 = overlap (mientras chunk K sube, K+1 prepara). Bajarlo a 1 desactiva el overlap pero es más predecible. |
| `heavy_light_lanes.enabled: true` | Activa lanes adaptativos en S5 — separa docs ≥ 10 MB del resto. Para que valga la pena necesitás `heavy_lane_min_batch` (default 50) docs por chunk. |

> Para el detalle de modos, leé el [tutorial 03](03-execution-modes-batched-vs-streaming.md).

---

## `environment` (top-level, 123)

```yaml
environment: staging                        # default "staging"; el otro valor es "prd"
```

Una etiqueta, no un comportamiento: los comandos headless la **ignoran por completo**. Existe para la consola de operación (`cmcourier console`). Con `prd`, la consola pinta un badge rojo permanente y el launcher exige **tipear `PRD`** antes de lanzar; `sync recover --apply` desde la pestaña `[8]` pide la misma confirmación.

Es barato y vale la pena: el YAML de producción declara que es de producción, y el operador no puede confundir dos terminales abiertas.

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
environment: prd

connections:
  rvi:
    kind: as400
    host: as400.banco.example
    database: RVILIB
  clientes_sql:
    kind: mssql
    host: sql.banco.example
    database: maestros
    trust_server_certificate: true

trigger:
  kind: csv
  csv_path: /data/lote-marzo.csv

indexing:
  source:
    kind: as400
    connection: rvi
    query: "SELECT * FROM RVILIB.RVABREP"

mapping:
  rvi_cm_csv_path: /data/MapeoRVI_CM.csv
  metadatos_csv_path: /data/MetadatosCM.csv

metadata:
  field_aliases:
    Nombre_Cliente: BAC_Nombre_Cliente
  field_sources:
    BAC_Nombre_Cliente:
      sources:
        - { source_type: "mssql:clientes", lookup_value_column: Nombre_Cliente, lookup_key_column: CIF }
      default_value: "(sin nombre)"
  sources:
    - { kind: mssql, alias: clientes, connection: clientes_sql, table: dbo.clientes }
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
    connection: rvi

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

Streaming + lanes + AIMD + cache + registro de conexiones + AS400 sync. Esto es lo que un dry-run productivo termina pareciendo. Las credenciales que necesita: `CMIS_USERNAME`/`CMIS_PASSWORD`, `RVI_USERNAME`/`RVI_PASSWORD` y `CLIENTES_SQL_USERNAME`/`CLIENTES_SQL_PASSWORD`.

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
