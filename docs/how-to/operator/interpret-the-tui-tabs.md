# Interpretar las tabs del TUI

> [← Volver al índice](../../INDEX.md) · [How-to](../README.md) · [Operador](README.md)

Atlas visual de las 5 tabs del TUI live. Cada tab tiene un propósito y un par de números clave — esta receta te dice qué mirar primero y qué significa cada cosa.

## Cuándo usarlo

- Estás corriendo la pipeline con `--tui` (default) y querés decodificar lo que ves.
- Necesitás saber qué tab abrir para diagnosticar un síntoma.

## Pre-requisitos

- Una corrida activa: `cmcourier ... pipeline run --tui ...`.
- Refresh rate: 250 ms (no toques nada, vas a ver actualización fluida).
- TUI corre en main thread, orchestrator en worker thread (`cli/_tui_runner.py`) — colgarse el TUI no cuelga la migración.

## Keybindings globales

| Tecla | Acción |
|-------|--------|
| `P` | Tab PREP |
| `U` | Tab UPLOAD |
| `C` | Tab CHUNKS |
| `B` | Tab BUCKET (solo modo streaming) |
| `D` | Tab DETAIL |
| `[` | Chunk previo (cursor en DETAIL) |
| `]` | Chunk siguiente |
| `Q` | Quit |

## PREP — tab `P`

S0–S4: triggers adquiridos, indexados, mapeados, metadata resuelta, PDFs ensamblados.

### Layout

```
  S0 TRIGGER   ████████████████████████████    1000   p50    12.4 ms  p95    45.2 ms
  S1 INDEXING  ████████████████████████░░░░     947   p50    88.1 ms  p95   312.0 ms
  S2 MAPPING   ███████████████████████░░░░░     923   p50     1.2 ms  p95     4.8 ms
  S3 METADATA  ██████████████████████░░░░░░     901   p50    24.6 ms  p95   102.3 ms
  S4 ASSEMBLY  █████████████████████░░░░░░░     880   p50   312.7 ms  p95  1822.1 ms

  EXCLUIDOS (S1 · la razón, en `batch show`)      37

 SLOW OPS (PREP, top 5)
  1  S4           20251015-001    1822 ms
  2  S3           20251015-007    1102 ms
  ...
```

### Qué significa cada número

- **Barra** — relativa al `count` máximo entre S0..S5 (no es % del total absoluto). Sirve para ver progresión relativa entre stages.
- **count** — docs procesados por ese stage.
- **p50 / p95** — latencias por op. p95 alto en S4 = PDFs grandes o disk slow. p95 alto en S1 = RVABREP o AS400 lento.
- **EXCLUIDOS (S1)** — documentos que S1 dejó afuera. **No son fallas**, y el contador NO dice por qué: hasta 148 este balde tenía una sola causa (`DELETED_AT_SOURCE`) y la etiqueta la nombraba; hoy junta también `EXCLUDED_BY_FILTER`, `SOURCE_ROW_INCOMPLETE` y `OUT_OF_SCOPE_RESUME`. El desglose por razón está en `cmcourier batch show`.
- **SLOW OPS** — top 5 ops que pasaron `slow_op_threshold_ms` (default 5000 ms). Solo S1–S4.

### Qué mirar primero

- Si S0 está muy por delante de S1 → la adquisición de triggers es OK pero indexing va lento (RVABREP o AS400 bottleneck).
- Si S4 atrás de S3 con p95 alto → PDF assembly es CPU-bound. Subí `prep_workers` o activá `s4_use_processes`.
- Mucho EXCLUIDOS → corré `batch show` y mirá la razón. Si dice `EXCLUDED_BY_FILTER`, es tu propia `triggers.filters.document_types` dejándolos afuera, no el origen dándolos de baja.

## UPLOAD — tab `U`

S5: throughput, latencia, AIMD, ancho de banda.

### Layout (sin lanes)

