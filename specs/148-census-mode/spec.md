# 148 — Censo del origen: todo documento tiene una razón

## Por qué

Hoy el operador no puede responder *"¿qué había en el origen y qué pasó
con cada cosa?"*. Sólo puede responder *"¿qué procesé?"*. Son preguntas
distintas y la segunda no sirve para auditar una migración.

El rastreo encontró **19 caminos** por los que un documento del origen
termina sin subirse. **Dos** dejan una razón legible por máquina
(`S1_FILTERED`, `S1_SKIPPED`). **Ocho no escriben absolutamente nada.**

Tres defectos concretos, todos afectando números que el operador ya está
mirando:

1. **El allow-list de códigos es invisible por diseño.**
   `services/triggers/direct_rvabrep.py:98-121` mete el filtro MÁS CHICO
   en el `IN` del SQL y rechaza el otro en Python con un `continue`
   pelado. Si el filtro chico son los códigos (el caso normal: un
   sistema, veinte códigos), **los documentos excluidos nunca vuelven del
   AS400**. Y si el chico son los sistemas, el `continue` de la línea 120
   los tira sin fila, sin contador y sin una línea de log.

2. **`S1_FILTERED` y `S1_SKIPPED` se caen de todo agregado.**
   `adapters/tracking/sqlite.py:862-884` hardcodea
   `_DISPLAY_OUTCOMES = ("DONE","FAILED","PENDING")` y sólo copia el
   conteo `if outcome in pivot[stage]`. La spec 062 §4 afirmaba que
   `batch show` y `analyze` los recibían "gratis": es **falso**. Hoy, en
   `batch show`, `DONE + FAILED + PENDING` no suma el total y nadie avisa.

3. **La clave sintética de `S1_FILTERED` colisiona.**
   `orchestrators/staged.py:862` arma `FILTERED__{shortname}__{system_id}`
   contra un índice único `(rvabrep_txn_num, batch_id)` con
   `INSERT OR IGNORE` (`sqlite.py:99-102, 500-513`). En modo
   `direct_rvabrep` cada fila borrada levanta su propia excepción
   (`services/indexing.py:185-190`), así que **N documentos borrados del
   mismo cliente colapsan en 1 fila**.

Y no hay denominador: `migration_batch.total_records` se escribe una vez
en `start_batch` y nunca se actualiza — streaming pasa `0`
(`streaming.py:410`), staged pasa el `batch_size` configurado
(`staged.py:661`). No es el conteo de documentos del origen.

## Qué

### REQ-001 — El código NUNCA va al SQL. Siempre.

Sin flag, sin modo: un solo camino.

- `_iter_filtered_rows` (`direct_rvabrep.py:98-121`) se reescribe. La
  query lleva **únicamente** `filters.systems`. `filters.document_types`
  deja de tocar el SQL.
- **El camino con `systems` pasa a hacer streaming.** Hoy
  `get_by_fields_in` termina en `cursor.fetchall()`
  (`adapters/sources/odbc_base.py:115`), mientras que sólo la rama sin
  filtros usa `query_stream` con `fetchmany(500)` (`odbc_base.py:127-149`).
  Con el censo siempre activo, `filters.systems: ["1"]` es la
  configuración NORMAL, y materializar un sistema entero en una lista de
  Python no es aceptable. Se agrega el equivalente en stream y
  `direct_rvabrep` lo usa.
- El `continue` de la línea 120 desaparece: la fila que no matchea
  `document_types` se clasifica y se registra (REQ-004).
- Las filas con shortname o system_id en blanco
  (`direct_rvabrep.py:86-88`, hoy un INFO agregado al final del scan)
  también se registran.

`filters.document_types` cambia de significado, y se documenta: de
*"traeme sólo estos"* pasa a *"de todo lo que traiga, migrá estos y
contame el resto"*.

### REQ-002 — Taxonomía cerrada de razones, en tres baldes

`domain/models.py`: `ReasonCode(StrEnum)` y `ReasonBucket(StrEnum)`, con
un mapeo total código → balde. El balde es lo que importa: cada uno se
resuelve de una manera distinta y **por una persona distinta**.

| Balde | Significa | Quién lo resuelve |
|---|---|---|
| `EXCLUIDO` | Decisión del operador, o el origen dice que no | nadie, está bien así |
| `BLOQUEADO` | Falta configuración | el operador, editando YAML/CSV/manifest |
| `FALLO` | Se rompió en ejecución | reintento o investigación |

```
EXCLUIDO   EXCLUDED_BY_FILTER     el código no está en filters.document_types
           DELETED_AT_SOURCE      código de borrado en RVABREP
           ALREADY_UPLOADED       ya S5_DONE en un batch previo
           OUT_OF_SCOPE_RESUME    fuera del alcance del resume

BLOQUEADO  CODE_NOT_MAPPED        IDRVI sin fila en MapeoRVI_CM.csv
           TYPE_NOT_IN_MANIFEST   el IDCM no existe en el manifest (145)
           IDENTITY_UNRESOLVED    no se resolvió CIF/shortname (147)
           METADATA_UNRESOLVED    un field_source no dio y no hay default
           SOURCE_ROW_INCOMPLETE  la fila RVABREP no trae shortname/sistema

FALLO      SOURCE_FILE_MISSING    el archivo no está en disco
           ASSEMBLY_FAILED        el PDF no se pudo armar
           CM_REJECTED_4XX        Content Manager lo rechazó
           CM_ERROR_5XX
           CM_TIMEOUT
           CM_TRANSPORT
           CLAIM_LOST             otro proceso se llevó el claim
           CRASHED                excepción no contemplada
           CANCELLED              se canceló la corrida
           INDEXING_FAILED        la query de indexado explotó
           SOURCE_ROW_NOT_FOUND   el trigger no matcheó ninguna fila
```

