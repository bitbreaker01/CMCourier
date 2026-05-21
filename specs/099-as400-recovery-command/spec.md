# 099 — `cmcourier sync recover`: recuperar filas faltantes en NIARVILOG

## Por qué

El bug del cambio 096 (arreglado en 098) dejó ~3445 documentos
subidos a CMIS y marcados `S5_DONE` en SQLite, pero **sin su fila en
AS400 NIARVILOG**. El 098 frena la pérdida futura; no repara lo ya
perdido. Hace falta una herramienta de remediación.

No alcanza con re-correr el pipeline: en una re-corrida, S5 ve los
docs como `S5_DONE` (`is_stage_done`) y los saltea — nunca se
re-encolan, NIARVILOG sigue sin ellos.

**El blocker conocido**: el INSERT a NIARVILOG necesita `DOCFRM`,
`IMGTIP`, `IDNBAC` y `TIPIDN`, que `migration_log` de SQLite no
almacena. Pero NO se perdieron — se pueden **re-derivar**:

* `DOCFRM` (= `document.index7`) y `IMGTIP` (= `document.image_type`)
  son columnas de la propia tabla RVABREP — se obtienen consultando
  la fila RVABREP por txn.
* `IDNBAC` (= `mapping.id_corto`) y `TIPIDN` (= `mapping.cmis_type`)
  salen del mapping `MapeoRVI_CM`, indexado por el id RVI (= `DOCFRM`).

> Aclaración de claves: el `txn` no es la PK literal de ninguna tabla
> — SQLite tiene PK `id` + único `(txn, batch_id)`; NIARVILOG tiene PK
> compuesta `(SISCOD,TRNNUM,DOCFRM,IMGARC)`. Pero por convención del
> banco hay **una fila por TRNNUM** en NIARVILOG, así que la
> recuperación keyea por txn — igual que el helper ya existente
> `read_state_by_txn`.

## Qué

Un subcomando nuevo: **`cmcourier sync recover --config X [--apply]`**.

Reconcilia SQLite → AS400 por txn: encuentra los docs `S5_DONE` que
NIARVILOG no tiene e inserta las filas faltantes (`STSCOD='O'`).

### Seguridad: dry-run por defecto

Es una herramienta que escribe en la tabla centralizada del banco. Por
eso:

* **sin `--apply`** → *dry-run*: reporta cuántas filas insertaría y
  cuáles, **sin tocar AS400**.
* **con `--apply`** → ejecuta los INSERT.

### Flujo

1. Arma desde el config (vía wiring): SQLite store, AS400 store,
   source RVABREP, `IndexingService`, `MappingService`.
2. **Enumera candidatos** — `SQLiteTrackingStore.iter_uploaded_records()`
   (nuevo): cada doc `S5_DONE` con sus campos
   (`txn`, `cm_object_id`, `shortname`, `cif`, `system_id`,
   `file_name`, `retry_count`). Opcional `--batch-id` para acotar.
3. Para cada candidato: `as400.read_state_by_txn(txn)`.
   * Fila presente → **skip** (`already_present`).
   * Fila ausente → recuperar.
4. **Re-derivar los 4 campos faltantes**: consultar la fila RVABREP
   por `txn`, convertirla con `IndexingService` a un `RVABREPDocument`
   (→ `DOCFRM`, `IMGTIP`), y `MappingService.get_mapping(DOCFRM)`
   (→ `IDNBAC`, `TIPIDN`). Si la fila RVABREP no existe, o el id RVI
   no está mapeado → **skip + reporte** (`unrecoverable`), nunca a
   ciegas.
5. Con `--apply`: `As400NiarvilogStore.insert_recovered_row(...)` —
   un INSERT directo con `STSCOD='O'` y los 11 campos explícitos.
6. **Reporte final**: `recovered`, `already_present`, `unrecoverable`
   (con el motivo por txn) — a stdout y al log.

### Cambios

1. **`SQLiteTrackingStore.iter_uploaded_records(batch_id=None)`**
   (nuevo) — itera los registros `S5_DONE` con los campos que la
   recuperación necesita; `batch_id` opcional para acotar.

2. **`As400NiarvilogStore.insert_recovered_row(*, siscod, trnnum,
   docfrm, imgarc, imgtip, ctecif, ctenum, idnbac, tipidn, objidn,
   numrei)`** (nuevo) — INSERT directo de una fila terminal
   `STSCOD='O'`. Usa el mismo `_with_retry` que el resto del store.

3. **`services/recovery.py`** (nuevo) — `As400Recovery` orquesta el
   flujo; `RecoveryResult` (recovered / already_present /
   unrecoverable) + `RecoveryItem` por txn no recuperable con motivo.

4. **`cli/commands/sync.py`** — subcomando `recover` con `--config`,
   `--apply`, `--batch-id`.

5. **`config/wiring.py`** — un helper que arma el bundle que
   `As400Recovery` necesita (reusa los builders existentes de source
   RVABREP / indexing / mapping).

### Tests

* `test_recovery.py`: txn ausente en AS400 → se recupera (campos
  re-derivados correctos); txn ya presente → skip; fila RVABREP
  inexistente → `unrecoverable`; id RVI sin mapping → `unrecoverable`.
* Dry-run: con `apply=False` no se llama `insert_recovered_row`.
* `test_sqlite_tracking_store.py`: `iter_uploaded_records` devuelve
  solo `S5_DONE`, respeta `batch_id`.
* `test_as400_niarvilog.py`: `insert_recovered_row` emite el INSERT
  correcto.
* `test_sync.py`: `sync recover` dry-run vs `--apply`; exit codes.

## Criterios de aceptación

1. `sync recover` sin `--apply` no escribe nada en AS400 y reporta el
   plan.
2. `sync recover --apply` inserta las filas faltantes con los 4
   campos re-derivados correctos y `STSCOD='O'`.
3. Un txn ya presente en NIARVILOG nunca se pisa.
4. Un txn que no se puede re-derivar (sin fila RVABREP / sin mapping)
   se reporta como `unrecoverable` con motivo — nunca se inserta a
   ciegas ni se cuenta como recuperado.
5. `pytest -m unit` y `-m integration` pasan.

## Riesgos

* **El INSERT necesita que la fila RVABREP siga existiendo.** Si el
  banco purgó RVABREP, esos txn quedan `unrecoverable` — el operador
  los ve listados y decide. Es correcto: mejor reportar que inventar.
* **`FINREI`/`PMRREI`**: la recuperación inserta el estado terminal;
  los timestamps quedan con el valor de INSERT (no el momento real
  del upload). Es tracking de remediación, aceptable — se documenta.
* **Idempotencia**: correr `recover --apply` dos veces es seguro — la
  segunda corrida ve las filas ya presentes y las saltea.

## Notas

- Solo cubre la dirección SQLite → AS400 para docs `S5_DONE`. Los
  `S5_FAILED` no se "recuperan" — no se subieron.
- Reusa `IndexingService` (fila RVABREP → `RVABREPDocument`) y
  `MappingService` para que los campos re-derivados sean idénticos a
  los que la corrida original habría escrito.
- Relacionado: 096 (introdujo el reconciliador), 098 (frenó la
  pérdida), 034 (`read_state_by_txn`, sync original).