```
  S5 UPLOAD     ███████████████░░░░░░░░░░░░░   624 / 1000 docs   142.3 MB / 234.8 MB
                chunk elapsed 00:04:12   avg 8.42 MB/s   est remaining 00:01:48
                p50   312.4 ms  p95   985.2 ms  p99  2103.1 ms

 WORKERS
  Pool capacity:   8   in-use 6   idle 2
  Queue depth:     142 pending

  Auto-tune:       ON
    target p95:    5,000 ms   observed p95: 985.2 ms
    adjust:        every 30s   next: in 12s
    timeout:       60.0s active   (range 30–600s)
    last move:     grow → workers=8  (18s ago)

 NETWORK (CMIS)
  Endpoint:      https://cm.example.com/cmis/...
  Bandwidth:     8.42 MB/s   peak 12.31 MB/s  ceiling 25.0 MB/s (config)

 UPLOAD SPEED (60s · MB/s · y: 0 → 25.0)
  ▁▂▃▅▆▇█▇▆▅▆▇█▇▆▅▆▇█▇▆▅▆▇█▇▆▅▆▇█▇▆▅▆▇█▇▆▅▆▇█▇▆▅▆▇█▇▆▅▆▇█▇▆▅▆▇█
  └──────────────────────────────────────────────────────────┘  -60s ............. now

 SLOW OPS (UPLOAD, top 5)
  1  20251015-042    worker-5             7204 ms
  ...

 ERRORS BY TYPE (UPLOAD) — 12 total
  http_5xx        10
  timeout          2
    HTTP status:  500×2  503×8
```

### Layout (con heavy/light lanes, 036)

```
 WORKERS (heavy/light · total budget 12)
  HEAVY  capacity   4   in-use   3   idle   1   queue   18
         done    47   failed    0
  LIGHT  capacity   8   in-use   6   idle   2   queue    9
         done   312   failed    1
```

### Qué significa cada número

- **`X / Y docs`** — uploaded / (uploaded + queue). El denominador cambia con el `queue_depth`.
- **`X MB / Y MB`** — bytes subidos del chunk actual (con `Y` cuando se conoce el total; cae a solo `X MB` si no).
- **chunk elapsed / avg / est remaining** — solo aparece cuando ya hubo actividad de upload (evita ruido inicial).
- **p50/p95/p99** — latencia de S5. Alimentan al AIMD.
- **Pool capacity / in-use / idle** — single-pool. Cuando lanes están ON, se reemplaza por panel dual HEAVY/LIGHT.
- **Queue depth** — docs preparados esperando upload. Alto sostenido = uploader es el cuello.
- **Auto-tune** — ver [`tune-aimd-for-a-slow-link.md`](tune-aimd-for-a-slow-link.md) para interpretación profunda.
- **Bandwidth current / peak / ceiling** — MB/s actual, máximo histórico, tope configurado (`cmis.max_bandwidth_mbps`, 0 = sin tope).
- **UPLOAD SPEED sparkline** — 60 s ventana, MB/s, escala fija al ceiling si está configurado o al peak observado.
- **ERRORS BY TYPE (UPLOAD)** — 104: desglose de las fallas **de S5 y sólo de S5**, por categoría (`timeout` / `http_4xx` / `http_5xx` / `transport` / `app_error`) y por status HTTP exacto. El total del rótulo es el total de UPLOAD, **no** el de la corrida: un documento que murió en S2 (identidad sin resolver) o en S4 (falta una página) no pasa por CMIS y no puede aparecer acá. Hasta 155 este bloque se llamaba `ERRORS BY TYPE` a secas y su total se usaba además como el `fallidos` de la corrida — por eso el monitor podía decir `0 fallidos` con diez documentos muertos en prep. El total de la corrida está en `fallidos` de la cabecera de `[6]` y en `FALLIDOS` del bloque OUTCOMES.

### Qué mirar primero

- Si Queue depth está clavado en su máximo → S5 no da abasto (subí workers o activá lanes).
- Si Bandwidth current << ceiling → AIMD no está escalando (revisá `growth_factor`).
- Si Slow Ops están dominados por un único worker → ese worker tiene un problema (conexión fría, slot pegado).

## CHUNKS — tab `C`

Vista multi-batch. Una fila por chunk (relevante con `batches_in_flight=2`).

### Layout

```
CHUNKS — pipeline rvabrep-trigger
──────────────────────────────────────────────────────────────
  total 2   done 1   prep 0   upload 1   queued 0   failed 0

    idx  batch_id        docs       MB  PREP d/s/f/x (elap)     UPLOAD d/s/f (elap)     RATE MB/s·d/s     state
    ───────────────────────────────────────────────────────────────────────────────────────────────────────────
      0  bk-001-c0       1000    234.8  1000/0/0/0   (12.4s)    1000/0/0    (28.1s)    8.4 · 35.6        ✓ DONE
      1  bk-001-c1       1000    241.2  1000/0/0/0   (11.8s)     624/0/0    (16.7s)   14.4 · 37.4        ▲ UPLOAD
    ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    TOTAL (2 chunks)     2000    476.0  2000/0/0/0   (24.2s)    1624/0/0    (44.8s)    10.6 · 36.3
```

