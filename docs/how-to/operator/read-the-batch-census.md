# Leer el censo de un batch y actuar sobre cada balde

> [← Volver al índice](../../INDEX.md) · [How-to](../README.md) · [Operador](README.md)

Desde la spec **148**, `cmcourier batch show` responde una pregunta que antes
no podía responder: *"¿qué había en el origen y qué pasó con CADA cosa?"*.
Todo documento que el origen entregó termina con una razón, y cada razón cae
en uno de tres **baldes**. El balde no es una etiqueta decorativa: dice
**quién tiene que hacer algo**.

| Balde | Significa | Quién lo resuelve |
|---|---|---|
| `EXCLUIDO` | Decisión tuya, o el origen dice que no | **nadie** — está bien así |
| `BLOQUEADO` | Falta configuración | **vos**, editando YAML / CSV / manifest |
| `FALLO` | Se rompió en ejecución | reintento o investigación |

Ese orden —`EXCLUIDO` → `BLOQUEADO` → `FALLO`— es el orden en que el reporte
los imprime, y es el orden en que conviene leerlos: de "no hagas nada" a
"andá a arreglarlo" a "investigá".

## Cuándo aplica

- Terminó una corrida y tenés que decir cuántos documentos se migraron **de
  cuántos había**.
- El total en origen y los migrados no coinciden y querés saber por qué.
- Antes de dar una migración por cerrada.

## Pre-requisitos

- Un `batch_id` (lo lista `cmcourier batch list -c config.yaml`).
- El YAML del pipeline, para que el comando sepa dónde está el tracking.

## 1. Sacar el censo

```console
$ cmcourier batch show -c config.yaml 4f3c1f2a-...
Batch: 4f3c1f2a-...
Status: completed
Started: 2026-09-11T14:33:41
Completed: 2026-09-11T15:02:07
Total en origen: 9
Migrados: 1

STAGE  DONE  FAILED  PENDING  FILTERED
S0     0     0       0        0
S1     0     0       0        3
S2     0     1       0        0
S3     0     0       0        0
S4     0     0       0        0
S5     1     1       0        0

CENSO — por qué no se subió cada documento
Baldes: EXCLUIDO 3 · BLOQUEADO 1 · FALLO 1

BALDE      RAZON               ID_RVI  DOCS
EXCLUIDO   EXCLUDED_BY_FILTER  CC03    2
EXCLUIDO   EXCLUDED_BY_FILTER  AA01    1
BLOQUEADO  CODE_NOT_MAPPED     ZZ99    1
FALLO      CM_TIMEOUT          CC03    1

!! DESCUADRE: migrados 1 + censados 5 = 6, pero el total en origen es 9 — 3 documentos sin explicar.
```

Lo primero que mirás es la **última línea**:

- `Cuadre OK: …` — todo documento del origen está explicado. Podés confiar en
  el cuadro de arriba.
