# 106 — Higiene de conexiones ODBC: thread-safety en `As400DataSource` y poda de conexiones huérfanas

## Por qué

Dos hallazgos de la auditoría de rendimiento, ambos sobre el ciclo de
vida de conexiones pyodbc:

### A. `As400DataSource` comparte UNA conexión entre threads, sin lock

`adapters/sources/as400.py:84` cachea `self._conn` y `_connect()`
(`:202-214`) no tiene sincronización alguna. pyodbc declara
`threadsafety = 1`: el módulo es compartible, **las conexiones no**.

Quién la golpea concurrentemente:

* S1 vía `IndexingService` cuando la fuente RVABREP es AS400.
* S3 vía `MetadataService` (`metadata.py:475`) con fuentes `as400:` y
  `prefetch_enabled: false` — desde los `prep_workers` threads
  (`staged.py:761-767`).

Con `prep_workers > 1` hay cursores concurrentes sobre la misma
conexión: comportamiento indefinido a nivel driver (corrupción de
resultados, serialización espuria, crashes intermitentes). Además dos
threads pueden abrir dos conexiones en carrera (`:203-207`) y una queda
filtrada sin referencia.

El propio docstring del módulo (`as400.py:7-10`) lo admite como deuda:
"un cambio futuro va a sumar conexiones con `threading.local()`". El
`As400NiarvilogStore` ya lo implementó (spec 095); este adapter quedó
atrás.

### B. `As400NiarvilogStore` filtra conexiones de threads muertos

El pool 095 cachea la conexión en `threading.local` y la registra en
`_all_conns` (`as400_niarvilog.py:613-627`) para que `close()` pueda
cerrarlas. Pero los `ThreadPoolExecutor` de S5 se **crean y destruyen
por chunk** (`staged.py:1039-1042` single-lane, `:1105-1113` dual-lane):
cuando el pool del chunk K muere, sus threads mueren, y sus conexiones
quedan **abiertas y huérfanas** en `_all_conns` hasta el `close()` del
final de la corrida.

Con C chunks × `ceiling` workers (hasta 50, ×2 en dual-lane) se acumulan
**cientos de jobs QZDASOINIT abiertos en el iSeries** durante una
corrida larga — presión real sobre un sistema de producción del banco.

## Qué

Un helper compartido `ThreadLocalConnectionPool` (`adapters/connection_pool.py`) que
encapsula el patrón completo: conexión thread-local lazy + registro
global para cierre + **poda de conexiones cuyos threads ya murieron**.
Ambos adapters lo adoptan.

### Requisitos

**REQ-001 — `ThreadLocalConnectionPool`.** API: `acquire()` (conexión del
thread actual, lazy vía factory inyectada), `reset_current()` (cierra y
desregistra solo la del thread actual), `close_all()` (cierra todas
desde cualquier thread), `open_count` (para tests/observabilidad). El
factory es quien envuelve `pyodbc.connect` y traduce errores al tipo del
adapter — el pool no conoce pyodbc.

**REQ-002 — Poda de threads muertos.** El registro guarda
`(thread, conn)`. Al crear una conexión nueva, el pool cierra y
desregistra las entradas cuyo thread ya no está vivo. Así el ciclo
"pool de chunk muere → pool de chunk nuevo conecta" recicla las
conexiones del chunk anterior: el número de conexiones abiertas queda
acotado por los threads vivos (+ la ventana entre chunks), no por el
total histórico de threads.

**REQ-003 — `As400DataSource` thread-safe.** Adopta el pool: una
conexión por thread, `close()` cierra todas, sin carreras en la
creación. La semántica de errores (`IndexingError` con SQLSTATE) no
cambia.

**REQ-004 — `As400NiarvilogStore` sin fuga.** Reemplaza su
`_local`/`_all_conns`/`_conns_lock` por el pool. El comportamiento 095
(conexión por worker, `_reset_connection` en retry, `close()` global) se
preserva — los tests de `test_as400_niarvilog_pool.py` siguen verdes sin
tocar.

### Fuera de alcance

* Reusar los `ThreadPoolExecutor` entre chunks (hallazgo P20 — el churn
  de threads). Ese es el fix estructural de fondo, pero toca los
  orchestrators; va como cambio separado. 106 acota el daño desde el
  lado del adapter.
* Límite máximo de conexiones del pool.

## Escenarios

**E1 — N threads sobre `As400DataSource` → N conexiones.**
Dado 8 threads haciendo `get_by_fields` concurrente,
cuando corren,
entonces se abren 8 conexiones, cada una tocada por exactamente un
thread.

**E2 — Poda al reciclar workers.**
Dado un primer pool de 4 threads que conecta y muere, y un segundo pool
de 4 threads que conecta,
cuando el segundo pool abre sus conexiones,
entonces las 4 conexiones del primer pool quedan cerradas y el pool
reporta como abiertas solo las vivas.

**E3 — `close()` global intacto.**
Dado conexiones abiertas por varios threads,
cuando `close()` se llama desde el thread principal,
entonces todas quedan cerradas (incluidas las de threads vivos).

**E4 — Retry resetea solo la conexión propia.**
Dado un `OperationalError` transitorio en un worker,
cuando el retry resetea la conexión,
entonces solo la del thread que reintenta se cierra y reabre (paridad
095, test existente).
