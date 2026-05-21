# 095 — Connection pool por worker para el sync AS400

## Por qué

Con `tracking.as400_sync.enabled=true`, el operador reporta que la
velocidad de carga **baja substancialmente**. El rastreo del código
confirma la causa — y no es la latencia de red en sí, es estructural.

`As400NiarvilogStore` cachea **una única conexión `pyodbc`**:

```python
# as400_niarvilog.py:198 + :545
self._conn: Any = None
...
def _connect(self) -> Any:
    if self._conn is not None:
        return self._conn          # ← una sola conexión, para todo
    self._conn = pyodbc.connect(self._build_connection_string())
    return self._conn
```

Ese store es **único**: `wiring.py:229` construye un solo
`As400NiarvilogStore` dentro de un solo `IdempotencyCoordinator`.
Y `_upload_one` (`staged.py:1097`) corre en **N worker threads de S5
en paralelo**, y cada thread llama al mismo coordinador:

* `try_claim()` antes del upload → 1-2 round-trips (`UPDATE`, y
  `INSERT` si la fila no existía — el caso común en migración fresca).
* `mark_uploaded()` / `mark_failed()` después → 1 round-trip más.

→ **2-3 statements sincrónicos a DB2-for-i por documento**, y **todos
embotellados en una sola conexión ODBC**. Una conexión ODBC procesa
un statement por vez: el driver serializa. Por más workers que tenga
S5, la porción AS400 de los N workers va en fila india.

El `CmisUploader` **no** tiene este problema: usa un `httpx.Client`
con pool interno y `warm_connection_pool(workers)` (`cmis_uploader.py:279`)
pre-abre N sockets. El paralelismo de S5 es real para CMIS — y queda
**anulado para AS400**. Ese es el cuello de botella.

Hay un problema latente extra: cero `threading.Lock` en
`as400_niarvilog.py`. Una conexión `pyodbc` compartida entre threads
sin lock depende de que el driver de IBM i serialice internamente —
frágil, además de lento.

## Qué

Reemplazar la conexión única por **conexiones thread-local**: cada
worker thread abre y cachea su propia conexión `pyodbc`. Es el patrón
estándar para `pyodbc` + thread pool y restaura el paralelismo real
de los claim/mark.

### Cambios

1. **`As400NiarvilogStore.__init__`** — reemplazar `self._conn` por:

   ```python
   self._local = threading.local()        # conexión de ESTE thread
   self._all_conns: list[Any] = []        # registro para close()
   self._conns_lock = threading.Lock()    # protege _all_conns
   ```

   `threading.local` no expone las conexiones de otros threads, así
   que `_all_conns` lleva el registro de todas las abiertas para que
   `close()` pueda cerrarlas.

2. **`_connect()`** — lee/crea la conexión del thread actual:

   ```python
   def _connect(self) -> Any:
       conn = getattr(self._local, "conn", None)
       if conn is not None:
           return conn
       _import_pyodbc()
       try:
           conn = pyodbc.connect(self._build_connection_string())
       except _pyodbc_error_type() as exc:
           raise As400CoordinationError(f"NIARVILOG connect failed: {exc}") from exc
       self._local.conn = conn
       with self._conns_lock:
           self._all_conns.append(conn)
       return conn
   ```

3. **`_reset_connection()`** — resetea **solo** la conexión del thread
   que está reintentando (no la de los demás). Cierra, la saca de
   `_all_conns`, y limpia `self._local.conn`.

4. **`close()`** — recorre `_all_conns` bajo lock y cierra todas.
   Idempotente vía el flag `_closed` existente.

5. **Sin cambios de schema, sin cambios de wiring.** El "pool" se
   dimensiona solo: cada thread que toque el store abre su conexión.
   El techo natural es la cantidad de workers de S5
   (`StagedPipeline._s5_max_workers`) — ya acotado por AIMD/lanes.

### Tests

`tests/integration/adapters/test_as400_niarvilog_pool.py` (nuevo),
con el fake de `pyodbc` ya existente:

* N threads concurrentes ejecutan `try_claim` → se abren **N**
  conexiones distintas (una por thread), no una compartida.
* Dos llamadas en el **mismo** thread reusan la misma conexión.
* `_reset_connection()` en un thread no toca la conexión de otro.
* `close()` cierra **todas** las conexiones registradas, no solo
  la del thread que llama.
* Retry: un `OperationalError` en un thread resetea solo su
  conexión; el siguiente intento de ESE thread reconecta.

## Criterios de aceptación

1. Con `as400_sync.enabled=true` y N workers de S5, se abren hasta N
   conexiones AS400 — los claim/mark de distintos workers NO se
   serializan en una sola conexión.
2. `close()` no deja conexiones colgadas, sin importar qué thread la
   llame.
3. El comportamiento single-thread es byte-idéntico a pre-095
   (mismo SQL, mismo orden, mismo retry).
4. `pytest -m unit` y `pytest -m integration` pasan.

## Ganancia esperada

La porción AS400 deja de ser un punto serial. Con N workers, los
2-3 round-trips por doc se solapan en N conexiones en vez de
encolarse en una. El techo de throughput del sync deja de ser
`1 / (latencia_AS400 × round_trips)` y pasa a escalar con los
workers, igual que ya lo hace CMIS.

> Magnitud exacta: depende de la latencia real a DB2-for-i. Medir
> con `rg '"kind":"niarvilog_' network-*.jsonl` y comparar
> `duration_ms` agregado antes/después.

## Riesgos

* **N sesiones simultáneas a AS400**: pre-095 el banco veía 1 sola
  conexión de CMCourier; post-095 ve hasta `s5_max_workers`.
  **Confirmar con el banco** que el perfil de usuario AS400 admite
  ese número de sesiones concurrentes. Si no, hay que capear el
  pool — pero eso reintroduce algo de serialización (un cambio
  futuro podría agregar `as400_sync.max_connections`).
* **Conexiones huérfanas por thread muerto**: si un worker thread
  muere sin pasar por `close()`, su conexión queda en `_all_conns`
  hasta el `close()` final del store. Aceptable — `close()` siempre
  corre al terminar el pipeline.
* **`pyodbc.pooling` global**: `pyodbc` tiene pooling a nivel módulo
  activado por default. Las conexiones thread-local conviven bien
  con eso; no hace falta tocarlo.

## Notas

- Pareja conceptual con el cambio **096** (modo de sync periódico):
  095 hace que el sync por-documento escale; 096 ofrece sacarlo
  enteramente del critical path. Son ortogonales y combinables.
- El patrón espejo está en casa: `CmisUploader.warm_connection_pool`.
  095 no necesita un `warm_*` explícito porque `pyodbc.connect` es
  barato comparado con el handshake CMIS; la apertura lazy por
  thread alcanza. Un `warm_connection_pool` para AS400 queda como
  posible spec futura si el costo del primer claim de cada worker
  se vuelve visible.
- Relacionado: cambio 034 (sync AS400 original), cambio 087
  (`CommitMode=0` en el connection string — el `_build_connection_string`
  no cambia, las conexiones thread-local heredan el mismo string).
