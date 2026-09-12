# State machine de `migration_log.status`

> [← Volver al índice](../INDEX.md) · [Diagramas](README.md)

Cada fila en `migration_log` (SQLite tracking) tiene un campo `status` que indica en qué stage está ese documento. La state machine es estrictamente sucesiva.

## Estados y transiciones

```mermaid
stateDiagram-v2
    [*] --> S0_PENDING

    S0_PENDING --> S0_DONE: trigger acquired

    S0_DONE --> S1_PENDING: hand off
    S1_PENDING --> S1_DONE: RVABREP resolved
    S1_PENDING --> S1_SKIPPED: is_uploaded(txn_num) == true (062)

    S1_DONE --> S2_PENDING: hand off
    S2_PENDING --> S2_DONE: mapping resolved
    S2_PENDING --> S2_FAILED: IDRViNotMappedError o equivalente

    S2_DONE --> S3_PENDING: hand off
    S3_PENDING --> S3_DONE: metadata resuelta
    S3_PENDING --> S3_FAILED: SourceFailedError

    S3_DONE --> S4_PENDING: hand off
    S4_PENDING --> S4_DONE: PDF ensamblado
    S4_PENDING --> S4_FAILED: SourceFileMissingError / PDFAssemblyFailedError

    S4_DONE --> S5_PENDING: hand off
    S5_PENDING --> S5_DONE: POST a CMIS exitoso, cm_object_id capturado
    S5_PENDING --> S5_FAILED: RetriesExhaustedError o CMISClientError no-retryable

    S5_DONE --> [*]
    S1_SKIPPED --> [*]
    S2_FAILED --> [*]
    S3_FAILED --> [*]
    S4_FAILED --> [*]
    S5_FAILED --> [*]
```

## Reglas

- Transiciones son **estrictamente hacia adelante**. No hay `S3_DONE → S2_PENDING`.
- Solo `Sn_FAILED → Sn_PENDING` se permite vía `cmcourier batch retry-failed --stage SN` (re-corrida explícita).
- `S1_SKIPPED` es **terminal** — el doc ya está en Content Manager, no hay nada que hacer.
- `cm_object_id` se persiste solo en `S5_DONE`. Las otras filas tienen `NULL`.
- `error_message` se persiste en cualquier `*_FAILED`.

## Idempotencia cross-batch

La unicidad está garantizada por `UNIQUE INDEX idx_migration_log_txn_batch (rvabrep_txn_num, batch_id)`. Re-correr el mismo batch_id falla en insert. Re-correr otro batch_id procesa pero el primer chequeo de S1 detecta `is_uploaded(txn_num) == true` vía `INDEX idx_migration_log_uploaded ON (rvabrep_txn_num) WHERE status='S5_DONE'` y marca `S1_SKIPPED`.

## Query típica para diagnóstico

```sql
SELECT status, COUNT(*) AS n
FROM migration_log
WHERE batch_id = 'mi-batch-001'
GROUP BY status
ORDER BY status;
```

## Ver también

- [reference/tracking-db-schema.md](../reference/tracking-db-schema.md)
- [explanation/idempotency-and-retries.md](../explanation/idempotency-and-retries.md)
- [reference/error-codes.md](../reference/error-codes.md)
- [how-to/operator/retry-only-failed-records.md](../how-to/operator/retry-only-failed-records.md)
