# 122 — Papel de lija: micro-hallazgos restantes de la auditoría

## Por qué

Los últimos items accionables del informe, todos chicos, agrupados en
un cambio:

1. **5.13** — `compute_fields_hash` (document_cache.py) recalcula
   `sorted()` + SHA-256 **dos veces por documento** (`try_get` y
   `put`) sobre tuplas que se repiten toda la corrida (una por
   `id_rvi`). Memoizable.
2. **5.16** — `MetadataService` reconstruye `aliases_lower` (dict
   comprehension sobre `field_aliases`) **una vez por documento**
   dentro de `resolve()`; el mapping es inmutable.
3. **5.17** — `staged.py:1055`:
   `set_queue_depth(snapshot().queue_depth - 1)` — dos locks y una
   dataclass frozen por documento para decrementar un entero.
   `WorkerPoolStats` gana `decrement_queue_depth()`.
4. **5.24** — `multi_batch.py:546`: única lectura de `_chunks_state`
   sin `_state_lock` (path de error de prep) mientras el thread de
   upload puede escribirlo.
5. **5.25** — `streaming.py:681`: read-modify-write de `_peak_qsize`
   desde N producers sin sincronizar (subestima el pico del BUCKET
   tab). Se guarda bajo `_prep_in_flight_lock` (ya adyacente).
6. **5.26** — `_prep_loop` de N=2 no chequea cancelación: al cancelar,
   el productor sigue trayendo chunks completos y pagando su
   scaffolding (batch_id, recorder) aunque los per-doc aborten.
7. **P21** — `staged.run` calienta `self._workers` (4) sesiones cuando
   el pool de S5 se dimensiona a `_pool_ceiling()` (50 con AIMD) —
   pasa a `_pool_ceiling()` (paridad con streaming; relevante con
   `http2: false`).
8. **Encapsulación** — `data_provider.py:305` lee
   `uploader._timeout_s` (privado). `CmisUploader` gana la property
   `current_timeout_s`.

### Declinado con motivo

**5.18** (publicaciones por-doc del streaming al TUI): throttlearlas
arriesga estados finales rancios en los tabs por un ahorro de
~microsegundos por doc. No paga el riesgo.

## Escenarios

**E1 —** el hash de fields se computa una vez por tupla única.
**E2 —** `decrement_queue_depth` decrementa con piso en 0.
**E3 —** `current_timeout_s` refleja el valor vivo del AIMD.
**E4 —** el resto: mismas semánticas, carreras cerradas (cubierto por
la suite existente).
