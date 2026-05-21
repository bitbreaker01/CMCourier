# 098 — Fix de pérdida de datos en el reconciliador periódico

## Por qué

Reporte del operador: una corrida en `mode: periodic` procesó **~4000
documentos** pero AS400 NIARVILOG quedó con solo **~555 registros**.
~3445 registros de tracking perdidos.

El rastreo confirma una pérdida de datos real — dos bugs del cambio
096 que se combinan en `services/reconciler.py`:

### Bug 1 — una falla per-ítem aborta el batch entero

`As400Reconciler.run_pass` (`reconciler.py:134-140`):

```python
for item in items:
    outcome = self._reconcile_item(item)   # ← sin try/except
```

`_reconcile_item` hace `read_state` / `try_claim` / `mark_uploaded`
contra AS400. Si **un** doc levanta una excepción (error DB2 no
transitorio, constraint, dato inesperado), la excepción desenrolla
**toda** la pasada. Todos los ítems posteriores al que falló no se
procesan.

### Bug 2 — el buffer se drena ANTES de procesar → lo no-procesado se pierde

`PeriodicReconciler.run_one` (`reconciler.py:308-316`):

```python
items = self._buffer.drain()        # vacía el buffer
try:
    return self._reconciler.run_pass(items, ...)
except Exception:
    _log.exception("periodic reconcile pass failed")   # se traga todo
    return ReconcileResult()
```

`drain()` saca los ítems del buffer. Si `run_pass` explota a mitad
(Bug 1), los ítems no procesados **ya no están en el buffer y nadie
los re-encola** — se pierden para siempre. La excepción se traga sin
gritar.

### Bug 3 — el daemon se mata a sí mismo a mitad de trabajo

`stop()` hace `join(timeout=30)` sobre un thread `daemon=True`. Si al
terminar la corrida el daemon está en una pasada larga, a los 30s
`stop()` sigue, el proceso termina y el thread daemon se mata en seco
con ítems drenados sin procesar.

**Resultado neto**: el reconciliador drena el backlog, falla en un
registro problemático, y todo lo que venía después en ese batch
drenado se evapora. Encaja exacto con "4000 → 555".

> Los **documentos no se perdieron** — están en CMIS y en SQLite como
> `S5_DONE`. Lo que falta son las filas de NIARVILOG. La recuperación
> de esos ~3445 es un trabajo aparte (necesita re-derivar el contexto
> del doc; ver Notas).

## Qué

Hacer que el reconciliador sea **resiliente y sin pérdida**: ningún
ítem drenado puede desaparecer sin quedar registrado.

### Cambios

1. **`run_pass` resiliente per-ítem** — `cleanup_stale_in_progress`,
   cada `_reconcile_item`, y `_import_foreign_uploads` van envueltos
   en try/except. `run_pass` **nunca levanta excepción** y procesa
   **todos** los ítems: cada uno termina como `synced` / `conflict` /
   `consistent` / `failed`.

2. **Re-encolar los fallidos** — los ítems cuyo `_reconcile_item`
   falló se devuelven en `ReconcileResult.requeued`; `run_one` los
   re-`append`ea al buffer para reintento en la próxima pasada. Un
   doc permanentemente malo reintenta y se loguea cada vez —
   **visible, nunca perdido en silencio**.

3. **Corte cooperativo en `run_pass`** — nuevo parámetro
   `stop_event: threading.Event | None`. El loop lo chequea entre
   ítems; si está seteado, corta y manda el resto a `requeued`. El
   daemon le pasa su `_stop` → al pararse, sale rápido sin abandonar
   nada. La pasada FINAL de `stop()` corre con `stop_event=None`
   (sin corte) en el worker thread `daemon=False` — no la puede
   matar el cierre del proceso.

4. **`stop()` no mata trabajo** — con el corte cooperativo el daemon
   sale rápido; `stop()` joinea y después corre la pasada final
   completa en el thread no-daemon.

5. **Observabilidad** — `ReconcileResult` suma `failed` (conteo);
   `reconcile_pass` ahora logea `failed`, y un `reconcile_failure`
   por ítem fallido con el txn y el error. La pasada final logea un
   WARNING fuerte si quedan ítems sin sincronizar.

### Tests

`tests/unit/services/test_reconciler.py` (extender):

* Un `_reconcile_item` que levanta excepción → los demás ítems se
  procesan igual; el fallido va a `requeued`; `run_pass` no levanta.
* `cleanup_stale_in_progress` que levanta → los ítems se procesan
  igual.
* `run_one` re-encola los `requeued` al buffer.
* `run_pass` con `stop_event` seteado → corta, el resto va a
  `requeued`.
* `stop()` corre la pasada final completa sin perder ítems.

## Criterios de aceptación

1. Una pasada con un doc que falla sincroniza **todos los demás** —
   ningún ítem se pierde por una falla per-ítem.
2. Los ítems fallidos quedan en el buffer (reintento) o logueados
   como WARNING en la pasada final — **nunca desaparecen en silencio**.
3. `run_pass` no levanta excepción bajo ninguna falla de AS400.
4. Parar el daemon no mata ítems drenados sin procesar.
5. Sin fallas, el comportamiento es el de 096.
6. `pytest -m unit` pasa.

## Riesgos

* **Reintento infinito de un doc permanentemente malo**: se re-encola
  cada pasada. Mitigación v1: se loguea como `reconcile_failure` cada
  vez (visible). Un límite de reintentos / dead-letter queda para un
  cambio futuro.
* **Pasada final larga**: con un backlog grande, la pasada final de
  `stop()` puede tardar — pero corre en el worker thread no-daemon, así
  que la corrida no "termina" hasta sincronizar todo. Es el trade
  correcto: más lento, pero sin pérdida.

## Notas

- **Mitigación inmediata para el operador** (mientras 098 no esté
  desplegado): volver a `tracking.as400_sync.mode: claim`. El claim
  por-documento es más lento pero correcto — no tiene buffer ni
  batch que abortar.
- **Recuperación de los ~3445 perdidos**: trabajo aparte. La tabla
  SQLite no guarda `DOCFRM` / `IMGTIP` / `IDNBAC` / `TIPIDN` que el
  INSERT a NIARVILOG necesita; habría que re-derivarlos re-corriendo
  S0–S2 (sin upload ni ensamblado) para los txn faltantes. Se
  planifica como cambio separado.
- Gaps relacionados conocidos, NO incluidos acá para mantener el
  hotfix acotado: `_run_sequential` no arranca el daemon;
  `doctor --check as400_sync` no valida nombres de columna.
- Cambio relacionado: 096 (introdujo el reconciliador).