- `!! DESCUADRE: …` — **hay documentos que nadie explica**. El cuadro sigue
  siendo cierto en lo que dice, pero está incompleto: no lo uses como
  reporte final. Ver [§5](#5-qué-hacer-con-un-descuadre).

El desglose está ordenado a propósito: dentro de cada balde, el `reason_code`
que se llevó **más** documentos va primero, y dentro de un código, el `ID RVI`
más numeroso. La primera fila de cada balde es siempre la que más rinde
atacar.

## 2. `EXCLUIDO` — no hagas nada

Estos documentos no se migraron **porque así tenía que ser**. Si este balde
es grande, no es un problema: es la prueba de que tus filtros funcionan.

| Razón | Qué pasó | Qué hacer |
|---|---|---|
| `EXCLUDED_BY_FILTER` | El código RVI no está en `trigger.filters.document_types` | Nada — salvo que el código DEBIERA migrarse: ahí agregalo a la lista |
| `DELETED_AT_SOURCE` | La fila RVABREP trae código de baja (`ABACST`) | Nada. El origen lo dio de baja |
| `ALREADY_UPLOADED` | Ya estaba en `S5_DONE` en un batch previo | Nada. Es la idempotencia cross-batch haciendo su trabajo |
| `OUT_OF_SCOPE_RESUME` | Quedó fuera del alcance del `resume` que corriste | Nada, **si el alcance era el que querías**. Si no, re-corré el resume con el alcance correcto |
| `CLIENT_NOT_ACTIVE` (150) | El cliente no está en la lista de clientes con producto activo | **Nada. Es la directiva de negocio.** Content Manager no tiene espacio para todo RVABREP y sólo se migran los clientes activos |

> **`CLIENT_NOT_ACTIVE` no es un error ni una falta de configuración.** Es la
> directiva de negocio funcionando. Si este balde es enorme, está bien: ése
> era el punto. Lo que SÍ conviene chequear es **qué lista** lo decidió —
> `batch show` la imprime debajo del censo, con su ruta, su fecha de
> modificación y su cantidad de filas:
>
> ```
> Lista de activos (150): C:\ruta\clientes-activos.csv
>   modificada: 2026-08-31T09:15:00 · filas: 412339
> ```
>
> Si el conteo de filas te parece bajo, la lista puede estar incompleta.
> (Una lista **vacía** nunca llega hasta acá: la corrida aborta antes del
> primer documento — ver
> [`../client-eligibility.md`](../client-eligibility.md).)

> **Ojo con `EXCLUDED_BY_FILTER`.** Desde 148, `filters.document_types` ya no
> recorta la consulta al origen: el documento **viene igual** y se cuenta. Por
> eso este balde existe. Si ves un `ID RVI` acá que esperabas migrar, el
> arreglo es una línea de YAML — ver
> [`../../explanation/pipeline-stages.md`](../../explanation/pipeline-stages.md#s0--trigger-acquisition).

## 3. `BLOQUEADO` — esto lo arreglás vos, editando configuración

Ninguno de estos es un error de ejecución: reintentar **no sirve de nada**
hasta que cambies algo. Cada razón tiene un arreglo concreto y distinto.

| Razón | Qué falta | El arreglo, paso a paso |
|---|---|---|
| `CODE_NOT_MAPPED` | El `ID RVI` no tiene fila en `MapeoRVI_CM.csv` | El `ID_RVI` de la columna del censo es el que falta. Agregá la fila `IDSistema,IDRVI,IDCM` al CSV (modo manifest) y verificá con `cmcourier inspect mapping <ID_RVI> --system <n>` |
| `TYPE_NOT_IN_MANIFEST` | El `IDCM` al que mapea el código no existe en el manifest de tipos (145) | `cmcourier types discover -c config.yaml` para refrescar el manifest, `cmcourier types review <IDCM>` para marcar qué propiedades van al wire, y `cmcourier types check -c config.yaml` para confirmar que el cruce cerró |
| `IDENTITY_UNRESOLVED` | No se pudo resolver el shortname / CIF / sistema del cliente | Revisá la cadena `identity:` del YAML: el mensaje de error nombra el slot Y cada salto que se intentó. Ver [`../identity-chain.md`](../identity-chain.md) |
| `METADATA_UNRESOLVED` | Un campo requerido no lo dio ninguna fuente y no hay default | Agregá una fuente más a `metadata.field_sources.<CAMPO>.sources`, o un `default_value`. Ver [`../metadata-format.md`](../metadata-format.md) |
| `SOURCE_ROW_INCOMPLETE` | La fila RVABREP viene sin shortname o sin sistema | No lo arreglás vos: es dato sucio en el origen. Sacá la lista con el export y pasásela a quien administra RVABREP |

Después de cambiar la configuración:

```console
$ cmcourier batch retry-failed -c config.yaml --batch <batch_id>
$ cmcourier apply -c config.yaml --resume <batch_id>
```

`retry-failed` limpia el `reason_code` junto con el error: un documento que
reintentó bien deja de figurar en el censo y pasa a contar como migrado.
No queda contado dos veces.

Y no toca el balde `EXCLUIDO` (150): esas filas se quedan como están, con su
razón intacta. Reintentar una decisión de negocio no la cambia de opinión —
por eso el `Reset N` puede ser menor que la cantidad de `*_FAILED` del batch.

## 4. `FALLO` — reintento o investigación

Acá sí sirve reintentar, **pero no para todo**. La diferencia importa: tres
de estas razones son transitorias y las otras no se van a arreglar solas.

**Transitorias — reintentá, probablemente pasen:**

| Razón | Qué pasó |
|---|---|
| `CM_TIMEOUT` | Content Manager no respondió a tiempo |
| `CM_ERROR_5XX` | Content Manager devolvió un error de servidor |
| `CM_TRANSPORT` | Se cortó la conexión (red, TLS, DNS) |
| `CLAIM_LOST` | Otro proceso se llevó el claim del documento |
| `CANCELLED` | Cancelaste la corrida |

Si son muchos y seguidos, no reintentes de una: mirá primero el link y el
servidor ([`../../runbooks/cmis-down.md`](../../runbooks/cmis-down.md),
[`../operator/tune-aimd-for-a-slow-link.md`](tune-aimd-for-a-slow-link.md)).
Reintentar contra un CMIS caído sólo llena el log.

**NO transitorias — reintentar da exactamente el mismo resultado:**

| Razón | Qué pasó | Qué hacer |
|---|---|---|
| `CM_REJECTED_4XX` | Content Manager **rechazó** el documento (propiedad inválida, tipo mal, permisos) | Leé el `ERROR` de la tabla `FAILED records`: dice qué propiedad. Se arregla en el manifest o en `field_sources`, no reintentando |
| `SOURCE_FILE_MISSING` | El archivo no está en disco donde RVABREP dice | Verificá `assembly.source_root` y que el share esté montado. Si el archivo realmente no existe, es dato faltante en el origen |
| `ASSEMBLY_FAILED` | El PDF no se pudo armar (TIFF corrupto, página ilegible) | Investigá el documento puntual; suele ser un archivo dañado en el origen |
| `CRASHED` | Excepción no contemplada | **Esto es un bug o un entorno roto.** Mirá el log estructurado del `txn_num`; si se repite, abrí un issue con el traceback |
| `INDEXING_FAILED` | La consulta de indexado explotó | Casi siempre AS400 caído o una credencial vencida — ver [`../../runbooks/as400-down.md`](../../runbooks/as400-down.md) |
| `SOURCE_ROW_NOT_FOUND` | El trigger no matcheó ninguna fila RVABREP | El trigger apunta a algo que no existe. Revisá el CSV de triggers o el filtro de sistemas |

> `CRASHED` es el que más vale la pena mirar aunque sea uno solo. Antes de
> 148 ese camino **no escribía nada**: el documento quedaba registrado como
> `S4_DONE` y nunca se subía. Si aparece, el censo te está mostrando algo que
> hasta hace poco era invisible.

## 5. Qué hacer con un descuadre

`!! DESCUADRE` significa que `migrados + censados` no da el total del origen.

- **Faltan documentos** (el caso normal del descuadre): hay filas del origen
  que S1 contó y que no terminaron en ningún estado terminal. Con el batch
  todavía en `in_progress` es esperable: **todavía está corriendo**. Con el
  batch `completed`, no lo es: exportá el reporte y buscá los `*_PENDING`
  de la tabla de etapas, que son los que quedaron a mitad de camino.
- **Sobran documentos** (`contados de más`): el denominador quedó corto.
  Pasa en batches viejos, donde `total_records` era el `batch_size`
  configurado y no el conteo real del origen.
- **El batch no registró NINGUNA razón** y el reporte te lo dice
  explícitamente: es un batch **anterior a 148**. No tiene censo y no lo va a
  tener; el número de "total en origen" de esa corrida nunca fue real.

## 6. Llevarse el censo a una planilla

```console
$ cmcourier batch export-report -c config.yaml --batch <batch_id> \
    --format csv --output censo.csv
```

El CSV trae **tres bloques** separados por una línea en blanco, cada uno con
su propio header: las etapas, el cuadre (`metric,value`) y el desglose
(`bucket,reason_code,id_rvi,count`). `--format json` trae lo mismo en un
objeto `census`, listo para un script.

> El reporte lleva metadata de documentos del banco: **tratalo como
> confidencial**. Ver [`../../explanation/pii-handling.md`](../../explanation/pii-handling.md).

## Verificación

Terminaste cuando:

1. La última línea dice `Cuadre OK`.
2. El balde `BLOQUEADO` está vacío (o cada razón que queda tiene una decisión
   tomada y escrita).
3. El balde `FALLO` está vacío o sólo tiene razones no transitorias que ya
   investigaste.
4. `EXCLUIDO` puede ser tan grande como quiera: es la parte del origen que
   decidiste no migrar, y ahora podés demostrarlo documento por documento.

## Ver también

- [`retry-only-failed-records.md`](retry-only-failed-records.md) — el ciclo de reintento en detalle
- [`../../explanation/pipeline-stages.md`](../../explanation/pipeline-stages.md) — el censo dentro del pipeline, y qué significa hoy `filters.document_types`
- [`../identity-chain.md`](../identity-chain.md) — resolver `IDENTITY_UNRESOLVED`
- [`../cm-type-manifest.md`](../cm-type-manifest.md) — resolver `TYPE_NOT_IN_MANIFEST`
- [`../../reference/cli.md`](../../reference/cli.md) — flags exactos de `batch show` y `batch export-report`
