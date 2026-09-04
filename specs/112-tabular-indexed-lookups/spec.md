# 112 — `TabularDataSource`: lookups indexados en vez de full scan por consulta

## Por qué

`get_by_fields` (`tabular.py:113-120`) hace una **máscara booleana
O(filas) por cada filtro, por cada llamada** — reasignando el DataFrame
en cada paso. Los callers del hot path lo invocan **una vez por
documento**:

* `MetadataService` (`metadata.py:475`) — un lookup por campo con
  fuente `csv:` por documento (la cascada de S3).
* `IndexingService._query_for_trigger` (`indexing.py:208-213`) — una
  consulta RVABREP por `ClientTrigger` en S1.
* `LocalScanTriggerStrategy` (`local_scan.py:99`) — una consulta por
  **archivo escaneado**; en streaming además corre bajo el lock global
  del iterator de triggers, serializando a todos los producers.

Con un RVABREP mirror de millones de filas, S1+S3 son O(docs × filas):
el costo domina la fase PREP entera y es CPU pura de pandas tirada en
scans repetidos sobre datos inmutables.

## Qué

Índices hash **lazy por combinación de columnas**: la primera consulta
con una combinación de campos construye un índice
`valor(es) → posiciones` vía `DataFrame.groupby(...).indices` (un solo
scan); las consultas siguientes son un lookup de dict O(1) + un
`iloc` de las filas que matchean.

### Requisitos

**REQ-001 — Semántica idéntica.** Mismo resultado que el scan
secuencial para todos los casos: filtros multi-campo (igualdad AND),
filtros vacíos (todas las filas), sin match (lista vacía), columna
inexistente (`KeyError`), celdas NaN (nunca matchean — `groupby`
descarta claves NaN igual que `==`), orden de filas original.

**REQ-002 — Índice por demanda, una sola vez.** El índice de una
combinación de columnas se construye en la primera consulta que la usa
y se reutiliza después. El DataFrame es inmutable post-carga, así que
no hay invalidación. `close()` libera los índices junto con el df.

**REQ-003 — Thread-safe.** La construcción del índice se sincroniza —
los `prep_workers` threads consultan concurrentemente.

**REQ-004 — `get_by_fields_in` y `get_all` sin cambios.** `isin` ya es
un único scan por llamada batcheada; `get_all` sigue lazy (050).

## Escenarios

**E1 — Equivalencia funcional.** Para un CSV dado, `get_by_fields` con
filtros de 1 y 2 campos devuelve exactamente lo mismo pre/post-112.

**E2 — El índice se construye una vez.** Dos consultas con la misma
combinación de campos → un solo groupby.

**E3 — NaN nunca matchea.** Una fila con celda vacía no aparece al
filtrar por esa columna.

**E4 — Concurrencia.** 8 threads consultando la misma fuente devuelven
resultados correctos sin errores.
