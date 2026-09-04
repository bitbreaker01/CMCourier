# 113 — Pre-flight de sync AS400: lecturas batcheadas

## Por qué

`IdempotencyCoordinator.preflight_sync` (`idempotency.py:296-313`) hace
**un round-trip ODBC por txn del batch**, en un loop serial, antes de
que arranque el pipeline:

```python
for txn in sorted(batch_scope):
    row = self._safe_read(txn)   # SELECT ... WHERE TRNNUM = ? FETCH FIRST 1 ROWS ONLY
```

Con `batch_size` 1000 son **1000 queries secuenciales al iSeries** de
puro arranque — a ~20-50 ms de RTT corporativo, entre 20 y 50 segundos
de pre-flight por chunk que podrían ser una sola query.

Bug menor adyacente: `sqlite_done` se computa dos veces (`:300-304` y
`:308`) — la primera lectura (`is_stage_done` con `batch_id=""`) se
descarta incondicionalmente. Trabajo muerto por doc del scope.

## Qué

**REQ-001 — `read_states_by_txns` en el store.** Nuevo método de
`As400NiarvilogStore`: recibe una lista de txns y devuelve
`dict[trnnum → NiarvilogRow]` usando `IN` chunkeado (1000 por query,
paridad con `As400DataSource._IN_CHUNK_SIZE`). Semántica por txn
idéntica a `read_state_by_txn`: primera fila que matchea (convención
del banco: máx. una fila por txn); txns sin fila no aparecen en el
dict.

**REQ-002 — `preflight_sync` usa la lectura batcheada.** El loop pasa a
iterar sobre el dict: ceil(N/1000) round-trips en lugar de N. La
clasificación (imported / conflicts / consistente) no cambia.

**REQ-003 — Se elimina la computación muerta de `sqlite_done`.** Queda
solo `is_uploaded(txn)` (el estado terminal cross-batch, que es lo que
el pre-flight v1 realmente usa).

## Escenarios

**E1 — 2500 txns → 3 queries.** El store emite ceil(2500/1000) = 3
sentencias `IN`, no 2500.

**E2 — Clasificación intacta.** AS400 `'O'` + SQLite sin registro →
imported; AS400 `'N'` + SQLite done → conflict; txn ausente en AS400 →
consistente (sin acción).

**E3 — `raise_on_conflict` intacto.** Con conflictos y el flag, levanta
`IdempotencyConflictError` con la lista.