### Glyphs de estado

| Glyph | Estado |
|-------|--------|
| `·` | QUEUED |
| `▶` | PREP |
| `▲` | UPLOAD |
| `✓` | DONE |
| `✗` | FAILED |

### Qué significa cada columna

- **PREP d/s/f/x** — done / skipped / failed / **filtered** (los 4 outcomes de S1–S4; `filtered` son los códigos de baja).
- **UPLOAD d/s/f** — done / skipped / failed (sin filtered en S5).
- **RATE MB/s · d/s** — throughput del chunk: MB/s y docs/s sobre el `upload_elapsed_s`. Guion (`—`) cuando elapsed = 0.
- **TOTAL** — agregado de todos los chunks. Las filas QUEUED contribuyen su plan (docs/bytes) sin sus resultados, alineado con el preview de lo que va a venir.

### Qué mirar primero

- Si un chunk va en UPLOAD mientras el siguiente está PREP → el overlap N=2 está funcionando.
- Si todos los chunks quedan en QUEUED y solo uno avanza → revisá `processing.batches_in_flight` (debería ser 2).
- En streaming, los chunks colapsan a uno solo (single batch_id) — usá BUCKET en su lugar.

## BUCKET — tab `B` (solo streaming)

Producer/consumer view: PREP empuja, S5 drena, hay un bucket acotado en el medio.

### Layout

```
BUCKET
──────
  level   142 / 200   [█████████████████████░░░░░░░░░]
  peak    187 / 200

THROUGHPUT (5s window)
──────────────────────
  PREP     45.20 docs/s
  S5       38.12 docs/s

WORKERS
───────
  PREP     4 in-flight / 4 configured
  S5       up to 12 consumer threads

LANES (heavy/light, 065)
────────────────────────
  heavy  budget 4    busy 3    queue 18
  light  budget 8    busy 6    queue 9
  total budget 12

OUTCOMES (cumulative)
─────────────────────
  S5_DONE        624
  FALLIDOS         3
    S5_FAILED      3
  BLOQUEADOS      10
  EXCLUIDOS       37
  S1_FILTERED     37
  S1_SKIPPED       0
```

### Qué significa cada número

- **level / cap** — docs actualmente en el bucket / capacidad (`processing.streaming.bucket_size`).
- **peak** — máximo histórico desde el inicio.
- **PREP docs/s** vs **S5 docs/s** — ventana de 5 s. Si PREP > S5 sostenido, el bucket se llena (back-pressure correcta, PREP queda bloqueado).
- **PREP in-flight / configured** — workers PREP ocupados sobre los configurados (`processing.prep_workers`).
- **S5 up to N consumer threads** — techo (`cmis.workers` o el budget del AIMD).
- **LANES** — bloque por-lane solo si `heavy_light_lanes.enabled: true`.
- **OUTCOMES (cumulative)** — contadores acumulativos. `S1_SKIPPED` ≠ 0 indica idempotency cross-batch.
- **FALLIDOS / BLOQUEADOS / EXCLUIDOS** (155 + 157) — desde 157 el bloque
  separa las tres categorías de no-subidos, cada una por su balde:
  - **FALLIDOS** — fallas terminales de ejecución (`*_FAILED`, balde FALLO),
    S1 a S5. Debajo, indentada, va una línea por etapa que tenga algo:
    `S5_FAILED 3`. Estas SÍ se reintentan con `R`.
  - **BLOQUEADOS** (157) — `*_BLOCKED`: falta config (código sin mapear, tipo
    fuera del manifest, identidad o metadata sin resolver, fila de origen
    incompleta). No son fallas de ejecución: se arreglan editando la config y
    re-corriendo, **no** con `R`.
  - **EXCLUIDOS** (157) — `*_EXCLUDED` + `S1_FILTERED` + `S1_SKIPPED`: decisión
    de negocio/origen (cliente inactivo, código de baja, ya subido). No hay
    nada que arreglar.
  - `S5_DONE`, `FALLIDOS`, `BLOQUEADOS` y `EXCLUIDOS` se muestran siempre,
    aunque valgan cero; las etapas de FALLIDOS aparecen sólo cuando fallaron.
  - **Por qué importa**: hasta 156 un cliente inactivo (`CLIENT_NOT_ACTIVE`)
    vivía en una fila `S2_FAILED` y se contaba como falla — el operador veía
    `fallidos 38527` sobre documentos que nadie falló. 157 le dio a cada balde
    su propio sufijo de status, así que el monitor cuenta cada uno donde
    corresponde y una corrida que sólo excluyó/bloqueó ya no se anuncia en rojo.
  - **La invariante (155, extendida por 157)**: lo que este bloque cuenta al
    terminar —en las CUATRO categorías— tiene que coincidir con lo que
    `cmcourier batch show <batch_id>` cuenta después. Si difieren, la vista en
    vivo está mintiendo — la base es la verdad.
  - **El POR QUÉ por documento no está acá.** El contador vive en memoria y no
    lleva `reason_code`; el censo por razón está en
    [`read-the-batch-census.md`](read-the-batch-census.md).

