# 117 — Reconciler periódico: lectura batcheada + propagación en un write

## Por qué

En modo `periodic`, el reconciliador drena 5 minutos de uploads (a
10 docs/s son ~3000 items) y por CADA item hace, secuencial y en un
solo thread (`reconciler.py:188-254`):

1. `read_state(pk)` — 1 SELECT.
2. `try_claim` — 1 UPDATE (+1 INSERT si la fila no existe).
3. `mark_uploaded` / `mark_failed` — 1 UPDATE.

= **~3 round-trips ODBC por item, ~9000 sentencias por pasada**. A
20-50 ms de RTT corporativo, una pasada puede tardar más que el propio
intervalo. Y la pasada final de `stop()` (sin `stop_event`) bloquea el
cierre del proceso todo ese tiempo.

El paso intermedio por `'I'` (claim) es herencia del modo `claim` del
hot path: en el reconciler no aporta nada — el doc YA está subido (o
fallado); el estado 'I' vive microsegundos entre dos UPDATEs nuestros.

## Qué

### Requisitos

**REQ-001 — Lectura batcheada.** `run_pass` lee el estado de TODOS los
items de una vez vía `read_states_by_txns` (113 — `IN` chunkeado de a
1000). Si la lectura batcheada falla (AS400 caído), todos los items van
a `requeued` (semántica 098: nunca se pierde un item).

**REQ-002 — Propagación en un write directo al estado terminal.** Para
un item que necesita sync (fila ausente o en `'N'`):

* Fila en `'N'` → `update_terminal_if_new`: un solo
  `UPDATE ... SET STSCOD=?, OBJIDN=?, ... WHERE pk AND STSCOD='N'`.
  `rowcount == 0` ⇒ otro proceso tocó la fila entre el read y el write
  → conflicto (paridad con la race del claim).
* Fila ausente → `insert_terminal`: un INSERT con el estado final.
  `IntegrityError` ⇒ race → conflicto.

= 1 write por item sincronizado (+1/1000 de lectura amortizada), contra
3 round-trips pre-117.

**REQ-003 — Clasificación intacta.** `consistent` (AS400 'O' con
nuestro objectId), `conflict` (estado terminal/in-progress divergente)
y la resiliencia 098 (falla per-item aislada → `requeued`; `stop_event`
corta entre items) no cambian de semántica.

**REQ-004 — Import de subidas ajenas batcheado.** `_import_foreign_uploads`
usa la misma lectura batcheada para el scope (hoy: 1 SELECT por txn).

### Nota sobre el lookup por txn

`read_state(pk compuesta)` pasa a `read_states_by_txns` (solo TRNNUM).
Es la convención operativa documentada del banco — máx. una fila por
txn (`read_state_by_txn`, 034 fase 4) — y la misma que ya usa el
pre-flight (113).

## Escenarios

**E1 — 3 items sanos = 1 lectura + 3 writes** (pre-117: 9+ sentencias).
**E2 — Race en UPDATE** (`rowcount 0`) → conflicto, no se pisa.
**E3 — Race en INSERT** (`IntegrityError`) → conflicto.
**E4 — AS400 caído en la lectura batcheada** → todos los items a
`requeued`, `failed == len(items)`, sin excepción.
**E5 — Resiliencia per-item intacta** — un write que explota aísla su
item; el resto de la pasada sigue.
