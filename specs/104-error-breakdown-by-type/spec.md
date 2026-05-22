# 104 — Desglose de la tasa de error por tipo

## Por qué

El plan de stress (§3.4.2) exige la tasa de error **desglosada por
tipo**: timeout, HTTP 4xx, HTTP 5xx, error de aplicación. Y los umbrales
de §6.1 (🟢 <0,5% · 🟡 0,5–2% · 🔴 >2%) se aplican sobre esa tasa.

Un **único número** de tasa de error es indiagnosticable. No es lo mismo:

* un **5xx** — el servidor destino se está cayendo / saturando;
* un **4xx** — nuestro request está mal armado;
* un **timeout** — la red o el destino se saturan sin rechazar
  explícitamente.

T05 (estrés) existe **específicamente** para caracterizar *cómo* degrada
el sistema. Con un solo contador eso es imposible: necesitás ver que a
las 2x de concurrencia empiezan los 503, no "subió la tasa de error".

Estado actual, verificado en el código:

* La clasificación **ya existe** en los tipos de excepción:
  `CMISClientError.status_code` (4xx), `CMISServerError.status_code`
  (5xx), y los timeouts como `httpx.TimeoutException` dentro de
  `RetriesExhaustedError.__cause__`.
* Pero S5 la **descarta**:

  ```
  # staged.py:1228
  except (CMISClientError, CMISServerError, RetriesExhaustedError) as exc:
      ...
      mark_stage_failed(txn, batch_id, StageStatus.S5_FAILED, str(exc))
      return "failed"
  ```

  Los tres tipos se colapsan; el tipo queda solo en un string libre.
* `MetricsRecorder.record_upload_failed()` **no toma argumentos**
  (`staged.py:1050,1129`; `streaming.py:719,829`) — incrementa un único
  `_s5_failed: int`.
* `BatchSummary.to_record()` **no incluye el conteo de fallas** — sus
  campos son `pipeline`, `batch_id`, `total_docs`, `elapsed_s`,
  `throughput_docs_per_s`, `stages`.
* El dato crudo **con `status_code` ya existe** por documento en
  `network-*.jsonl` (`CmisUploader._emit_upload_failed`). El gap es
  puramente la **agregación y clasificación**.

## Qué

Clasificar cada falla de upload (S5) en un bucket tipado, agregar los
conteos por tipo (y por status HTTP exacto), y exponerlos en el resumen
de batch y en el TUI.

### Requisitos

**REQ-001 — Taxonomía de error.** Cinco buckets:
`timeout`, `http_4xx`, `http_5xx`, `transport`, `app_error`.

> Es un **superset** de los 4 del plan: separamos `transport` (un
> `ConnectError`, un abort Windows 10053) de `app_error` (un PDF roto,
> una respuesta malformada) porque diagnósticamente no son lo mismo.
> El plan los agruparía como "error de aplicación"; este desglose es
> estrictamente más útil y se colapsa fácil si la planilla lo pide.

**REQ-002 — Clasificador puro y testeable.** Una función pura
`classify_failure(exc) -> ErrorCategory`. Debe **desenvolver**
`RetriesExhaustedError.__cause__` para encontrar la causa raíz: un
timeout que agota el presupuesto de reintentos cuenta como `timeout`,
NO como una categoría genérica de "reintentos agotados". Sin red, sin
I/O — se testea con instancias de excepción.

**REQ-003 — `record_upload_failed` recibe la categoría.** Hoy no toma
argumentos. Pasa a `record_upload_failed(category, status_code=None)`.
El handler de S5 (`staged.py:1228`) clasifica `exc` antes de llamarlo.

**REQ-004 — Sub-desglose por status HTTP exacto.** Además del bucket,
contar el status code exacto: `{503: 38, 500: 2}`. Para un stress test,
saber que es **503** (señal de sobrecarga / backpressure del destino) y
no un 500 genérico es justo lo que T05 necesita ver.

**REQ-005 — En el `BatchSummary`.** `to_record()` agrega tres campos:
`failed_total`, `failures_by_type` (dict bucket→conteo) y
`failures_by_status` (dict status→conteo). Con `total_docs` ya presente,
la tasa de error (`failed_total / total_docs`) y su comparación contra
los umbrales de §6.1 quedan derivables.

**REQ-006 — Visible en vivo en el TUI.** El desglose aparece en la tab
de upload/detalle **durante** la corrida, no solo al cierre. Durante T05
querés ver el instante en que arrancan los 5xx, no enterarte al final.

**REQ-007 — Un 409 recuperado NO es un error.** Un 409 que
`CmisUploader._try_recover_409` resuelve devuelve un objectId —
`upload()` no lanza, nunca llega al clasificador. No requiere manejo
especial; se documenta para que quede explícito que la recuperación de
409 no infla la tasa de error.

### No-soluciones descartadas

> ❌ **Parsear el string de error de SQLite.** El tipo hoy se persiste
> como texto libre vía `str(exc)`. Reconstruir la categoría parseando
> ese string es frágil y dependiente del formato del mensaje. La
> categoría se calcula en el origen, con el objeto excepción en mano,
> donde la información es estructurada.

### Fuera de alcance

* **Conteo de reintentos** (el plan §3.4.2 también lo pide). Relacionado
  pero distinto — es un eje de medición aparte. El dato crudo ya existe
  en `RetriesExhaustedError.attempts` y en `network-*.jsonl`. Candidato
  a ampliación o spec hermano.
* **Fallas pre-S5 (S2/S3/S4).** Con el corpus sintético del spec 102
  (PDFs de una página, sin mapping faltante ni archivos ausentes) son
  prácticamente nulas. Si aparecieran, caen en `app_error`. Un desglose
  de errores por etapa del pipeline es otro spec.
* **Exit codes y comparación contra umbrales.** Este spec produce los
  números; compararlos contra §6.1 (verde/amarillo/rojo) lo hace la
  planilla de análisis del plan de pruebas.

## Escenarios

**E1 — 5xx clasificado con su status.**
Dado un upload que recibe HTTP 503 y agota reintentos,
cuando S5 registra la falla,
entonces incrementa `failures_by_type["http_5xx"]` y
`failures_by_status[503]`.

**E2 — Timeout envuelto en RetriesExhausted.**
Dado un upload cuyos reintentos se agotan por un `ReadTimeout`,
cuando `classify_failure` procesa el `RetriesExhaustedError`,
entonces desenvuelve `__cause__` y cuenta `timeout` — no `app_error` ni
una categoría de "reintentos".

**E3 — 4xx.**
Dado un upload que recibe HTTP 400,
cuando S5 registra la falla,
entonces incrementa `http_4xx` y `failures_by_status[400]`.

**E4 — Resumen con tasa derivable.**
Dado un batch de 10.000 docs con 40 fallas (38×503, 2×timeout),
cuando se emite el `BatchSummary`,
entonces `to_record()` reporta `failed_total=40`,
`failures_by_type={"http_5xx":38,"timeout":2}`, y la tasa 0,4% es
derivable contra `total_docs`.

**E5 — 409 recuperado no cuenta.**
Dado un 409 que `_try_recover_409` resuelve a un objectId existente,
cuando termina el upload,
entonces no se incrementa ningún contador de error.

**E6 — Batch limpio, intacto.**
Dado un batch sin fallas,
cuando se emite el resumen,
entonces `failed_total=0` y ambos dicts quedan vacíos.
