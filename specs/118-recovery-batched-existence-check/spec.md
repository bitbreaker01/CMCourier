# 118 — `sync recover`: chequeo de existencia batcheado

## Por qué

`As400Recovery.recover` (`recovery.py:78-110`) hace, por CADA doc
`S5_DONE` de SQLite, un `read_state_by_txn` — **un SELECT AS400 por
registro**. El comando existe para remediar los ~3445 registros
perdidos del bug 096, pero recorre TODO el tracking (o el batch): con
100k docs subidos son 100k round-trips secuenciales, y en el caso común
(casi todos ya presentes en NIARVILOG) ese chequeo de existencia es el
ÚNICO trabajo por doc.

La re-derivación RVABREP + mapping y el INSERT solo corren para los
txns FALTANTES — un conjunto chico por naturaleza. El N+1 que duele es
el chequeo de existencia.

## Qué

**REQ-001 — Existencia batcheada.** `recover` lee la presencia en
NIARVILOG de todos los txns de una vez vía `read_states_by_txns` (113 —
`IN` chunkeado de a 1000). ceil(N/1000) queries en lugar de N.

**REQ-002 — Clasificación intacta.** `already_present` /
`recovered` / `unrecoverable` (fila RVABREP ausente, ID RVI sin mapear,
error por-txn aislado) no cambian de semántica. El dry-run
(`apply=False`) sigue sin escribir.

**REQ-003 — AS400 caído falla rápido.** Si la lectura batcheada
levanta, el comando falla de entrada con el error real — mejor señal
para el operador que 100k errores por-txn idénticos (pre-118 cada txn
habría caído en `unrecoverable` con el mismo "error: unreachable").

### Fuera de alcance

* Batchear `find_document_by_txn` (la consulta RVABREP por txn
  faltante) — solo corre para el conjunto chico de faltantes, y con
  fuente CSV ya es O(1) post-112.

## Escenarios

**E1 — 2500 docs, todos presentes → 3 SELECTs** (pre-118: 2500).
**E2 — El faltante se recupera igual** con los campos re-derivados.
**E3 — Dry-run intacto.**
