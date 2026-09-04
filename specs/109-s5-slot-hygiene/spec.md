# 109 — S5: el trabajo de idempotencia sale del slot del semáforo

## Por qué

En `_upload_one` (`staged.py:1141`), el orden pre-109 era:

1. `acquire()` del semáforo (el slot de concurrencia que el AIMD
   dimensiona) — `staged.py:1170-1174`.
2. **Con el slot ya tomado**: `is_stage_done` (query SQLite),
   `mark_stage_pending`, y `try_claim` — que en modo
   `as400_sync.mode="claim"` son **1-2 round-trips ODBC al AS400** —
   `staged.py:1176-1191`.
3. Recién ahí el upload real a CMIS.

Consecuencias:

* Un slot de upload pasa parte de su vida haciendo trabajo que **no es
  upload**. Con `claim` activo, cada documento retiene el slot durante
  una ida al AS400 antes de tocar la red CMIS — con capacity 4-8 (el
  arranque típico del AIMD), eso es una fracción real del presupuesto
  de concurrencia quemada en coordinación.
* Los docs que se saltean (`S5_DONE` previo, claim perdido contra un
  competidor) **consumen un slot completo para no subir nada**. En un
  re-run con miles de skips, la cola de S5 avanza al ritmo del
  semáforo aunque no haya ningún upload real.

## Qué

Mover el pre-flight de idempotencia **antes** del `acquire`: el slot se
toma únicamente para el upload real.

### Requisitos

**REQ-001 — Pre-flight sin slot.** `is_stage_done(S5_DONE)`,
`mark_stage_pending` y `try_claim` corren antes de adquirir el semáforo
(o el semáforo de lane). Los outcomes `done` (ya subido) y `skipped`
(claim perdido) retornan sin haber tocado el slot.

**REQ-002 — El slot cubre solo el upload.** `acquire` → `StageTimer` +
`uploader.upload` + marks de resultado → `release`. La semántica de
los contadores (`mark_busy`/`mark_idle`/`mark_completed`/`mark_failed`)
no cambia.

**REQ-003 — Semántica de cancelación intacta.** El chequeo cooperativo
del token sigue siendo previo a todo (paridad 097): un doc cancelado no
hace pre-flight ni toma slot.

**REQ-004 — Sin cambio de contrato.** Mismos outcomes
(`done`/`failed`/`skipped`), mismas transiciones de tracking, mismos
counters. Los tests existentes de S5 pasan sin modificación.

### Nota sobre la ventana de claim

Con el claim fuera del slot, un doc puede quedar `STSCOD='I'` en
NIARVILOG mientras espera un slot (antes, claim y upload eran
contiguos). La espera está acotada por
`threads_del_pool × duración_de_upload / capacity` — minutos en el peor
caso, muy por debajo del umbral de `stale_in_progress_minutes`
(default 30) que recicla claims huérfanos. Sin cambio de configuración.

### Fuera de alcance

* Batchear las escrituras NIARVILOG de S5 (hallazgo P14) — cambio
  separado.
* Reusar los pools de threads entre chunks (P20).

## Escenarios

**E1 — El claim ocurre antes de tomar el slot.**
Dado un pipeline con coordinator activo,
cuando se procesa un documento,
entonces la secuencia observada es `is_stage_done` → `try_claim` →
`acquire` → upload (nunca `acquire` antes del pre-flight).

**E2 — Un skip no consume slot.**
Dado un doc ya en `S5_DONE` de una corrida previa,
cuando S5 lo procesa,
entonces retorna `done` sin haber llamado `acquire`.

**E3 — Claim perdido no consume slot.**
Dado un coordinator cuyo `try_claim` devuelve False,
cuando S5 procesa el doc,
entonces retorna `skipped` sin haber llamado `acquire`.

**E4 — Happy path intacto.**
Dado un run normal de 2 docs,
cuando corre,
entonces `s5_done == 2` y el tracking queda idéntico a pre-109.
