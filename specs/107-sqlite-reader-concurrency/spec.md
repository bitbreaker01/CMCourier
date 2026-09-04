# 107 — Lecturas concurrentes del tracking store + índice sobre `batch_id`

## Por qué

Dos hallazgos de la auditoría, ambos en `adapters/tracking/sqlite.py`:

### A. Un solo lock global serializa TODAS las lecturas

El store abre **una** conexión de lectura con `check_same_thread=False`
y la protege con `_reader_lock` (`sqlite.py:249-254`). Todas las
lecturas del pipeline pasan por ahí, serializadas:

* S1: `is_stage_done` + `is_uploaded` por doc (`staged.py:711,714`).
* S2/S3/S4: `is_stage_done` por doc (`staged.py:813,898,972`).
* S5: `is_stage_done` por doc **con el slot del semáforo ya tomado**
  (`staged.py:1176`), desde hasta 50-100 worker threads.
* El TUI: `list_docs_for_batch` a 4 Hz con un chunk seleccionado en el
  tab DETAIL (`tui/app.py:33` → `data_provider.py:248`).

= **6 lecturas serializadas por documento** + el polling del TUI, todos
peleando por el mismo lock. SQLite en WAL mode permite lectores
concurrentes **con conexiones separadas** — el lock de aplicación existe
solo porque hay una única conexión compartida.

### B. No hay índice sobre `batch_id`

Los únicos índices son `(rvabrep_txn_num, batch_id)` (columna líder
txn) y el parcial de `S5_DONE` (`sqlite.py:94-103`). Toda query que
filtra **solo por `batch_id`** hace full table scan de `migration_log`:

* `list_docs_for_batch` (`:624`) — la del tab DETAIL, **4 veces por
  segundo**, sosteniendo el `_reader_lock`.
* `get_batch_details` (`:595`) — 3 queries.
* `list_txn_nums_for_batch` (`:564`) — el resume scope.
* `retry_failed` (`:647`), `uploaded_records` con filtro (`:505`).

Con cientos de miles de filas acumuladas entre corridas, cada tick del
TUI escanea la tabla completa mientras los workers esperan el lock.

### C. (Menor) `is_stage_done` re-arma el SQL en cada llamada

Los placeholders se generan con f-string por invocación
(`sqlite.py:551-558`) — hasta 12 por stage. El texto es idéntico cada
vez, pero armarlo por llamada es trabajo repetido en el camino más
caliente del store; precomputarlo por stage lo elimina y estabiliza el
statement cache de SQLite.

## Qué

### Requisitos

**REQ-001 — Índice `idx_migration_log_batch` sobre `(batch_id)`.**
Creado en `_create_schema` con `IF NOT EXISTS` — las DBs existentes lo
adquieren al reabrirse, sin migración. Las queries de B pasan de full
scan a index lookup.

**REQ-002 — Conexiones de lectura por thread.** Las lecturas puras
(`is_uploaded`, `is_stage_done`, `list_txn_nums_for_batch`,
`uploaded_records`, `list_batches`, `get_batch_details`,
`list_docs_for_batch`) usan una conexión SQLite **por thread** vía
`ThreadLocalConnectionPool` (106) — sin lock de aplicación. WAL
garantiza snapshot consistente por lectura; la poda del pool evita
acumular conexiones de threads muertos (los pools de S5 se reciclan por
chunk).

**REQ-003 — Las escrituras síncronas conservan su conexión + lock.**
`start_batch` y `retry_failed` escriben fuera de la cola del writer;
mantienen la conexión dedicada actual (`_sync_conn`) con su lock. Un
solo escritor a la vez contra WAL (el writer thread es el otro) — sin
cambio de semántica.

**REQ-004 — Semántica de visibilidad intacta.** El contrato
read-after-write no cambia: las escrituras encoladas se vuelven
visibles al commitear el writer thread (ventana ≤ 1 s / 500 items, como
hoy); `flush()` sigue garantizando visibilidad total. Los tests
existentes de `test_sqlite_tracking_store.py` pasan sin modificación.

**REQ-005 — SQL de `is_stage_done` precomputado.** El texto SQL por
stage se genera una vez a nivel módulo (dict `StageStatus → sql`), no
por llamada.

### Fuera de alcance

* Renderizar solo el tab activo del TUI y bajar la cadencia del DETAIL
  (hallazgo C del informe TUI) — va en el cambio de métricas/TUI.
* `synchronous=OFF` y la durabilidad ante corte de energía (P17) —
  decisión de producto separada.

## Escenarios

**E1 — Lecturas concurrentes sin lock global.**
Dado 16 threads haciendo `is_uploaded`/`is_stage_done` en paralelo
mientras el writer thread commitea escrituras,
cuando corren,
entonces ninguna lectura falla, cada thread usa su propia conexión y el
resultado post-`flush()` es consistente.

**E2 — El índice existe y las queries por batch lo usan.**
Dado un store recién abierto,
cuando se inspecciona el schema,
entonces `idx_migration_log_batch` existe y el query plan de
`list_docs_for_batch` usa el índice (no `SCAN migration_log`).

**E3 — DB preexistente adquiere el índice.**
Dado un archivo de tracking creado por una versión anterior (sin el
índice),
cuando se reabre con el store nuevo,
entonces el índice se crea y las queries lo usan.

**E4 — Read-after-write con flush.**
Dado un `mark_stage_done` encolado,
cuando se llama `flush()` y luego `is_stage_done` desde otro thread,
entonces devuelve True (paridad con el comportamiento actual).

**E5 — Threads reciclados no acumulan conexiones.**
Dado dos olas de threads lectores donde la primera muere,
cuando la segunda lee,
entonces las conexiones de la primera se cierran (poda 106).