### Qué mirar primero

- `FALLIDOS` ≠ 0 con `S5_DONE` en cero → nada está subiendo; mirá en qué etapa está el número y andá a `batch show` por la razón.
- `level` clavado al cap → S5 es el cuello, subí workers o activá AIMD.
- `level` clavado a 0 → PREP no llega, subí `prep_workers` o revisá S4.
- PREP > S5 con bucket llenándose → comportamiento esperado (back-pressure).
- Modo batched abriendo BUCKET → vas a ver un stub: "active in streaming mode only — see CHUNKS".

## DETAIL — tab `D`

Drill-down por chunk. Mueve el cursor con `[` y `]`, después abrí con `D`.

### Layout

```
DETAIL — per-chunk drill-down
──────────────────────────────────────────────────────────────
  chunk 1   batch bk-001-c1   state UPLOAD   docs 624

  txn_num           file_name              size       status      reason
  ─────────────────────────────────────────────────────────────────────────
  20251015-001      INVOICE_001.pdf     2.4 MB    S5_DONE       —
  20251015-002      STATEMENT_07.tif    1.1 MB    S5_DONE       —
  20251015-042      INVOICE_042.pdf    45.2 MB    S5_FAILED     CMISServerError: 503
  ...
```

### Qué significa

- **chunk N** — chunk seleccionado por el cursor.
- **docs N** — total de filas en este chunk.
- **status** — uno de `S0_DONE`, `S1_DONE`, `S1_SKIPPED`, `S2_DONE/FAILED`, ..., `S5_DONE/FAILED`.
- **reason** — `error_message` si está, sino `—`.

### Qué mirar primero

- Si los FAILED tienen `error_message` con un patrón (mismo path, mismo tipo de error) → la causa está localizada y un retry les puede dar.
- Si DETAIL se queda vacío → el chunk todavía no llegó a estados terminales (el panel se popla a medida que docs cierran).
- Para `chunk`s muy grandes (>2000 filas), el panel trunca y te apunta a `cmcourier batch show <batch_id>` para la lista completa.

## Si algo sale mal

| Síntoma | Acción |
|---------|--------|
| El TUI no actualiza pero el log sigue creciendo | El TUI se quedó, el worker thread sigue. Quit con `Q` y revisá `logs/` |
| Tab `B` vacía con stub message | Estás en `mode: batched` — usá `C` (CHUNKS) |
| Sparkline UPLOAD plano en cero | No hubo upload todavía o `pipeline_metrics: false` |
| LANES no aparece en UPLOAD/BUCKET | `heavy_light_lanes.enabled: false` o batch debajo de `heavy_lane_min_batch` |

## Ver también

- [`run-a-migration-from-csv.md`](run-a-migration-from-csv.md) — la corrida canónica
- [`tune-aimd-for-a-slow-link.md`](tune-aimd-for-a-slow-link.md) — interpretar el panel auto-tune
- [`configure-heavy-light-lanes.md`](configure-heavy-light-lanes.md) — los paneles LANES
- [`run-a-streaming-load-against-staging.md`](run-a-streaming-load-against-staging.md) — la tab BUCKET en acción