Los cuatro `CM_*` salen de `classify_failure`
(`observability/error_classification.py:37-65`), que **ya produce ese
enum** y hoy sólo alimenta métricas — se conecta a la base, no se
reinventa.

### REQ-003 — Sin estados nuevos: `reason_code` es un eje ORTOGONAL

Decisión deliberada. `status` dice **dónde paró**; `reason_code` dice
**por qué**; el balde dice **quién lo arregla**. Tres preguntas, tres
campos, cero estados nuevos que propagar por la máquina de recovery, la
consola y los docs.

Ejemplo: un código no mapeado es `status=S2_FAILED` +
`reason_code=CODE_NOT_MAPPED` + balde `BLOQUEADO`. Que el status diga
"FAILED" no lo vuelve un error de ejecución: el balde lo desmiente, y el
balde es lo que se reporta.

### REQ-004 — Dos columnas nuevas en `migration_log`, y TODO camino escribe

`adapters/tracking/sqlite.py:62-84`:

- `reason_code TEXT` — el enum de REQ-002, `NULL` para los que subieron.
- `id_rvi TEXT` — **hoy el código RVI no se guarda en ninguna parte de la
  base** (sólo vive en `RVABREPDocument.index7`, en memoria). Sin esta
  columna no se puede agrupar el censo por código, que es exactamente la
  pregunta del operador. Se llena SIEMPRE, no sólo en las exclusiones.

DDL aditivo con el patrón que ya existe (`PRAGMA table_info` +
`ALTER TABLE ADD COLUMN`, `sqlite.py:325-339`, precedente de la spec 124).

Los **ocho caminos mudos** pasan a escribir fila con su `reason_code`:
`direct_rvabrep.py:86-121` (allow-list y filas incompletas),
`indexing.py:230` (el `active = [...]` que descarta borrados sin contar),
`staged.py:837-843` (`RVABREPNotFoundError`), `:889-895`
(`IndexingError`), `:897-906` (`resume_out_of_scope`), `:1455-1469`
(claim perdido), y **`streaming.py:745` y `:905`** (`except BaseException`
que hoy sólo incrementa el tally).

> Ese último punto arregla, de paso, el bug de los ~200 uploads perdidos:
> hoy la excepción no-CMIS se cuenta en el tally y no persiste nada, y
> como `mark_stage_pending` es `INSERT OR IGNORE` el documento queda como
> `S4_DONE`. Con el censo ese camino escribe `CRASHED` y deja de ser
> invisible. **El censo no es sólo un reporte: es la red que hace
> imposible perder un documento en silencio.**

Se arregla también la colisión de la clave sintética
(`staged.py:862`): cada fila borrada usa su `txn_num` real en lugar de
`FILTERED__{shortname}__{system_id}`.

### REQ-005 — Denominador real

`migration_batch.total_records` deja de ser el `batch_size` configurado o
un `0`: se actualiza con el conteo real de documentos del origen a medida
que S1 los ve. Es el número contra el que todo lo demás tiene que sumar.

### REQ-006 — `batch show` arreglado (la única superficie de este cambio)

- `_pivot_status_counts` (`sqlite.py:862-884`) deja de hardcodear
  `("DONE","FAILED","PENDING")`: rinde todas las salidas que existan.
  Arregla de una `batch show`, `batch export-report` y la consola
  `7·BATCHES`, que comparten la proyección.
- `batch show` agrega: **total en origen**, migrados, y el desglose por
  **balde** → `reason_code` → **`id_rvi`**, con conteos.
- **Los números tienen que SUMAR.** Si el total del origen no coincide
  con la suma de los baldes más los subidos, `batch show` lo dice
  explícitamente en vez de mostrar un cuadro que no cierra. Un reporte
  que no cuadra y no avisa es peor que no tener reporte.
- `batch export-report` exporta lo mismo a CSV/JSON.

### REQ-007 — Docs

`docs/explanation/pipeline-stages.md` (el censo y qué significa ahora
`filters.document_types`), `docs/reference/config-reference.yaml`,
`docs/reference/cli.md` (`batch show`), una guía de operación que lea el
censo balde por balde y diga qué hacer con cada uno, y `CHANGELOG.md`
`[Unreleased]` marcando el cambio de comportamiento.

## Fuera de alcance

- **Comando `census` propio y pantalla nueva en la consola.** Decisión
  del operador: primero que `batch show` diga la verdad. Si después hace
  falta una vista dedicada, tiene su spec.
- **Resumen obligatorio al final de la corrida.** Mismo motivo.
- **Colapsar errores repetidos en el reporte** (las 1194 líneas idénticas
  de `22003`). Transversal, no es de acá.
- **`sync recover` reparando fallas** (hoy sólo selecciona `S5_DONE`,
  `sqlite.py:635`). El censo los hace visibles; repararlos es otra spec.
