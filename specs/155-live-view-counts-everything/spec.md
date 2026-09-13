# 155 — La vista en vivo tiene que contar lo mismo que el censo

## Por qué

Una corrida del operador terminó así:

```
batch — completada  elapsed 26s
subidos 0  fallidos 0  salteados 22618
corrida completed · 0 subidos · 0 fallidos

  OUTCOMES (cumulative)
    S5_DONE         0
    S5_FAILED       0
    S1_FILTERED 22618
    S1_SKIPPED      0
```

Diez documentos habían fallado en S2 con
`identity.cif could not be resolved from field 'BAC_CIF'`. El monitor
dijo **`0 fallidos`**, en verde, y ni una línea los mencionaba. Recién
aparecieron en `7·BATCHES`, que lee la base.

La causa es puntual. `tui/data_provider.py:321`:

```python
failed_total, failures_by_type, failures_by_status = (
    self._upload_metrics.failure_breakdown()
)
```

`failed_total` sale de las métricas de **UPLOAD**: es el desglose de S5
por tipo y status HTTP de la spec 104. Una falla de S2, S3 o S4 no pasa
por ahí. Y `OUTCOMES` (`tui/bucket_tab.py:88-93`) tiene cuatro líneas
—`S5_DONE`, `S5_FAILED`, `S1_FILTERED`, `S1_SKIPPED`— y ninguna para las
etapas del medio.

El dato existe: `staged.py` calcula `s2_failed`, `s3_failed` y
`s4_failed`, y `tui/chunks_tab.py` ya renderiza `prep_failed` en la vista
multi-batch. **Nunca se plomeó a la vista de un batch simple.**

Este es el cuarto caso de la misma familia en una sola jornada: el
sistema hizo lo correcto —falló los documentos, los registró, con un
mensaje accionable— y lo comunicó mal. Los otros tres: el monitor decía
"deleted at source" sobre documentos que nadie borró (153), el mensaje de
un crash tiraba el `str(exc)` que era lo único útil (154), y el
`except BaseException` de streaming contaba en el tally sin persistir
nada (148).

## Qué

### REQ-001 — La invariante

> **Lo que la vista en vivo cuenta al terminar tiene que coincidir con lo
> que `batch show` cuenta después.** Si difieren, la vista en vivo está
> mintiendo — la base es la verdad.

No se trata de mostrar más números: se trata de que los que ya se
muestran dejen de ser de una sola etapa.

### REQ-002 — `fallidos` cuenta TODAS las etapas

`TUISnapshot.failed_total` pasa a ser la suma de las fallas terminales de
todas las etapas (S1..S5), no el `failure_breakdown()` de upload. El
desglose de 104 sigue existiendo tal cual para el bloque
`ERRORS BY TYPE`, que **se re-rotula a `ERRORS BY TYPE (UPLOAD)`**: ese
sí es específico de CMIS y está bien que lo sea, lo que estaba mal era
que su total se usara como el total de la corrida.

Alcanza a la cabecera del monitor (`cli/console/monitor_pane.py:71`) y al
aviso de cierre de `cli/console/app.py` (`Corrida {outcome}: {done}
subidos · {failed} fallidos`), que leen la misma fuente.

### REQ-003 — `OUTCOMES` muestra dónde murieron

El bloque de `tui/bucket_tab.py:88-93` suma las etapas que hoy faltan.
Sólo se rinden las que tienen algo (un bloque de nueve líneas en cero es
ruido), pero `S5_DONE` y el total de fallas se muestran siempre, aunque
valgan cero:

```
OUTCOMES (cumulative)
  S5_DONE         0
  FALLIDOS       10        ← total, todas las etapas
    S2_FAILED    10
  S1_FILTERED 22618
  S1_SKIPPED      0
```

### REQ-004 — Una corrida sin un solo documento subido no se anuncia en verde

`corrida completed · 0 subidos · 0 fallidos` en verde, con 22618
documentos excluidos y 10 fallados, describe un desastre con el tono de
un éxito. Cuando `S5_DONE == 0` y hubo documentos elegibles, el cierre
sale en `warning` y nombra el motivo dominante, con el puntero a
`batch show`. El color es información, no decoración.

### REQ-005 — Un test que ate las dos vistas

Un test de integración que corra un batch con fallas en S1, S2, S3, S4 y
S5 a la vez y exija que el `failed_total` del snapshot **coincida** con
la suma de fallas que `get_batch_details` reporta para ese batch. Es el
candado de REQ-001: sin él, la próxima etapa que se agregue vuelve a
quedar afuera del contador y nadie se entera.

### REQ-006 — Docs

`docs/how-to/operator/interpret-the-tui-tabs.md` (qué cuenta cada número
y que `ERRORS BY TYPE` es sólo de upload);
`docs/explanation/operations-console.md`; `CHANGELOG.md`.

## Fuera de alcance

- **Rediseñar el monitor.** Se arreglan los números que ya están, no se
  agregan paneles.
- **El desglose por `reason_code` en vivo.** El contador vive en memoria
  y no lo tiene; ese desglose es de `batch show` y así se documentó en
  153. Acá sólo se corrige CUÁNTOS, no POR QUÉ.
