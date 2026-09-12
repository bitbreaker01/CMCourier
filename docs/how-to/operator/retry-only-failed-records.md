# Reintentar solo los records fallados

> [← Volver al índice](../../INDEX.md) · [How-to](../README.md) · [Operador](README.md)

Una corrida termina con exit code `1` y algunos `*_FAILED` en el tracking. No querés re-correr todo el batch — solo querés que la pipeline vuelva a procesar las filas que pincharon.

## Cuándo usarlo

- Exit code `1` después de una corrida.
- `cmcourier batch show` te muestra fallados en algún stage.
- El motivo del fallo fue transitorio (timeout 5xx, race con archivo en S4, AS400 momentáneamente caído).

## Pre-requisitos

- La corrida original terminó (no hay proceso vivo sobre el mismo `batch-id`).
- Conocés el `batch-id` que querés reintentar.
- El motivo del fallo ya está resuelto (backend levantado, archivo presente, etc.).

## Pasos

### 1. Identificá qué falló

```bash
cmcourier batch show mi-batch-001 --config sample/config.yaml
```

Vas a ver una tabla por stage con `DONE / FAILED / PENDING` y abajo una sección `FAILED records` con `TXN_NUM / STAGE / ERROR`.

Si querés un dump completo a CSV para analizar offline:

```bash
cmcourier batch export-report \
    --config sample/config.yaml \
    --batch mi-batch-001 \
    --format csv \
    --output /tmp/mi-batch-001-report.csv
```

O directo al tracking DB para listar solo los FAILED:

```bash
sqlite3 sample/tracking.db <<SQL
SELECT rvabrep_txn_num, status, error_message
  FROM migration_log
 WHERE batch_id='mi-batch-001'
   AND status LIKE '%_FAILED'
 ORDER BY status, rvabrep_txn_num;
SQL
```

### 2. Resetear los FAILED a PENDING

El comando es `batch retry-failed`. Por default resetea **todos** los `*_FAILED` del batch a `*_PENDING`:

> **Salvo el balde `EXCLUIDO` (150).** Una fila cuyo `reason_code` sea una
> exclusión —`CLIENT_NOT_ACTIVE`, por ejemplo, que es `S2_FAILED`— **nunca**
> se reintenta, sin importar su `status`. Reintentar una decisión de negocio
> no la cambia de opinión, y con la lista de clientes activos del banco serían
> cientos de miles de documentos re-procesados para volver a excluirlos igual.
> Por eso el `Reset N` que ves abajo puede ser menor que el total de
> `*_FAILED` que contaste en el paso 1: la diferencia es el balde `EXCLUIDO`,
> y está bien así. Ver
> [`read-the-batch-census.md`](read-the-batch-census.md).

```bash
cmcourier batch retry-failed \
    --config sample/config.yaml \
    --batch mi-batch-001
```

Output esperado:

```
Reset N FAILED rows to PENDING (batch=mi-batch-001, stage=all)
```

Si querés acotar el reset a un stage específico (solo los S5_FAILED, por ejemplo, porque sabés que fue un blip de CMIS):

```bash
cmcourier batch retry-failed \
    --config sample/config.yaml \
    --batch mi-batch-001 \
    --stage S5
```

Stages válidos: `S1`, `S2`, `S3`, `S4`, `S5` (definido en `_STAGES_FOR_RETRY`).

### 3. Re-correr la pipeline con el mismo batch-id

```bash
cmcourier csv-trigger-pipeline run \
    --config sample/config.yaml \
    --batch-id mi-batch-001 \
    --resume
```

`--resume` hace que la pipeline auto-detecte el `from-stage` mínimo necesario mirando el estado del batch. Los records que ya están en `S5_DONE` no se tocan (idempotency vía la UNIQUE en `rvabrep_txn_num`).

Si conocés exactamente desde qué stage querés arrancar, usá `--from-stage N` en vez de `--resume`:

```bash
# Por ejemplo, sabés que solo fallaron uploads — arrancá en S5
cmcourier csv-trigger-pipeline run \
    --config sample/config.yaml \
    --batch-id mi-batch-001 \
    --from-stage 5
```

## Verificación

```bash
# Exit code 0 → todo recuperado
echo $?

# Conteo: cero FAILEDs
sqlite3 sample/tracking.db \
    "SELECT status, COUNT(*) FROM migration_log
       WHERE batch_id='mi-batch-001' GROUP BY status;"
```

Esperás ver solo `S5_DONE` (y eventualmente `S1_SKIPPED` por idempotency cross-batch). Cero `*_FAILED`, cero `*_PENDING`.

Vista resumida:

```bash
cmcourier batch show mi-batch-001 --config sample/config.yaml
```

## Si algo sale mal

| Síntoma | Probable causa | Acción |
|---------|----------------|--------|
| Vuelven a fallar los mismos | Fallo no transitorio | Mirá `error_message` real, no asumas blip. Puede ser `SourceFileMissingError`, `IDRViNotMappedError`, etc. |
| `Reset 0 FAILED rows` | El batch no tiene FAILEDs | Verificá `batch show`, quizás ya alguien hizo el reset |
| Falla en S4 con `SourceFileMissingError` | El archivo no está en `assembly.source_root` | Caso de operaciones: el archivo se perdió. Ningún retry lo arregla — necesitás re-poblar el árbol o saltearlo del batch |
| Falla en S2 con `IDRViNotMappedError` | El mapping no cubre ese `id_rvi` | Agregá la fila al CSV de mapping y reintentá |

## Ver también

- [`run-a-migration-from-csv.md`](run-a-migration-from-csv.md) — la corrida canónica
- [`tune-aimd-for-a-slow-link.md`](tune-aimd-for-a-slow-link.md) — si los fallos S5 son por timeouts crónicos
- [`recover-from-a-corrupted-tracking-db.md`](recover-from-a-corrupted-tracking-db.md) — cuando el problema es la DB, no los records
