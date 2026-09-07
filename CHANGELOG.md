# Changelog

Todos los cambios notables de CMCourier están documentados acá.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), y este proyecto adhiere a [Semantic Versioning](https://semver.org/spec/v2.0.0.html) una vez que el código empiece a shippearse.

> **Fase pre-implementación**: mientras todavía no shippeó código, los releases se taguean en hitos significativos de documentación (ratificación de la constitución, decisiones arquitectónicas, consolidación del roadmap). Una vez que el primer cambio del MVP se mergee, el proyecto pasa a SemVer estándar.

---

## [Unreleased]

_Sin cambios pendientes de release — todo el trabajo está versionado
abajo. El roadmap post-MVP vive en `docs/roadmap/POST-MVP.md`._

---

## [0.109.0] — 2026-09-06 — **Consola: doctor granular, pipeline elegible y pestaña SYNC**

Respuesta a la primera ronda de uso real de la consola (seis preguntas
del operador): el doctor se puede correr de a un check, el pipeline se
elige desde el launcher sin editar el YAML, y el sync SQLite ↔ AS400
NIARVILOG dejó de ser sólo CLI. Specs 126-128, TDD estricto y revisión
antagonista.

### Added

- **Spec 126 — doctor por check individual.** El selector de `[4]`
  ofrece tres niveles: todos, por grupo, o **un check suelto**
  (`run_doctor(selected=<nombre>)`; `CHECK_NAMES` y `group_of()`
  publicados por `cli/doctor.py`). La ayuda `?` lista todos los checks
  con su grupo, generada desde `CHECK_NAMES` (nunca re-tipeada). El CLI
  `cmcourier doctor --check` también acepta el nombre de un check.
- **Spec 127 — selector de pipeline en el launcher.** `[5]` deja elegir
  `csv` / `rvabrep` / `local_scan` / `single_doc` con los parámetros de
  cada uno; es un **override de sesión** (`SessionOverrides.trigger`)
  que viaja por `apply_overrides`, así que la corrida, el resumen de
  config efectiva (`pipeline … (override|del YAML)`) y el **doctor**
  ven la misma config. Un trigger inválido bloquea el lanzamiento con
  el motivo; un `scan_path` vacío ya no pasa como `.` (gotcha de
  `Path("")`).
- **Spec 128 — pestaña `8·SYNC`** (`8` / `F8`). Estado (`s`),
  recuperar con **dry-run obligatorio** antes de `aplicar` (habilitado
  sólo tras simular el mismo `batch_id` con filas; confirm *danger*,
  `PRD` tipeado en producción) y resolver por `txn` (`as400 manda`
  read-only; `local manda` escribe en AS400 con confirmación). Todo en
  worker thread con salida acumulada. Si el YAML no habilita el sync o
  faltan credenciales AS400, la pestaña lo dice y se deshabilita.

### Changed

- **`cli/sync_ops.py`** concentra la lógica de `sync status | recover |
  resolve` sin click (`SyncOpError`, stores siempre cerrados);
  `cmcourier sync` delega en ella con los mismos mensajes y exit codes
  (2 config, 1 uso, 3 AS400).
- Guía `docs/how-to/probar-la-consola.md` actualizada (doctor
  granular, selector de pipeline, pestaña SYNC, ocho pestañas).
- `As400Recovery.close()` cierra el store AS400 y la fuente RVABREP que
  `build_as400_recovery` le inyecta; `niarvilog_columns_from_schema`
  pasa a ser API pública de `config/wiring.py`.

### Fixed

Ronda antagonista (Opus) sobre 126-128:

- `[1] INICIO` interpolaba los mensajes de conexión (`[IBM][…]`, JSON)
  en un `Static` con markup: el cuerpo ahora se arma como `Content`
  (texto plano para lo externo). Ojo: `textual.markup.escape` NO cubre
  tags en mayúscula y el parser sí los traga — por eso no se usa.
- `[5] CORRER` con el selector de pipeline (127) empujaba `Lanzar`
  fuera de la pantalla: `RunPane` ahora scrollea.
- `[8] SYNC`: la salida usa `Log` con altura fija (el `1fr` colapsaba a
  cero dentro del `TabPane`), scroll al final, tope de 200 líneas, y
  `cm_object_id` sólo visible con `local manda`.
- El worker del doctor y el de SYNC ya no tumban la consola ante una
  excepción inesperada (`exit_on_error`): reportan un `FAIL`.
- `doctor`: `sample_dry_run` construía el pipeline con process pool
  (sólo se apagaba en `atexit`) y dejaba el tracking store abierto; en
  la consola cada corrida sumaba procesos. Ahora corre in-process y
  cierra el store.
- `sync recover` dejaba abiertos el store AS400 y la fuente RVABREP;
  `sync status` abría un SQLite que no usaba.
- El check individual de `[2] CREDENCIALES` corría sobre el YAML crudo,
  no sobre la config efectiva con overrides.

---

## [0.108.0] — 2026-09-06 — **Consola de operación interactiva (`cmcourier console`)**

La TUI dejó de ser solo un monitor: ahora `cmcourier console --config X`
abre una consola de 7 pantallas donde el operador carga credenciales,
corre el doctor, ajusta parámetros, lanza y monitorea — todo con
teclado. Diseño congelado con dos pasadas de review UX adversarial e
implementado en 3 fases (specs 123-125).

### Added

- **Spec 123 — shell + credenciales + doctor.** Comando `console`;
  navegación de 7 tabs con bindings F1-F7 inmunes al foco; badge de
  entorno (campo nuevo `environment: staging|prd` en el YAML);
  credenciales de sesión SOLO en memoria (nunca al YAML) con TTL de
  45 min e interlock de 3 intentos contra el lockout del perfil AS400;
  doctor interactivo por grupos (los reales de `--check`) con estado
  *stale* cuando cambian credenciales u overrides.
- **Spec 124 — launcher + overrides + lock + auditoría.** Overrides de
  sesión con modelo draft/applied (un borrador sin guardar nunca llega
  a una corrida); launcher por `trigger.kind` con cadena de guardas
  (credenciales frescas → doctor → confirmación tipeada "PRD" →
  corridas `in_progress` en el tracking); lock de config sostenido
  durante la corrida; auditoría persistida por batch (operador,
  estación, hash de config, overrides, veredicto del doctor, outcome)
  vía migración idempotente de `migration_batch`.
- **Spec 125 — monitor + batches.** Monitor en vivo reusando los
  renderers de `tui/` (refresca solo el tab visible), con desglose de
  fallos por tipo y cuello de botella; cancelación con `x` (drain) que
  se queda en la consola para mostrar el resumen; BATCHES operable con
  `DataTable` navegable por teclado (detalle, retry con ruteo a
  reanudar, export) y columnas de auditoría.

### Fixed

- Consola: `Static` con `markup=False` para los mensajes de error de
  CMIS/AS400 (traen JSON con corchetes que Textual leería como markup);
  cambiar de tab suelta el foco de los `Input` (si no, la vista no
  mudaba); el timer del reloj tolera el teardown.
- `scripts/staging/register-model.sh`: la verificación final abortaba
  por un `SyntaxError` de quoting (heredoc en su lugar).

---

## [0.107.0] — 2026-09-04 — **Papel de lija: la auditoría queda saldada**

Tres specs con los micro-hallazgos restantes. Con esto, todos los
items accionables de la auditoría de rendimiento (críticos, altos,
medios y menores) están cerrados o declinados con motivo documentado.

### Changed

- **Spec 120 — higiene de respuestas del uploader.** `resp.text` se
  decodifica SOLO en el path de error (helper `_http_error`); el
  lookup de 409 pide únicamente `cmis:name` + `cmis:objectId` en
  formato succinct en vez de 5000 hijos con todas sus propiedades.
- **Spec 121 — se elimina `find_documents_batch` (código muerto).**
  El lookup batcheado de S1 no tenía callers de producción y su
  semántica (sin distinción not-found vs all-deleted) no matcheaba el
  contrato de `S1_FILTERED` — una trampa para quien lo "aprovechara".
  `indexing.batch_size` del YAML se conserva (sin efecto) para no
  romper configs existentes.
- **Spec 122 — micro-hallazgos.** `compute_fields_hash` memoizado (se
  computaba 2× por doc); los aliases de metadata se precomputan en el
  constructor (se reconstruían por doc); `decrement_queue_depth()`
  atómico (antes: dos locks + una dataclass por doc); dos carreras
  menores cerradas (`_chunks_state` en el path de error de prep,
  `_peak_qsize` del BUCKET tab); el productor N=2 chequea cancelación
  entre chunks; el warmup de conexiones usa el techo AIMD (paridad con
  streaming); `CmisUploader.current_timeout_s` reemplaza el acceso
  privado del TUI. Declinado con motivo: throttlear las publicaciones
  por-doc del streaming (riesgo de estados finales rancios por un
  ahorro de microsegundos).

---

## [0.106.0] — 2026-09-04 — **Hallazgos medios de la auditoría: backlog cerrado**

Cuatro specs que cierran el backlog de la auditoría de rendimiento: la
higiene del uploader, los N+1 restantes del sync AS400, y el fix
estructural del churn de threads.

### Fixed

- **Spec 116 — el `TokenBucket` acumulaba tokens sin techo.** Tras una
  pausa (fase de PREP larga), la primera ráfaga de uploads salía sin
  throttling — el límite de red se violaba justo cuando más importa.
  Cap de burst de 1 s de presupuesto, con drenaje en cuotas para
  pedidos mayores que el cap. De paso: `BandwidthLimiter.read` ahora
  cobra por los bytes REALMENTE leídos (las lecturas cortas y el EOF
  pagaban de más), y se eliminó un spin de sleeps infinitesimales por
  residuo de float.
- **Spec 116 — docstring sincerado del uploader.** El módulo prometía
  "creación recursiva de carpetas con cache en memoria" que nunca
  existió.

### Changed

- **Spec 116 — timeouts granulares de httpx.** El connect se capea a
  10 s (`min(10, timeout)`): un host caído falla en segundos, no en 5
  minutos. Read/write conservan el timeout configurado (y el ajuste en
  vivo del AIMD).
- **Spec 117 — reconciler periódico batcheado.** Una pasada de 3000
  items hacía ~9000 sentencias ODBC secuenciales (read + claim + mark
  por item). Ahora: UNA lectura batcheada (`IN` chunkeado, 113) +
  **un solo write guardado directo al estado terminal** por item
  (`UPDATE ... WHERE STSCOD='N'` / INSERT terminal; el rowcount /
  `IntegrityError` conservan la detección de race). ~3004 sentencias.
  El import de subidas ajenas también se batcheó.
- **Spec 118 — `sync recover` sin N+1.** El chequeo de existencia en
  NIARVILOG (un SELECT por doc `S5_DONE` — el único trabajo por doc en
  el caso común) pasa a la lectura batcheada: ceil(N/1000) queries.
- **Spec 119 — pools de threads de vida larga.** Cada stage de prep
  (S2/S3/S4) y cada chunk de S5 creaba y destruía su
  `ThreadPoolExecutor` — ~80 000 ciclos de spawn/join en una corrida de
  20M docs, y la causa raíz de la fuga de conexiones ODBC que 106
  mitigó desde el adapter. El `StagedPipeline` ahora es dueño de pools
  lazy persistentes (prep, S5, S5-heavy, S5-light) reutilizados por
  todos los chunks, con `shutdown_worker_pools()` en el `finally` de
  los tres orchestrators.

---

## [0.105.0] — 2026-09-04 — **Hallazgos altos de la auditoría de rendimiento**

Siete specs que atacan los hallazgos de prioridad alta: el trabajo de
coordinación quemando slots de upload, el TUI renderizando paneles que
nadie mira, el logging por-MiB del hot path, los N+1 de pandas y del
pre-flight AS400, el ruteo subóptimo de S4 y la inanición de lanes.

### Fixed

- **Spec 115 — lanes: restitución de capacidad.** Una lane drenada 15 s
  migraba su capacidad y quedaba en 1 **para siempre** con flujo
  continuo (la única vía de vuelta era que la otra lane se vaciara
  15 s). Ahora el controller restituye el split inicial apenas la lane
  drenada vuelve a reportar trabajo.
- **Spec 115 — dispatcher de streaming sin head-of-line blocking.** El
  `put` bloqueante a una cola de lane llena clavaba al dispatcher
  entero — consumers light ociosos con trabajo en el bucket. Ruteo
  no-bloqueante con overflow acotado por lane; el poison espera a los
  overflows (ningún item se pierde).

### Changed

- **Spec 109 — S5: el slot del semáforo cubre solo el upload.** El
  pre-flight de idempotencia (`is_stage_done` + `mark_stage_pending` +
  `try_claim` AS400) corre ANTES del `acquire`. Los skips (ya subido,
  claim perdido) ya no consumen presupuesto de concurrencia de upload.
- **Spec 110 — TUI: renderiza solo el tab activo.** Pre-110 los cinco
  tabs se formateaban en cada tick de 0.25 s — incluido DETAIL, que
  con chunk seleccionado disparaba una query SQL 4 veces por segundo
  para descartarse. Cambiar de tab o mover el cursor pinta al
  instante; el DETAIL activo refresca a 1 Hz.
- **Spec 111 — dieta de logging del hot path.** El threshold de
  `cmis_upload_progress` sube de 1 MiB a 8 MiB (8× menos json.dumps +
  escrituras a disco desde los workers de S5); los tres logs por
  consulta del document cache bajan a DEBUG. `stage_complete` queda a
  INFO — alimenta slow-ops y `diagnose`.
- **Spec 112 — `TabularDataSource` con lookups indexados.**
  `get_by_fields` pasaba una máscara booleana O(filas) por filtro, una
  vez POR DOCUMENTO (S1/S3/local-scan). Ahora: índice hash lazy por
  combinación de columnas (`groupby(...).indices`) → lookup O(1),
  semántica idéntica al scan. La fase PREP deja de ser O(docs × filas).
- **Spec 113 — pre-flight AS400 batcheado.** `preflight_sync` hacía un
  round-trip ODBC por txn del batch (1000 SELECTs secuenciales de puro
  arranque). Nuevo `read_states_by_txns` con `IN` chunkeado de a 1000:
  ceil(N/1000) queries. También se elimina la doble computación muerta
  de `sqlite_done`.
- **Spec 114 — `s4_smart_routing` default `true`.** Con el process pool
  activo por default (066), los PDF nativos (`shutil.copy2`) pagaban
  pickle/IPC/spawn sin necesidad. Opt-out con `false` en el YAML.

---

## [0.104.0] — 2026-09-04 — **Fixes críticos de la auditoría de rendimiento**

Cuatro specs que eliminan los cinco hallazgos críticos de la auditoría
de rendimiento end-to-end: un cuelgue de 300 s por documento en el
retry de 401, dos bombas de concurrencia ODBC, el lock global de
lecturas de SQLite y las métricas sin techo que dejaban ciego al AIMD.

### Fixed

- **Spec 105 — el retry de 401 no rebobinaba el stream.** Un 401 en el
  primer intento (sesión CMIS vencida) reintentaba con el file handle
  en EOF: `Content-Length` completo, file part vacío, y el server
  esperando el resto del body hasta el read timeout — **300 s de
  cuelgue por documento**, justo cuando expiran las sesiones de todos
  los workers a la vez. El rebobinado ahora cubre también el path de
  re-autenticación (que sigue sin consumir presupuesto de retries).
- **Spec 106 — `As400DataSource` compartía UNA conexión pyodbc entre
  threads sin lock.** pyodbc declara `threadsafety = 1` (conexiones no
  compartibles); con `prep_workers > 1` los cursores concurrentes de
  S1/S3 caían sobre la misma conexión. Ahora cada thread tiene la suya.
- **Spec 106 — fuga de conexiones ODBC en `As400NiarvilogStore`.** Los
  `ThreadPoolExecutor` de S5 se reciclan por chunk y las conexiones de
  los threads muertos quedaban abiertas hasta el final de la corrida —
  cientos de jobs QZDASOINIT acumulados en el iSeries. El pool nuevo
  las poda cuando un thread nuevo conecta.
- **Spec 108 — race en el dict de buckets de métricas.** Un stage nuevo
  registrándose durante un `stages_snapshot()` del TUI tiraba
  `RuntimeError: dictionary changed size during iteration`.

### Changed

- **Spec 106 — `ThreadLocalConnectionPool`**
  (`adapters/connection_pool.py`): helper compartido para el patrón
  conexión-por-thread con registro global, `reset_current()` para
  retries y poda de threads muertos. Lo adoptan `As400DataSource`,
  `As400NiarvilogStore` y el tracking store de SQLite.
- **Spec 107 — lecturas concurrentes del tracking store.** El
  `_reader_lock` global que serializaba **6 lecturas por documento**
  (más el polling del TUI a 4 Hz) se reemplaza por conexiones de
  lectura por thread — WAL permite lectores concurrentes con
  conexiones separadas. Índice nuevo `idx_migration_log_batch`: las
  queries que filtran solo por `batch_id` (tab DETAIL,
  `get_batch_details`, `retry_failed`, resume scope) pasan de full
  table scan a index lookup; las DBs existentes lo adquieren
  automáticamente al reabrirse. El SQL de `is_stage_done` se precomputa
  por stage.
- **Spec 108 — métricas de stage acotadas.** `_StageBucket` pasa de una
  lista sin techo (O(total_docs) por bucket en modo streaming — leak de
  memoria, y ordenada completa ~13 veces por segundo por el TUI
  sosteniendo el lock del hot path) a una **ventana deslizante** de
  2048 muestras con summary cacheado. Los percentiles reflejan el
  comportamiento reciente — el AIMD vuelve a reaccionar a degradaciones
  de CMIS en segundos en vez de quedar clavado en el p95 histórico.
  `count` y `sum_ms` siguen siendo acumulativos (`batch_summary`,
  `analyze` y `diagnose` no cambian de semántica).

---

## [0.103.0] — 2026-05-22 — **Banco de pruebas de stress: contenido sintético, deadline y desglose de errores**

Tres specs hermanos para correr el plan de stress contra el destino
CMIS sin materializar 13 millones de documentos / 20 TB de corpus, sin
estar de niñera durante un soak de 24 h, y con diagnóstico real cuando
el destino empieza a degradar.

### Added

- **Spec 102 — contenido sintético on-the-fly.**
  `services/mock/synthetic_content.py` genera un PDF de una página
  válido por documento, sin `img2pdf`/`PIL` — costo O(tamaño), nunca
  cuello de botella. Tamaño determinístico por `txn_num`, distribución
  configurable (default 60/30/10 alineado al plan de stress §3.3). Con
  `assembly.synthetic_content.enabled: true`, S4 genera en vez de leer
  el archivo fuente — el dir de staging puede ir sobre tmpfs / RAM
  disk para velocidad de memoria, sin tocar el disco físico del origen.
- **Spec 103 — control de tiempo de ejecución (`--max-duration`).**
  `DeadlineWatchdog` prende el `CancellationToken` existente (097) al
  vencer un plazo de pared — el mismo drain cooperativo, disparado por
  reloj. Acepta duraciones humanas (`30m`, `1h30m`, `90`) en los
  cuatro comandos `run`; funciona headless (caso T04, soak 24 h).
- **Spec 104 — desglose de la tasa de error de upload por tipo.**
  `observability/error_classification.py` clasifica cada falla de S5
  en `timeout` / `http_4xx` / `http_5xx` / `transport` / `app_error`,
  desenvolviendo `RetriesExhaustedError.__cause__`. El
  `MetricsRecorder` cuenta total + por tipo + por status HTTP exacto;
  el `BatchSummary` y el tab UPLOAD del TUI exponen el desglose en
  vivo (clave para T05: ver el instante en que arrancan los 503).

### Changed

- `MetricsRecorder.record_upload_failed` ahora recibe
  `(category, status_code)`. El `BatchSummary` lleva `failed_total` /
  `failures_by_type` / `failures_by_status` — derivable la tasa de
  error contra los umbrales del plan §6.1.
- `PdfAssembler.AssemblerConfig` acepta un `synthetic_provider`
  opcional; default `None` → comportamiento intacto para migraciones
  reales.
- `AssemblyConfig` (schema) gana un sub-bloque `synthetic_content`
  con `enabled` / `seed` / `size_mix`.

### Fixed

- 11 tests pre-existentes rotos por drift de specs previos (no eran
  regresiones de estos cambios — ya estaban rojos en `0.102.0`). Test
  doubles desactualizados tras 084, 093, 096; el port `IDataSource`
  creció `query` / `query_stream`. La suite completa pasa:
  **1626 / 1626**.

---

## [0.102.0] — 2026-05-21 — **Instalador offline para servidores air-gapped**

El script que arma el bundle de instalación offline (para el servidor
de migración del banco, sin internet) vivía solo en la máquina del
operador — nunca estuvo en el repo. Este cambio lo incorpora y le da
una contraparte Linux + documentación.

### Added

- **`installer/build-offline-bundle.ps1`** — arma un bundle offline
  para Windows Server x86_64 air-gapped (wheels + wheel del proyecto +
  config + `install.bat`).
- **`installer/build-offline-bundle.sh`** — la versión Linux
  (manylinux x86_64 + `install.sh`).
- **`docs/how-to/build-offline-installer.md`** — how-to del flujo de
  dos máquinas, con el gotcha de la versión de Python.
- **`scripts/publish-release.sh`** — arma el export bundle y lo
  publica como GitHub Release (tag `v<version>`) vía `gh`. El ZIP NO
  se commitea — los binarios no ensucian el historial de git.

### Changed

- `scripts/export-bundle.sh` ahora deja el ZIP en `releases/` con
  nombre versionado (`cmcourier-export-<version>.zip`).
- `.exportignore`: agrega `releases/`; `installer/` NO se excluye —
  el instalador es un entregable.
- `.gitignore`: ignora `dist-offline/` (output del instalador offline)
  y `releases/` (los bundles van a GitHub Releases, no al repo).

---

## [0.101.0] — 2026-05-21 — **Refresh de documentación: config + comandos al día**

Auditoría previa al handoff: la documentación de referencia estaba
desactualizada por ~48 versiones. `config-reference.yaml` declaraba
"version 0.52.0".

### Changed

- **`docs/reference/config-reference.yaml`** — al día contra
  `config/schema.py`. Agregados: `processing.mode`/`streaming` (063),
  `s4_use_processes`/`s4_max_processes`/`s4_smart_routing` (066/094),
  `cmis.http2`/`upload_chunk_bytes` (089/090), las perillas AIMD de
  068, `assembly.keep_staged_files` (085), `as400_sync.mode`/`periodic`
  (096), `local_scan.recursive` (088).
- **`docs/reference/cli.md`** — secciones nuevas `diagnose` (092) y
  `sync recover` (099).

### Notas

- Sin cambios de código. Diferido a un refresh futuro:
  `config-schema.md`, los tutoriales y los diagramas — ver
  `specs/100-docs-refresh/`.

---

## [0.100.0] — 2026-05-21 — **`cmcourier sync recover`: recuperar filas faltantes en NIARVILOG**

El bug 096 (arreglado en 098) dejó documentos subidos a CM y marcados
`S5_DONE` en SQLite, pero sin su fila en AS400 NIARVILOG. El 098 frenó
la pérdida futura; este cambio agrega la herramienta de remediación
para reparar lo ya perdido.

### Added

- **`cmcourier sync recover --config X [--apply] [--batch-id Y]`**:
  reconcilia SQLite → AS400 por txn. Para cada doc `S5_DONE` que
  NIARVILOG no tiene, re-deriva los campos que SQLite no almacena
  (`DOCFRM`/`IMGTIP` desde la fila RVABREP vía `IndexingService`,
  `IDNBAC`/`TIPIDN` desde el mapping) e inserta la fila terminal.
  **Dry-run por defecto** — `--apply` ejecuta los INSERT.
- **`As400Recovery`** (`services/recovery.py`): el motor de
  recuperación. Recorrido **resiliente por-txn** (lección del 098) —
  un txn que falla se reporta como `unrecoverable`, no aborta el resto.
- **`SQLiteTrackingStore.uploaded_records`**: enumera los docs
  `S5_DONE` con los campos que la recuperación necesita.
- **`As400NiarvilogStore.insert_recovered_row`**: INSERT directo de
  una fila terminal `STSCOD='O'`.
- **`IndexingService.find_document_by_txn`**: busca la fila RVABREP de
  un txn y la convierte a `RVABREPDocument`.

### Notas

- Solo recupera la dirección SQLite → AS400 para docs `S5_DONE`.
- Idempotente: re-correr `recover --apply` saltea las filas ya
  presentes. Un txn sin fila RVABREP o con id RVI no mapeado se
  reporta como `unrecoverable` — nunca se inserta a ciegas.
- ~10 tests nuevos (`test_recovery.py`, `uploaded_records`).
- Spec: `specs/099-as400-recovery-command/`.

---

## [0.99.1] — 2026-05-20 — **Fix de pérdida de datos en el reconciliador periódico**

Reporte del operador: una corrida en `mode: periodic` procesó ~4000
documentos pero AS400 NIARVILOG quedó con solo ~555 registros. Pérdida
de datos real — dos bugs del cambio 096 que se combinaban en
`services/reconciler.py`:

- **`run_pass` abortaba el batch entero ante una falla per-ítem**: el
  loop llamaba `_reconcile_item` sin `try/except`, así que un error de
  AS400 en un solo doc desenrollaba toda la pasada.
- **`run_one` drenaba el buffer ANTES de procesar**: si `run_pass`
  explotaba a mitad, los ítems ya drenados no volvían al buffer — se
  perdían en silencio.

### Fixed

- **`run_pass` resiliente**: `cleanup_stale_in_progress`, cada
  `_reconcile_item` y `_import_foreign_uploads` van envueltos en
  `try/except`. La pasada **nunca levanta excepción** y procesa
  **todos** los ítems.
- **Re-encolado de fallidos**: los ítems que fallan vuelven al buffer
  (`ReconcileResult.requeued`) para reintento en la próxima pasada —
  nunca se pierden en silencio.
- **Corte cooperativo** (`run_pass(..., stop_event=)`): el daemon
  corta entre ítems al pararse y re-encola el resto; la pasada final
  corre completa en el thread no-daemon, a salvo del cierre del proceso.
- **Observabilidad**: `reconcile_pass` ahora logea `failed` / `requeued`;
  un `reconcile_failure` por ítem fallido con su txn y error.

### Mitigación / recuperación

- Mientras no se despliegue: usar `tracking.as400_sync.mode: claim`
  (sincronización por-documento, más lenta pero sin pérdida).
- Los ~3445 registros perdidos: los documentos están en CMIS y en
  SQLite (`S5_DONE`) — solo faltan las filas de NIARVILOG. La
  recuperación se planifica como cambio separado (requiere re-derivar
  `DOCFRM`/`IMGTIP`/`IDNBAC`/`TIPIDN`, que SQLite no almacena).

### Notas

- ~5 tests nuevos en `tests/unit/services/test_reconciler.py`.
- Spec: `specs/098-periodic-reconciler-data-loss/`.

---

## [0.99.0] — 2026-05-20 — **Cancelación cooperativa del pipeline desde el TUI**

El operador reportó: al apretar **"q"** con la corrida en progreso, el
TUI se cerraba pero el pipeline seguía corriendo; solo Ctrl+C repetido
lo frenaba. Causa raíz (`cli/_tui_runner.py`): el pipeline corre en un
worker thread `daemon=False` y, al cerrar el TUI, el `worker.join()`
bloquea esperando que termine **solo** — no existía ningún mecanismo
de cancelación (`rg cancel|abort|stop_event` en `orchestrators/` daba
cero resultados).

### Added

- **`CancellationToken`** (`services/cancellation.py`): flag
  thread-safe de cancelación cooperativa sobre `threading.Event`.
- **Chequeos cooperativos** en `StagedPipeline`: el loop de triggers
  de S0/S1 y los métodos per-doc (`_s2_one`/`_s3_one`/`_s4_one`/
  `_upload_one`) saltean el trabajo cuando el token está prendido —
  *drain*: dejan de tomar trabajo nuevo, lo que está en vuelo termina.
- **`StreamingOrchestrator` / `MultiBatchOrchestrator`** exponen
  `cancel_token` y cortan sus loops propios (producer / chunk loop).
- **`ConfirmCancelScreen`** (`tui/confirm_screen.py`): modal sí/no.
  `"q"` con la corrida en progreso lo abre; `s` confirma y prende el
  token, `n`/`escape` la descarta. `"q"` con la corrida completa sale
  directo (pre-097).

### Changed

- `cli/_tui_runner.py` comparte el `cancel_token` del orchestrator con
  el `CMCourierTUI`. Al confirmar la cancelación, el `worker.join()`
  espera solo el drain (segundos) en vez de la corrida entera.

### Notas

- **Backward-compat**: sin cancelar, el comportamiento es
  byte-idéntico a pre-097. El token se crea siempre; headless nunca se
  prende.
- Ctrl+C **no** cambia — sigue siendo el abort de emergencia. 097
  arregla la `"q"`, que ahora es una salida real y ordenada.
- ~17 tests nuevos (`test_cancellation.py`,
  `test_staged_cancellation.py`, `test_quit_confirmation.py`).
- Spec: `specs/097-tui-cooperative-cancellation/`.

---

## [0.98.0] — 2026-05-20 — **Modo de sincronización periódico para AS400**

El sync AS400 del cambio 034 es **por-documento y sincrónico**: cada
doc en S5 hace `try_claim` antes y `mark_uploaded`/`mark_failed`
después — round-trips a DB2-for-i en el critical path del upload. Para
despliegues **sin migrador competidor**, ese claim atómico distribuido
es overhead que no compra nada. Este cambio agrega un modo de sync
alternativo, opt-in, que saca AS400 del hot path.

### Added

- **`tracking.as400_sync.mode`** (`claim` | `periodic`, default
  `claim`) + **`PeriodicSyncConfig.interval_minutes`** (schema).
- **Modo `periodic`**: S5 escribe solo SQLite y encola el contexto
  del doc en un `PendingSyncBuffer`. Un reconciliador de fondo
  (`As400Reconciler` + daemon `PeriodicReconciler`) propaga a AS400
  cada `interval_minutes`, más una pasada final al terminar la corrida.
- **Tres categorías de reconciliación**: `synced_to_as400` (doc local
  propagado), `synced_to_local` (fila `O` ajena importada a SQLite vía
  `SQLiteTrackingStore.record_external_upload`), `conflict` (estado
  divergente en las dos bases → resolución manual).
- **Log nuevo `reconcile-{date}.jsonl`** (`cmcourier.metrics.reconcile`):
  un evento `reconcile_pass` por pasada + un `reconcile_conflict` por
  cada conflicto, con el `hint` de qué `sync resolve` correr.
- Integración del daemon en los tres orchestrators (`StagedPipeline`,
  `StreamingOrchestrator`, `MultiBatchOrchestrator`).

### ⚠️ Tradeoff

El modo `periodic` **sacrifica la prevención de doble-upload**. El
claim atómico por-documento del modo `claim` existe para que dos
sistemas no suban el mismo doc; en `periodic` los conflictos se
detectan **post-hoc** vía el log, no se previenen. El default sigue
siendo `claim`.

### Notas

- **Backward-compat total**: `mode: claim` (default) es byte-idéntico
  a pre-096.
- Diferido: el CLI `sync reconcile` standalone — un proceso CLI no
  tiene el `PendingSyncBuffer` in-process. Se planifica por separado.
- ~28 tests nuevos (`test_reconciler.py`, schema, coordinator,
  observability).
- Spec: `specs/096-as400-periodic-sync-mode/`.

---

## [0.97.0] — 2026-05-20 — **Connection pool por worker para el sync AS400**

Con `tracking.as400_sync.enabled=true` el operador reportó una caída
substancial de la velocidad de carga. El rastreo confirmó la causa:
`As400NiarvilogStore` cacheaba **una única conexión `pyodbc`**
compartida por todos los worker threads de S5. Una conexión ODBC
serializa statements, así que los 2-3 round-trips a NIARVILOG por
documento (`try_claim` + `mark_uploaded`) de los N workers se
encolaban en fila india — el paralelismo de S5 quedaba anulado para
la porción AS400.

### Changed

- **`As400NiarvilogStore`** ahora usa **conexiones thread-local**:
  cada worker thread abre y cachea la suya (`threading.local`). Un
  registro interno (`_all_conns` + lock) permite que `close()` cierre
  todas. `_reset_connection` (retry) resetea solo la conexión del
  thread que reintenta.

### Notas

- **Backward-compat total**: el comportamiento single-thread es
  byte-idéntico a pre-095 (mismo SQL, mismo orden, mismo retry). Sin
  cambios de schema ni de wiring — el pool se dimensiona solo a la
  cantidad de workers de S5.
- **Riesgo operacional**: el banco pasa de ver 1 sola conexión de
  CMCourier a ver hasta `s5_max_workers`. Confirmar que el perfil de
  usuario AS400 admite ese número de sesiones concurrentes.
- **4 tests nuevos** en
  `tests/integration/adapters/test_as400_niarvilog_pool.py`.
- Spec: `specs/095-as400-sync-connection-pool/`.

---

## [0.96.0] — 2026-05-19 — **Smart routing PDF/TIFF en S4 (evita overhead del ProcessPool en Windows)**

Datos reales del operador productivo (Windows, 200 docs mixtos)
mostraron un GAP de **5808s** entre el tiempo total de S4 y la suma
de sus sub-stages. Análisis: ese gap es overhead del
`ProcessPoolExecutor` con `spawn` (pickle + IPC + spawn cold-start
+ queue) — ~29s por doc.

**Los PDF nativos** son `shutil.copy2` puro: libera el GIL durante
I/O y NO necesita ProcessPool. **Los TIFF/JPEG** sí lo necesitan
(`img2pdf` es CPU bound). Pre-094 todos iban al pool sin distinguir.

### Added

- **`ProcessingConfig.s4_smart_routing: bool = False`** (schema):
  flag opt-in. Cuando `True` Y el process pool está activo, los
  docs con `document.is_pdf == True` corren inline en el thread
  del prep_workers; los paginados siguen yendo al process pool.
- **`StagedPipeline.s4_smart_routing`** param: wired desde el
  schema.

### Uso

```yaml
processing:
  prep_workers: 40             # threads para inline path
  s4_use_processes: true       # process pool activo
  s4_smart_routing: true       # ← 094 — evita pool para PDFs nativos
```

### Ganancia esperable

Para el workload medido del operador (114 PDFs nativos × 29s
overhead/doc) → **~3300s ahorrados** = **4-7× speedup** del wall
time de prep. Combinado con la exclusión de AV del source/temp,
el speedup compuesto puede llegar a 10-15×.

### Notas

- **Backward-compat total**: default `False` preserva pre-094
  byte-idéntico.
- El benchmark sintético en Linux (spec 091) mostraba ProcessPool
  ganando para Small files. En Windows productivo con SMB + AV el
  overhead del `spawn` invierte la conclusión para PDFs nativos.
  **Datos empíricos del operador manda.**
- **6 tests nuevos** en
  `tests/unit/orchestrators/test_s4_smart_routing.py`.
- Pareja con spec 093 (sub-stage metrics que confirmaron el GAP).

---

## [0.95.0] — 2026-05-19 — **Métricas sub-stage en S4 (granularidad para diagnose)**

Operador productivo con prep lento. `cmcourier diagnose` (spec 092)
puede decir "S4 es el cuello", pero NO dónde dentro de S4 se va el
tiempo. S4 hace 4 cosas distintas: source_stat, copy_native (o
encode_pdf), discover_pages, dst_stat.

### Added

- **`AssemblyTimings`** dataclass en `pdf_assembler.py` con campos
  opcionales por sub-stage + `path_kind`.
- **`PdfAssembler.assemble_traced(doc)`**: retorna
  `tuple[StagedFile, AssemblyTimings]`. Mide cada sub-paso con
  `time.perf_counter()`.
- **`_pool_assemble_traced`** en `pool.py`: análogo para el
  ProcessPool, devuelve los timings vía pickle al proceso padre.
- **`StagedPipeline._record_s4_substages`** (helper estático):
  registra cada timing > 0 como `S4.<sub>` en el
  `MetricsRecorder`. Campos en 0 (camino no tomado) se skipean.

### Changed

- **`StagedPipeline._stage_s4_one`** ahora usa el path traced.
  Cero cambio funcional aparte del logging adicional de timings.

### Notas

- **Backward-compat total**: `assemble(doc)` sigue retornando solo
  `StagedFile`. El método nuevo es `assemble_traced(doc)`.
- **Output de `cmcourier diagnose --latest` post-093**: además de
  S1-S5, muestra buckets `S4.copy_native`, `S4.source_stat`,
  `S4.encode_pdf`, etc. con sus propios %, p50, p95. Permite
  identificar EXACTAMENTE dónde dentro de S4 se va el tiempo
  (disco source, CPU del PDF assembly, o disco temp).
- **8 tests nuevos** entre
  `tests/unit/adapters/assembly/test_pdf_assembler_traced.py` y
  `tests/unit/orchestrators/test_s4_substage_metrics.py`.

---

## [0.94.0] — 2026-05-19 — **Nuevo comando `cmcourier diagnose` (análisis de bottlenecks reproducible)**

Operador productivo reportó prep lento (350s para 200 docs pesados,
~1.75s/doc). Pedidos ad-hoc en PowerShell para extraer métricas de
los `metrics-*.jsonl` fallaban silenciosamente. **Necesitamos
diagnóstico versionado, no shell improvisado.**

### Added

- **`cmcourier diagnose --config <yaml> --latest`**: lee
  `observability.log_dir/metrics-*.jsonl`, parsea los eventos
  `batch_summary`, calcula tabla por stage (count / avg / p50 / p95
  / sum / %) y detecta el cuello (stage con > 50% del wall).
- **`--batch <id>`**: analiza un batch específico.
- **`--list`**: lista todos los batches disponibles.
- **Sugerencias contextuales por patrón**: S4 con alta latencia →
  sospechas de disco. S5 con alta latencia → sospechas de CMIS
  server. Cada sugerencia incluye comandos PowerShell concretos
  para verificar.

### Design notes

- **Fail-loud**: si no hay logs o batch_id no existe, sale con
  código != 0 + mensaje explicativo. Sin `-ErrorAction
  SilentlyContinue`.
- **No depende de SQLite**: solo lee los JSONL. Sirve aunque la
  tracking DB se haya perdido.
- **Read-only**: cero modificación de estado.

### Notas

- **Pareja con spec 093 (próxima)**: métricas sub-stage en S4.
  Cuando shippee 093, el comando muestra el detalle interno
  (s4_open_source, s4_read_source, s4_assemble_pdf,
  s4_write_staged) sin requerir cambios adicionales.
- **15 tests nuevos** en
  `tests/unit/cli/commands/test_diagnose.py`.

---

## [0.93.0] — 2026-05-18 — **Script de benchmark para S4 pool comparison (POC sin cambio de comportamiento)**

Operador reportó que S4 era lento con archivos chicos y sospechó
que el ProcessPool no daba paralelismo real. En vez de aceptar la
intuición o rechazarla, **medimos**. Spec 091 agrega un script de
benchmark al repo.

### Added

- **`scripts/bench-s4-pool-comparison.py`**: script standalone que
  compara Serial vs ThreadPool vs ProcessPool sobre 4 workloads
  sintéticos (PDFs chicos, PDFs grandes, TIFFs paginados, mix
  realista). CLI: `--docs N --workers N --runs N --workload {small|large|tiff|mixed|all}`.

### Resultados de referencia (Linux, 8 cores físicos)

```
Workload          | Serial      | ThreadPool  | ProcessPool | Mejor
Small (PDF 100KB) | 1494 docs/s |  470 docs/s |  513 docs/s | Serial
Large (PDF 5MB)   |   78 docs/s |  224 docs/s |  283 docs/s | ProcessPool 1.7×
TIFF (3 pages)    |   77 docs/s |   69 docs/s |  283 docs/s | ProcessPool 4.1×
Mixed (real)      |  295 docs/s |  289 docs/s |  425 docs/s | ProcessPool 1.5×
```

**Conclusión basada en datos**: el default actual
(`processing.s4_use_processes: true`) gana en 3 de 4 workloads
productivos. **NO se cambia comportamiento**. El operador puede
opt-out con el toggle existente si su mix es mayormente Small.

### Notas

- **Cero cambios en código productivo**. Es una herramienta
  manual en `scripts/`.
- En Windows el `spawn` es más caro que `fork` de Linux; los
  números pueden diferir. El operador debe medir en su ambiente
  antes de tomar decisiones.
- Si en Windows el ProcessPool pierde para Small Y el workload es
  mayormente Small, una spec futura podría rutear por tipo/tamaño:
  ThreadPool inline para PDF nativo chico, ProcessPool para
  TIFF/grande. **NO se implementa speculativamente hoy.**

---

## [0.92.0] — 2026-05-18 — **`cmis.upload_chunk_bytes` configurable (fix GIL contention en uploads paralelos)**

Bug crítico de throughput. Operador reportó: 30 workers paralelos
subiendo archivos >50 MB topaban en 20 MB/s agregado, mientras
que `curl` con UN archivo grande satura el link a 100 MB/s. El
fix de HTTP/2→HTTP/1.1 (spec 089) no movió la aguja. El cuello
estaba dentro del proceso Python.

### Causa raíz

El uploader llamaba a ``enc.read(8192)`` literal — chunks de 8 KB
sobre el MultipartEncoder. Para un archivo de 50 MB son 6400 reads
por archivo. Cada read corre código Python (format multipart
wire, copy buffers, progress callback). El **GIL serializa** ese
trabajo entre los 30 threads workers, generando ~192,000
GIL acquisitions por batch.

Inconsistencia: el ``BandwidthLimiter`` (path alterno) ya usaba
1 MiB chunks por default. Solo el path normal del MultipartEncoder
quedó en 8 KB hardcoded.

### Changed

- **`cmis.upload_chunk_bytes: int`** (schema, default `1 << 20`
  = 1 MiB). Rango `[4 KiB, 64 MiB]`. Configurable para que el
  operador suba el chunk para archivos gigantes o lo baje si
  tiene memoria limitada.
- **`CmisConfig.upload_chunk_bytes: int = 1 << 20`** (dataclass
  del adapter): mismo campo, propagado por wiring.
- **`CmisUploader._read_chunk`**: reemplaza el literal `8192` por
  `self._cfg.upload_chunk_bytes`.

### Uso

```yaml
cmis:
  workers: 30
  upload_chunk_bytes: 1048576       # 1 MiB default — basta para casi todo
  # upload_chunk_bytes: 4194304     # 4 MiB para archivos > 100 MB
```

### Notas

- **Backward-compat**: configs sin override ahora usan 1 MiB en
  vez de 8 KiB. **Estrictamente mejor** — no hay tradeoff
  operacional. Speedup esperado: 3x-5x en escenarios de muchos
  workers + archivos grandes.
- **Memoria**: 30 workers × 1 MiB = 30 MiB de buffers. Despreciable.
  Con 4 MiB: 120 MiB, todavía razonable.
- **5 tests nuevos** en
  `tests/unit/adapters/upload/test_upload_chunk_bytes.py`.
- Pareja con 089: ambas atacan el mismo síntoma (throughput
  agregado bajo con muchos workers) desde lados distintos. 089:
  TCP exclusivas. 090: menos GIL contention. Aplicar ambas si una
  sola no alcanza.

---

## [0.91.0] — 2026-05-18 — **`cmis.http2` toggle (opt-out de HTTP/2 multiplexing)**

Bug productivo. Operador con link de 1 Gbps subiendo archivos
> 50 MB con 30 workers se topaba en **20 MB/s agregado**, mientras
que `curl` con UN archivo grande satura el link. CPU OK (workers
busy), no es prep ni server-side per-doc.

### Causa raíz

CMCourier abría `httpx.Client` con `http2=True` hardcoded desde
spec 060. httpx negocia HTTP/2 y reusa pocas conexiones TCP
multiplexando muchos streams. El server CMIS, con
`SETTINGS_INITIAL_WINDOW_SIZE` chico (default RFC: 64 KB/stream),
serializa el throughput agregado de los 30 streams concurrentes.

HTTP/1.1: cada worker mantiene su propia TCP exclusiva (hasta
`max_keepalive_connections`), flow window independiente, escala
con N workers.

### Added

- **`cmis.http2: bool = True`** (schema): flag opt-out. Default
  `True` preserva spec 060.
- **`CmisConfig.http2: bool = True`** (dataclass): mismo flag.
- **`CmisUploader.__init__`** pasa `http2=config.http2` al
  `httpx.Client` en vez del literal `True` hardcoded.

### Uso

```yaml
cmis:
  workers: 30
  http2: false                  # ← fuerza HTTP/1.1
```

### Notas

- **Backward-compat total**: configs pre-089 cargan idénticamente.
- httpx con `http2=False` solo habla HTTP/1.1. Con `True` negocia
  vía ALPN y cae a 1.1 si el server no anuncia h2 — comportamiento
  preservado.
- **4 tests nuevos** en
  `tests/unit/adapters/upload/test_http2_toggle.py`.
- Pareja conceptual con 088: 088 mejora la entrada (descubrir
  archivos en subdirs), 089 mejora la salida (no serializar
  uploads paralelos).

---

## [0.90.0] — 2026-05-18 — **`local_scan` con flag `recursive`**

El operador descubrió que el modo `local_scan` solo listaba el
directorio raíz del `scan_path` — archivos bajo subdirectorios se
ignoraban silenciosamente. El árbol natural del RVI es
`source_root/<ABAICD>/<ABAJCD>/file.PDF`, así que apuntar a un raíz
alto no procesaba nada.

### Added

- **`LocalScanTriggerConfig.recursive: bool = False`** (schema):
  flag opt-in para descender por todos los subdirectorios. Default
  `False` preserva el contrato pre-088.
- **`LocalScanTriggerStrategy.__init__`** acepta `recursive: bool`.
  Cuando `True`, usa `Path.rglob("*")` en vez de `Path.iterdir()`.
  Los filtros de filename (`*.PDF` y `*.001`) y el lookup contra
  RVABREP no cambian.

### Uso

```yaml
trigger:
  kind: local_scan
  scan_path: C:\rvi-sources                # raíz del árbol RVI
  recursive: true                          # ← nuevo
```

### Notas

- **Backward-compat total**: configs pre-088 cargan idénticamente.
- **Symlinks circulares**: `rglob` sigue symlinks por default en
  Python 3.11/3.12. Edge case raro en Windows productivo — no
  mitigado en 088.
- **Performance**: `rglob` itera todo el árbol antes de filtrar.
  Para árboles muy grandes, preferí `as400` trigger con `query`
  filtrado.
- **6 tests nuevos** en
  `tests/unit/services/test_local_scan_recursive.py`.

---

## [0.89.0] — 2026-05-18 — **Adapter NIARVILOG conecta con `CommitMode=0` (sin commit control)**

Bug productivo descubierto activando el sync AS400 contra una tabla
NIARVILOG no journaled. Síntoma:

```
SQL7008 - RVIMGLOG in LIBHJJ not valid for operation
```

### Causa raíz

El IBM i Access ODBC Driver, por default, abre la conexión con
commitment control `*CHG` (commit on change). DB2 for i exige
**journaling activo** sobre la tabla para escribir bajo `*CHG`. Si
la tabla no está journaled → SQL7008 al primer UPDATE/INSERT.

CMCourier no necesita commitment control: cada operación
(`try_claim`, `mark_uploaded`, `mark_failed`, etc.) es una sola
sentencia atómica. El claim atómico viene del predicado
`WHERE STSCOD='N'` del UPDATE — no de la transacción.

### Fixed

- **`As400NiarvilogStore._build_connection_string`**: agrega
  `CommitMode=0;` al connection string. `0` = `*NONE`. El
  `conn.commit()` posterior queda como no-op inofensivo.
- **Adapter `adapters/sources/as400.py` NO se toca** en 087. Solo
  el NIARVILOG (que escribe). Si surge SQL7008 contra el RVABREP
  source en otro ambiente, se replica el fix análogo en otra spec.

### Notas

- **Backward-compat total**: tablas journaled siguen funcionando
  (no se aprovecha el journal, pero tampoco se requiere). Tablas
  no journaled pasan de roto a funcional.
- **Atomicidad preservada**: dos procesos haciendo claim simultáneo
  siguen viendo uno ganar (1 row affected) y el otro perder
  (0 rows affected) gracias al row-locking de DB2 independiente del
  commit control.
- **2 tests nuevos** en
  `tests/unit/adapters/tracking/test_niarvilog_connection_string.py`.
- Pareja con 086 (wiring del CLI sync). 086 dejó que el override de
  columnas llegara al adapter; 087 hace que el adapter pueda
  ejecutar contra cualquier tabla NIARVILOG (journaled o no).

---

## [0.88.0] — 2026-05-18 — **`cmcourier sync` honra `tracking.as400_sync.columns`**

Bug productivo descubierto activando el sync AS400. El operador
configuró overrides de columnas en el YAML porque su tabla NIARVILOG
no tiene los nombres canónicos. `batch run` los respetaba; el CLI
`sync status` / `sync resolve` los **ignoraba silenciosamente** y
usaba los defaults hardcodeados.

Síntoma:

```
AS400 error: NIARVILOG niarvilog_cleanup_stale failed:
SQL0206 - Column or global variable FINREI not found
```

### Fixed

- **`sync.py:_load_stores`**: pasa
  `columns=_niarvilog_columns_from_schema(sync_cfg.columns)` al
  constructor de `As400NiarvilogStore` — alinea con el path que ya
  funcionaba en `wiring.py`. Pre-086 el adapter caía a sus defaults
  canónicos (`FINREI`, `PMRREI`, …) ignorando el YAML.

### Notas

- **Backward-compat total**: operadores sin override en el YAML
  siguen viendo el comportamiento previo. Operadores con override
  pasan de roto a funcional.
- **No fixea** el bug semántico de `cleanup_stale_in_progress`
  usando `finished_at` (FINREI) en vez de `started_at` (PMRREI) —
  las filas en `STSCOD='I'` no tienen `finished_at` por
  definición. Eso queda para spec 087 si surge en producción.
- **1 test nuevo** en
  `tests/unit/cli/commands/test_sync_honors_columns_override.py`.

---

## [0.87.0] — 2026-05-18 — **Cleanup de staged files post-S5_DONE**

Bug productivo descubierto por el operador: "los archivos no se
están borrando luego de que se cargan a CMIS". Verificado con `rg`:
**cero llamadas a `unlink` en el orchestrator** pre-085. Los PDFs
ensamblados en `temp_dir` quedaban en disco para siempre.

Implicancia: un lote de 100k docs × ~500KB ≈ 50GB de basura
acumulada. El operador tenía que limpiar a mano.

### Added

- **`StagedPipeline._cleanup_staged_file`**: borra `staged.path`
  con `unlink(missing_ok=True)` después de un `mark_uploaded` /
  `mark_stage_done` exitoso. Idempotente. `OSError` se loguea como
  warning pero no propaga — un cleanup roto no debe revertir un
  `S5_DONE` ya persistido.
- **`AssemblyConfig.keep_staged_files: bool = False`** (schema):
  opt-out para preservar `temp_dir/{txn_num}.pdf` post-upload
  cuando el operador necesita inspeccionar el ensamblado para debug.
- **`StagedPipeline.__init__`**: parámetro keyword-only nuevo
  `keep_staged_files: bool = False`. Wired desde `wiring.py`.

### Notas

- **Solo se borran los staged en `temp_dir`** — `source_root` (los
  archivos del RVI bajo `ABAICD/ABAJCD/`) **NO se tocan**. La fuente
  del banco es read-only para CMCourier.
- **Retry-safe**: si un S5 falla y se hace `cmcourier batch retry`,
  S4 regenera el staged desde source. El cleanup no invalida retry.
- **Migración**: si tu deploy actual asume que `temp_dir` persiste
  los staged (raro, pero posible), poné `assembly.keep_staged_files:
  true` en el YAML para preservar el comportamiento pre-085.
- **Tests nuevos** en
  `tests/unit/orchestrators/test_staged_cleanup_after_upload.py`.

---

## [0.86.0] — 2026-05-18 — **AS400 metadata source activo + `lookup_value_source` configurable**

Descubierto en producción enriqueciendo metadata desde AS400. Pre-084
dos problemas:

1. **`source_type: "as400:<alias>"` tiraba `NotImplementedError`**.
   El schema y el wiring ya registraban el adapter, pero el fetch
   en runtime nunca se cableó.
2. **Lookups CSV/AS400 estaban hardcoded contra el CIF del trigger**.
   No había manera de buscar por otra clave (ej. `rvabrep.txn_num`
   contra una tabla de operaciones, o `trigger.shortname` contra una
   tabla de regiones).

### Added

- **`as400:<alias>` source type** funcional. El `MetadataService`
  ahora resuelve fields contra `As400DataSource` con el mismo
  contrato que CSV (prefetch al arranque, lookup O(1) en runtime).
- **`FieldSourceItem.lookup_value_source`** (schema) — string con
  formato `<scope>.<attr>`. Default `"trigger.cif"` preserva 100% el
  comportamiento pre-084. Valores admitidos:
  - `"trigger.cif"` / `"trigger.shortname"` / `"trigger.system_id"`
    / `"trigger.<otro_attr>"` (lee atributo o `audit_row`)
  - `"rvabrep.<col>"` (cualquier campo del `RVABREPDocument`:
    `txn_num`, `index1..7`, `image_type`, etc.)
- **`SourceConfig.lookup_value_source`** (dataclass del service) —
  mismo campo, wired desde el schema.

### Changed

- **`MetadataService._fetch_csv` → `_fetch_lookup`** (unificado).
  Maneja CSV y AS400 indistintamente — el port `IDataSource` ya
  unifica el contrato. Nuevo helper privado `_resolve_lookup_value`
  parsea `lookup_value_source`.
- **`MetadataService._prefetch_csv_sources`**: extendido para
  pre-cachear sources AS400 además de CSV. Nombre conservado por
  backward-compat de tests.

### Uso

```yaml
metadata:
  sources:
    clientes_as400:
      kind: as400
      as400_connection:
        host: as400.banco.com
        port: 446
        database: PRODLIB
      query: "SELECT CIF, NOMBRE FROM PRODLIB.CLIENTES WHERE ABACST <> 'D'"

  field_sources:
    # Lookup AS400 por CIF (default lookup_value_source)
    BAC_Nombre_Cliente:
      sources:
        - source_type: "as400:clientes_as400"
          lookup_key_column: "CIF"
          lookup_value_column: "NOMBRE"

    # Lookup por otra clave del RVABREP
    BAC_Descripcion_Operacion:
      sources:
        - source_type: "as400:operaciones"
          lookup_key_column: "TXN"
          lookup_value_column: "DESC"
          lookup_value_source: "rvabrep.txn_num"
```

### Notas

- **Backward-compat total**: configs pre-084 cargan idénticamente.
  `lookup_value_source` tiene default `"trigger.cif"` que es
  exactamente el comportamiento hardcoded pre-084.
- **Memoria**: el prefetch lee TODA la tabla AS400 al arranque. Para
  datasets gigantes, el operador debe usar `query` con `WHERE` para
  acotar.
- **Tests nuevos**:
  - `tests/unit/config/test_field_source_lookup_value_source.py`
  - `tests/unit/services/test_metadata_as400_lookup.py`
- Pareja con spec 083 (`field_sources` solo con `default_value`).

---

## [0.85.0] — 2026-05-18 — **`field_sources` admite solo `default_value` (constantes hardcodeadas)**

Mejora descubierta configurando metadata productiva. Algunas propiedades
CMIS son **constantes por operador** (banco emisor, tipo de canal,
versión del modelo documental) — no salen de ningún source. Pre-083
el schema exigía ``sources`` con ``min_length=1``, forzando al
operador a inventar un source dummy garantizado a fallar para que el
motor cayera al ``default_value``.

### Changed

- **`FieldConfig.sources`**: `Field(min_length=1)` → `Field(default_factory=list)`.
  Se puede omitir o ser `[]`.
- Nuevo `@model_validator` exige que al menos UNO de `sources`
  non-empty o `default_value` no-`None` esté presente. Field
  completamente vacío sigue siendo error.

### Uso

```yaml
metadata:
  field_sources:
    BAC_BANCO:
      default_value: "BAC"                # ← sin sources, OK

    BAC_CIF:
      sources:
        - source_type: rvabrep
          lookup_value_column: index2
      default_value: "000000"             # ← legacy, sigue funcionando
```

### Notas

- **Backward-compat total**: configs pre-083 con sources non-empty
  siguen validando idénticamente.
- El motor (`MetadataService._resolve_one`) ya soportaba sources
  vacíos cayendo al default — el cambio es 100% del lado del schema.
- **6 tests nuevos** entre
  `tests/unit/config/test_field_config_default_only.py` y
  `tests/unit/services/test_metadata_default_only.py`.
- Pareja con spec 084 (AS400 metadata source): 083 cubre constantes,
  084 cubre lookup dinámico AS400. Las dos juntas eliminan los
  workarounds con sources dummy.

---

## [0.84.0] — 2026-05-17 — **Writer thread del SQLiteTrackingStore resiliente**

Bug productivo crítico: docs quedaban en ``S4_DONE`` aunque el
upload completaba exitosamente (eventos ``cmis_upload`` con status
2xx en los logs). **Sin error visible. Sin ``S5_DONE`` ni
``S5_FAILED`` en SQLite.** El pipeline seguía con el siguiente
doc. Resultado neto: TODOS los uploads "fallaban" según el
tracking, aunque CM los había recibido.

### Causa raíz

``SQLiteTrackingStore`` usa un **daemon thread** que drena una
queue de escrituras. Pre-082 el except del loop capturaba **solo**
``sqlite3.Error``:

```python
except sqlite3.Error:
    _log.exception("tracking writer: batch commit failed (size=%d)", len(batch))
```

Si cualquier otra excepción (``TypeError``, ``ValueError``,
``RuntimeError``…) escapaba dentro del while, el thread moría
silenciosamente. Daemon threads no propagan al main thread — solo
desaparecen. Las escrituras siguientes a la queue se perdían sin
trace.

### Fixed

- **Inner try/except**: ``sqlite3.Error`` → ``Exception``. Cualquier
  fallo de commit se loguea y se reintenta drenando.
- **Outer try/except global** envolviendo el while entero — si una
  excepción escapa de ``_drain_batch`` o cualquier helper, el
  thread loguea y sigue vivo.
- **Test regression**: monkey-patch ``_drain_batch`` para tirar
  ``RuntimeError`` y verifica que el writer thread sigue
  ``is_alive()`` después.

### Notas

- Síntoma típico antes del fix: ``cmcourier batch show <batch>``
  reportaba todos los docs en ``S4_DONE`` aunque hubieran subido a CM.
- ``s5_upload_attempt`` y ``cmis_upload`` events presentes en logs,
  pero ningún ``stage_done`` post-S4.
- Si los uploads sí estaban en CM, los datos son recuperables —
  el tracking SQLite se puede re-sincronizar via ``cmcourier sync``
  contra el repo CMIS (otra spec si se necesita).
- **2 tests nuevos** en
  ``tests/unit/adapters/tracking/test_writer_resilience.py``.
- Total: **662 unit tests passed** (660 previos + 2 nuevos).

---

## [0.83.0] — 2026-05-17 — **`max_bandwidth_mbps` honra la convención de networking (Mbps reales)**

**Breaking change semántico**. Pre-083 el field
``cmis.max_bandwidth_mbps`` decía "mbps" en su nombre pero se trataba
internamente como megabytes/s. El operador configuraba ``50``
esperando 50 Mbps (6.25 MB/s) y obtenía 50 MB/s (400 Mbps), 8x más
permisivo de lo que pedía.

### Changed

- **``TokenBucket.__init__``**: ``rate = mbps × 125_000`` bytes/s
  (1 Mbps = 1_000_000 bits/s = 125_000 bytes/s). Pre-083 era
  ``mbps × 1_000_000`` que asumía megabytes.
- **``TUIDataProvider``**: divide ``max_bandwidth_mbps`` por 8 al
  cachear el ceiling del chart, para que el eje Y (en MB/s, donde
  el sampler escribe) coincida con la unidad correcta.

### Migración para operadores

Si tu YAML tenía un valor que **funcionaba para un throughput
deseado en MB/s**, multiplicalo por 8 antes de upgrade:

```yaml
# Pre-083 (interpretado como MB/s):
cmis:
  max_bandwidth_mbps: 50    # quedaba como 50 MB/s = 400 Mbps

# Post-083 (interpretado como Mbps real):
cmis:
  max_bandwidth_mbps: 400   # 400 Mbps = 50 MB/s — mismo throughput
```

Si tu valor era pensado **como Mbps** desde el principio, no
hace falta cambiar: el comportamiento ahora coincide con el nombre.

### Notas

- Tests existentes ajustados: ``TestTokenBucket`` y
  ``TestBandwidthLimiter`` cambian ``mbps=0.5`` → ``mbps=4.0`` y
  ``mbps=1.0`` → ``mbps=8.0`` para conservar el throughput de
  prueba (0.5 MB/s y 1 MB/s respectivamente).
- 5 tests nuevos en
  ``tests/unit/adapters/upload/test_bandwidth_mbps_semantics.py``
  verifican la conversión exacta — 8 Mbps = 1 MB/s, 80 Mbps =
  10 MB/s, 1 Mbps = 125_000 bytes/s.
- 1 test ajustado en ``test_data_provider`` para el nuevo ceiling
  computation.
- ``_BandwidthSampler.current_mbps()`` / ``peak_mbps()`` siguen
  devolviendo MB/s a pesar del nombre. Esos siguen siendo
  internamente confusos, pero su renombre toca demasiada
  superficie del TUI — diferido hasta que aparezca otra confusión.
- Total: **660 unit tests passed** (655 previos + 5 nuevos).

---

## [0.82.0] — 2026-05-17 — **Fix crítico: `BandwidthLimiter` rompía 100% de uploads con throttle activo**

Bug productivo crítico descubierto durante las pruebas de
throughput: con ``cmis.max_bandwidth_mbps > 0``, **100% de los
uploads fallaban** con:

```
TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'
  File ".../requests_toolbelt/multipart/encoder.py", line 488
    self.len = len(self.headers) + total_len(self.body)
```

Causa: ``cmcourier`` envuelve el file handle en ``BandwidthLimiter``
cuando hay throttle configurado. Pre-076 el adapter usaba
``httpx.Client.post(files=...)`` que armaba el multipart internamente
sin llegar al path problemático. Spec 076 cambió a
``MultipartEncoder`` para streaming real, y ahí emergió: el
``MultipartEncoder.__init__`` calcula ``Content-Length`` con
``total_len(body)`` que prueba ``__len__`` → ``len`` → ``fileno`` →
``getvalue`` y si no encuentra nada **devuelve ``None``
silenciosamente**. El ``BandwidthLimiter`` no exponía ninguno.

Por qué no se notó antes de las pruebas productivas: con
``max_bandwidth_mbps == 0`` (default), el stream NO se envuelve
y pasa el file handle nativo, que tiene ``fileno()``. El bug solo
se manifestaba con throttle activado.

### Fixed

- **``BandwidthLimiter.fileno()``** nuevo método que delega al
  stream subyacente. Con eso ``requests_toolbelt.total_len()`` obtiene
  el fd, hace ``os.fstat`` y calcula el tamaño correcto del file part.

### Notas

- **Workaround temporal** sin el fix: setear
  ``cmis.max_bandwidth_mbps: 0.0`` (sin throttle). Vuelve el path
  donde el stream es el ``fh`` nativo.
- **Gap de tests detectado**: los tests existentes del uploader
  mockean httpx con ``respx`` y no entran al path de
  ``MultipartEncoder.__init__`` con un ``BandwidthLimiter`` real.
  Spec 080 cierra ese gap con 3 tests nuevos en
  ``tests/unit/adapters/upload/test_bandwidth_limiter_fileno.py``
  — incluyendo un regression test del bug exacto.
- Total: **655 unit tests passed** (652 previos + 3 nuevos).

---

## [0.81.0] — 2026-05-17 — **Chart de UPLOAD con eje Y etiquetado + detección de link speed**

Iteración del chart del 080 con tres ajustes operativos:

1. **Barras pegadas**: el espacio entre barras del 080 confundía
   más que ayudaba. Ahora cada barra ocupa 1 columna, sin aire —
   convención de chart-of-bars estándar.
2. **Eje Y con etiquetas numéricas**: cada row del chart se
   prefijia con una etiqueta de 4 chars + ` │ ` separator. Ticks
   en 100%/75%/50%/25% del cap. El operador lee la altura de cada
   barra sin contar pixels.
3. **Techo del eje Y desde la NIC**: cuando ``cmis.max_bandwidth_mbps``
   no está configurado, ya no caemos al peak observed (que cambia
   con cada upload). En su lugar, detectamos la velocidad de la
   interfaz física más rápida UP via ``psutil.net_if_stats()`` y la
   convertimos a MB/s (÷ 8). Para 1 Gbps NIC → cap = 125 MB/s.

### Added

- **``src/cmcourier/observability/network_info.py``** nuevo módulo
  con ``detect_link_speed_mbps()`` — usa psutil para encontrar la
  NIC física más rápida UP. Filtra interfaces virtuales por prefix
  (``lo``, ``docker``, ``br-``, ``veth``, ``vboxnet``, ``tun``,
  ``vpn``, ``Loopback``, ``utun``, etc.).

### Changed

- **``src/cmcourier/tui/chart.py``** `render_bar_chart`:
  * Barras pegadas (sin spacing).
  * Sub-sampling ahora basado en ``width_chars`` directo
    (cada barra es 1 char), no en ``// 2``.
  * Nuevo kwarg ``show_y_axis: bool = True`` — controla si cada
    line se prefijia con el label del eje Y.
- **``src/cmcourier/tui/data_provider.py``**: cachea el ceiling
  de bandwidth UNA VEZ al construir, con prioridad
  ``cmis.max_bandwidth_mbps`` > NIC link speed > auto-scale.
- **``src/cmcourier/tui/upload_tab.py``**: footer del eje X
  re-alineado al nuevo prefix del chart (4 chars label + ` └`).

### Notas

- **Comportamiento del ceiling**: si la detección de NIC falla
  (ej. solo virtuales, psutil exception), el fallback es 0.0 →
  auto-scale al peak. Comportamiento pre-079 como red de
  seguridad.
- **Tests nuevos**: 15 en
  ``tests/unit/tui/test_chart_bar_y_axis.py`` (7) y
  ``tests/unit/observability/test_network_info.py`` (8). Los
  14 tests del 078 ahora pasan ``show_y_axis=False`` para
  testear el core sin el prefix.
- Total: **652 unit tests passed** (637 previos + 15 nuevos,
  cero regresiones).

---

## [0.80.0] — 2026-05-17 — **Chart de UPLOAD multi-línea con barras delgadas verdes**

El sparkline de bandwidth del tab UPLOAD (025 fase 3) era **una
sola línea** de caracteres bloque Unicode. Era chico, sin contraste,
sin color — operativamente difícil de leer durante una corrida real.

### Changed

- **``src/cmcourier/tui/chart.py``** gana
  ``render_bar_chart(values, *, y_max, height=8, width_chars=60,
  color="green")``. Devuelve un string multi-línea ya envuelto en
  rich markup ``[color]...[/color]``. Resolución vertical de
  ``height * 8`` sub-niveles (64 por default) — 8x más que el
  sparkline. Las barras ocupan 2 columnas cada una (bloque +
  espacio) para look "delgada" con aire entre barras.
- **``src/cmcourier/tui/upload_tab.py``** reemplaza la llamada a
  ``render_sparkline`` por ``render_bar_chart`` con altura 8,
  ancho 60, color ``green``.
- **Sub-sampling automático**: si la serie tiene más valores
  que ``width_chars // 2`` barras (típicamente 30 para
  ``width_chars=60``), los valores se agrupan promediando. La
  serie de 60 segundos se ve como 30 barras de 2 segundos cada
  una.
- **Mínimo visible**: si un valor con ``ratio > 0`` cae en
  sub-nivel 0 por redondeo, se fuerza un ``▁`` en la línea
  inferior. Garantiza que cualquier bandwidth no-nulo se vea,
  por más chico que sea.

### Notas

- **``render_sparkline`` permanece intacto** — ningún otro tab lo
  usa hoy, pero preservamos la API por si alguien lo agrega en
  el futuro. Cumple con open/closed (no rompemos consumers
  potenciales).
- **Color es configurable** vía el kwarg ``color``. Por default
  ``green`` (Textual rich color name). Cualquier color rich
  válido funciona.
- **Tests nuevos**: 14 en
  ``tests/unit/tui/test_chart_bar.py`` cubriendo edge cases
  (vacío, todo cero, ceiling cero), rendering proporcional
  (full, half, quarter), spacing entre barras, sub-sampling,
  color markup, mínimo visible, y alineamiento de columnas.
- Total: **637 unit tests passed** (623 previos + 14 nuevos),
  cero regresiones.

---

## [0.79.0] — 2026-05-17 — **Progress de upload en tiempo real**

Pre-079, el sampler de bandwidth de la TUI (tab UPLOAD) solo
recibía datos cuando un upload completaba. Para uploads de varios
segundos (archivos grandes contra LAN de 1 Gbps), la TUI mostraba
0 MB/s durante todo el upload y los datos aparecían de golpe al
completion. Operativamente: imposible ver throughput en vivo de
un upload puntual, imposible detectar throttling intermedio o
sockets colgados a mitad de upload.

### Fixed

- **``CmisUploader._post_with_retries``** ahora envuelve el
  ``MultipartEncoder`` (076) en un
  ``MultipartEncoderMonitor`` con un callback de progreso. Cada
  vez que el encoder transmite ≥ 1 MB acumulado (umbral
  ``_PROGRESS_THRESHOLD_BYTES``), el callback emite un evento
  ``cmis_upload_progress`` con ``bytes_delta``.
- **``_BandwidthSampler``** gana ``record_progress(bytes_delta,
  ts=None)``: agrega bytes al bucket del segundo current, sin
  esperar al completion. Thread-safe vía el mismo lock existente.
- **``_BandwidthHandler.emit``** ahora procesa también
  ``cmis_upload_progress``. El branch ``cmis_upload`` (completion)
  resta los ``progress_bytes`` ya reportados del ``size_bytes``
  total para evitar double-counting.
- **``_emit_network``** acepta ``progress_bytes=0`` (kwarg opcional)
  y lo agrega al log record si es no-cero.

### Notas

- **Threshold de 1 MB**: para uploads chicos (< 1 MB) no se
  emite ningún evento de progress — el completion event los
  contabiliza con la lógica 069 existente. Para uploads grandes
  (500 MB) → ~500 eventos de progress durante el upload, uno
  por cada MB transmitido. La TUI ve ``current_mbps`` updateándose
  cada segundo.
- **Sin double-counting**: ``cumulative_bytes`` final coincide
  exactamente con el ``size_bytes`` del upload, verificado por
  tests.
- **Edge case de retries**: si un upload falla a mitad y se
  reintenta, los progress events del attempt fallido siguen
  contados en el sampler. ``cumulative_bytes`` puede exceder
  ligeramente el real (sobre-conteo en error transitorio).
  Aceptable para telemetría — el contractual es el
  ``migration_log`` del tracking, no los samplers de la TUI.
- **Tests nuevos**: 11 en
  ``tests/unit/observability/test_bandwidth_progress.py``
  cubriendo el sampler (4), el handler (3), el branch de
  completion con/sin progress_bytes (3), y un sanity test (1).
- Total: **623 unit tests passed** (612 previos + 11 nuevos),
  cero regresiones.

---

## [0.78.0] — 2026-05-17 — **`MultipartEncoder` streaming para uploads CMIS (throughput fix)**

S5 corría a **3 MB/s** contra IBM Content Manager v8 en producción,
mientras que el legacy ``RVIMigration`` llegaba a **45 MB/s** contra
la misma infra y ``curl -F`` directo medía **55 MB/s** desde la
misma compu. El cuello no era infra ni CM: era el armado del body
multipart en CMCourier.

Spec 060 migró de ``requests`` a ``httpx[http2]``. El cambio
introdujo silenciosamente un bug de pre-buffering:
``httpx.Client.post(files={...})`` lee el archivo entero a memoria
para armar el body multipart como un único ``bytes`` blob antes de
abrir el socket TCP. Con 20 workers paralelos subiendo docs de
25 MB:

* **20 × 25 MB = 500 MB** allocados en RAM antes de transmitir el
  primer byte.
* **20 threads** peleando por el GIL haciendo el armado en Python.
* **GC pauses** cascading con tanta memoria alloc/free rápido.
* Los sockets TCP **idle** durante toda la fase pre-buffer.

El legacy (pre-060) usaba ``requests-toolbelt.MultipartEncoder``,
que es un **iterator lazy**: lee chunks de 8 KB del disco
on-demand y los manda **directo al socket TCP**. Cero buffer en
RAM. Pre-076 ese comportamiento estaba perdido. Spec 060 probó
solo contra Alfresco — HTTP/2 con frontend Apache absorbe el
pre-buffer vía multiplexing. Contra IBM CM v8 (HTTP/1.1 puro, sin
multiplexing) el bug es dominante.

### Fixed

- **``CmisUploader._post_with_retries``** ahora arma el body con
  ``requests_toolbelt.MultipartEncoder`` y lo pasa a httpx como
  ``content=iter(lambda: encoder.read(8192), b"")`` con
  ``Content-Length`` explícito desde ``encoder.len``. Los chunks
  del archivo van **directo del disco al socket TCP**, sin
  buffer intermedio. Esperamos throughput equivalente al legacy
  (~45 MB/s) o mejor en runs productivos contra IBM CM v8.
- El encoder **se reconstruye en cada retry attempt** después del
  ``stream.seek(0)`` existente. ``MultipartEncoder`` es lazy:
  construirlo no toca el archivo, costo cero.
- ``Content-Length`` viaja explícito en headers — evitamos
  ``Transfer-Encoding: chunked`` que algunos servers viejos no
  manejan bien.

### Changed

- **Re-agregada dep runtime ``requests-toolbelt>=1.0,<2.0``** en
  ``pyproject.toml``. Spec 060 la había sacado al migrar a httpx;
  la traemos de vuelta porque ``MultipartEncoder`` no tiene
  equivalente nativo en httpx. **NO traemos ``requests`` ni
  ``RequestsAdapter``** — seguimos con ``httpx[http2]`` para todo
  lo demás (warmup, retry, HTTP/2 contra Alfresco).

### Notas

- **Cómo verificar el fix**: en el TUI tab UPLOAD durante un run
  productivo, ``peak_mbps`` debería superar significativamente
  los 7 MB/s previos, y ``current_mbps`` mantenerse sostenido
  por encima de los 10 MB/s. Contra LAN corporativa de 1 Gbps,
  esperamos 30-50+ MB/s.
- **Tests nuevos**: 5 en
  ``tests/unit/adapters/upload/test_multipart_encoder.py`` —
  valida que el POST usa ``content=`` (no ``files=``/``data=``),
  que el body es un iterable lazy (no bytes), que los campos
  CMIS están presentes, y que el retry reconstruye el encoder
  sin reusar uno consumido. Cero regresión en los 56 integration
  tests del adapter (``respx`` sigue capturando el POST OK
  aunque el formato del body interno cambió).
- Total: **612 unit tests passed** (607 previos + 5 nuevos).
- **Si el fix no resuelve los 3 MB/s**: hipótesis siguientes en
  spec 076 (plan B) son GIL contention en httpx, Windows TCP
  send buffers default, o algún cuello específico de IBM CM v8
  que el legacy bypassaba.

---

## [0.77.0] — 2026-05-17 — **Normalizar `image_path` del RVABREP en IndexingService**

El ``ABAICD`` (``image_path_column``) del RVABREP real del banco
viene con leading forward-slash — convención del RVI heredado, que
escribe paths "absolutos" desde la raíz del file share
(ej. ``/RVI9/020526/0004``). Pre-077 ese path llegaba al assembler
tal cual y al concatenarlo con ``assembly.source_root`` vía
``Path / Path``, pathlib descartaba silenciosamente ``source_root``:

```python
>>> Path("sample/mockfiles") / "/RVI9/020526/0004" / "DOC.001"
WindowsPath('/RVI9/020526/0004/DOC.001')   # source_root evaporado
```

Resultado: el assembler buscaba los archivos en el root del drive
(``C:\RVI9\...`` en Windows, ``/RVI9/...`` en Linux), ignorando
completamente la config ``assembly.source_root``. ``mock generate``
materializaba los archivos en ``<root>/RVI9/...`` correctamente
(porque su planner sí normaliza), pero la pipeline real no los
encontraba — los dos interpretaban el mismo ``ABAICD`` de forma
diferente.

Misma clase de bug que 074 (CHAR padding): un detalle de
representación del RVI/AS400 filtraba al dominio. Fix en la misma
frontera adapter-source ↔ dominio.

### Fixed

- **``services/indexing.py:_row_to_document``** ahora normaliza
  ``image_path`` antes de construir el ``RVABREPDocument``: aplana
  backslashes a forward slashes, strippea whitespace, strippea
  leading separators. Casos cubiertos:
  - ``"/RVI9/020526/0004"`` → ``"RVI9/020526/0004"`` (caso real)
  - ``"\RVI9\020526\0004"`` → ``"RVI9/020526/0004"``
  - ``"//RVI9/..."`` → ``"RVI9/..."``
  - ``"  /RVI9/...  "`` → ``"RVI9/..."``
  - paths relativos quedan intactos.
- Helper privado ``_normalize_image_path`` en
  ``services/indexing.py``. No se reutiliza el helper homónimo del
  planner del ``mock generate`` (que devuelve ``Path``) — para
  mantener el tipo ``str`` del campo
  ``RVABREPDocument.image_path``.

### Notas

- **Casos NO cubiertos** (siguen siendo "absolutos" para pathlib y
  pisarían ``source_root``):
  - ``"C:\some\path"`` (drive letter explícito) — raro en AS400.
  - ``"\\server\share\path"`` (UNC) — el operador debe configurar
    ``source_root`` como raíz del share.
- **Tests nuevos**: 10 en
  ``tests/unit/services/test_indexing_image_path.py`` cubriendo
  los 5 casos del happy-path + edge cases (empty, only-separators,
  internal/trailing separators preservados).
- Total: **607 unit tests passed** (597 previos + 10 nuevos), cero
  regresiones.

---

## [0.76.0] — 2026-05-17 — **Strip whitespace de strings que vuelven del AS400**

Los campos ``CHAR(N)`` de DB2 / iSeries vuelven *padded* a longitud
fija con espacios — un ``CHAR(1)`` con valor lógico vacío llega a
Python como ``" "``, un ``CHAR(8)`` con ``"SHORT1"`` llega como
``"SHORT1  "``. Pre-076 ese padding filtraba al dominio y rompía
múltiples cosas a la vez:

* El check de "deleted" interpretaba ``ABACST = " "`` (un solo
  espacio padding) como truthy y tiraba ``RVABREPDeletedError``,
  haciendo que un RVABREP perfectamente válido apareciera como
  "every record marked deleted".
* El matching de triggers contra RVABREP fallaba silenciosamente
  cuando ``"SHORT1  "`` no matcheaba ``"SHORT1"``.
* La idempotency key (``rvabrep_txn_num``) terminaba en SQLite +
  como ``cmis:name`` en CM con trailing whitespace.
* Lookups en metadata sources AS400 fallaban por la misma razón
  (``BRANCH_ID = "001     "`` ≠ ``"001"``).

Estos no eran bugs separados — eran un único hueco en la frontera
adapter-dominio. El adapter leakeaba un detalle de representación
de DB2 al resto del sistema. El fix se aplica donde corresponde: en
el adapter mismo.

### Fixed

- **``As400DataSource`` strippea trailing whitespace** de valores
  ``str`` al materializar rows de pyodbc (``query()`` y
  ``query_stream()``). Único punto de intervención — aplica a S1
  indexing, S3 metadata sources AS400, ``mock generate
  --rvabrep-as400``, y al passthrough ``cmcourier as400-query``.
- Helper privado ``_normalize_row(columns, row)`` aplica
  ``.strip()`` solo a valores ``str``. Tipos numéricos
  (``int``/``float``/``Decimal``), ``date``/``datetime``,
  ``bool``, ``bytes`` y ``None`` pasan sin tocar.

### Notas

- **No afecta CSVs** (``TabularDataSource``). Los CSVs no tienen
  padding fixed-width; si vienen con espacios, son intencionales.
- **Cambio observable** para operadores que hacían
  ``cmcourier as400-query "SELECT ..."`` y veían los strings con
  padding. Post-076 los ven trimmed. **Esto es deseable** — el
  padding nunca era información, era ruido de representación.
- **Tests nuevos**: 17 en
  ``tests/unit/adapters/sources/test_as400_normalize.py`` cubriendo
  cada tipo de valor + filas mixtas realistas. Total: **597 unit
  tests passed** (580 previos + 17 nuevos).

---

## [0.75.0] — 2026-05-16 — **Portability fixes para AS400 real**

Tres bugs surgieron durante la primera preparación productiva contra
el AS400 real del banco desde un cliente Windows. Los tres son fixes
quirúrgicos (1-2 líneas + tests), ninguno cambia arquitectura. Se
shippean juntos porque pertenecen al mismo dominio: "hacer que
CMCourier funcione contra un AS400 real desde una compu nueva".

### Fixed

- **`doctor` `as400_connectivity`** usa ahora
  `SELECT 1 FROM SYSIBM.SYSDUMMY1` (la pseudo-tabla canónica de DB2 /
  iSeries) en lugar de `SELECT 1` solo. DB2 rechaza `SELECT` sin
  `FROM` con sqlstate 42000, así que el check fallaba aunque la
  conexión y las credenciales estuvieran OK.
- **`mock generate --rvabrep-as400` respeta `indexing.source.query`**.
  Pre-075 ignoraba completamente el `query` con WHERE / JOIN
  definido en el YAML y ejecutaba `SELECT * FROM <table>` sobre la
  tabla entera. Ahora si hay `source.query`, el adapter lo wrappea
  como `(query) AS T` y solo procesa el subconjunto filtrado.
- **`mock generate` fallback table prepende schema**. Cuando ni
  `source.query` ni `connection.table` están seteados, el fallback
  ahora es `{database}.RVABREP` (típicamente `RVILIB.RVABREP`) en
  lugar de `RVABREP` bare, que fallaba con `table not found` cuando
  la library no estaba en la library list del usuario AS400.
- **Planner del `mock generate` ahora es permisivo ante
  `image_type` desconocido**. Pre-075, cualquier valor de `ABABST`
  distinto a `B` / `C` / `O` (ej. `T`, `P`, vacío, o un código del
  banco que no estaba en el RVABREP del banco original) abortaba
  toda la generación con `ConfigurationError`. Ahora el planner
  emite un warning estructurado (`txn_num`, `image_type`,
  `file_name`, `reason`) y skipea esa fila. El run sigue procesando
  el resto. La misma lógica aplica al caso contradictorio
  `ABABST=O` con filename no-PDF.

### Notas

- **Cambio de contrato en el planner**: pre-075 el comportamiento
  era fail-fast; post-075 es warn-and-continue. Operadores que
  dependían del fail-fast para detectar RVABREP malformado pueden
  grep-ear los warnings post-run o usar `cmcourier analyze` para
  agregarlos.
- **Tests nuevos**: `test_doctor_as400_query.py` (3 tests),
  `test_mock_generate_as400.py` (5 tests), `test_planner.py`
  reemplaza el test de `raises` por 3 tests de skip + warning. Total:
  8 tests nuevos, 1 reemplazado.
- **Sin cambios funcionales** fuera de los tres fixes citados.

---

## [0.74.0] — 2026-05-16 — **Sacar reference data de `docs/`**

`docs/samples/` estaba mezclando documentación (un YAML anotado),
fixtures de datos (CSVs, xlsx, response txt), y código legacy
suelto del proyecto viejo (`cmis_service.py`). Peor: el código de
producción dependía de la ruta — `cmcourier mock rvabrep` tenía
como default `docs/samples/csv/MapeoRVI_CM.csv`, así que `docs/`
no era solo doc sino también data directory. Eso violaba un
principio implícito: docs es para leer, no para ser dependency
runtime.

### Cambiado

- **Nuevo directorio `reference-data/` en la raíz** con
  `csv/`, `excel/`, `cmis-responses/`. Los archivos del antiguo
  `docs/samples/csv/`, `excel/`, `responses/` se movieron acá
  vía `git mv` (history preservado).
- **`docs/samples/config-reference.yaml` → `docs/reference/config-reference.yaml`**.
  El YAML anotado sí es doc; va donde corresponde.
- **`src/cmcourier/cli/commands/mock.py`**: `_DEFAULT_IDRVI_SOURCE`
  apunta a `reference-data/csv/MapeoRVI_CM.csv`. La string del
  flag `--help` también.
- **Path references actualizadas** en `docs/INDEX.md`,
  `docs/reference/cli.md`, `docs/adr/007`,
  `docs/how-to/mock-rvabrep-generator.md`,
  `docs/how-to/local-staging-simulation.md`,
  `docs/how-to/developer/add-a-new-config-field.md`,
  `docs/tutorials/{README,01,05}.md`.
- **Constitution + skill registry**: `.specify/memory/constitution.md`
  y `.atl/skill-registry.md` apuntan a `reference-data/` en vez
  de `docs/samples/`. También se removió la mención a
  `docs/domain/` (no existe desde 0.74).

### Removido

- **`docs/samples/cmis_service.py`** (untracked, gitignored) —
  borrado del filesystem. Era código legacy del proyecto viejo
  sin referencias en el código actual.
- **`docs/samples/`** — directorio eliminado, queda vacío
  después del move.

### Notas

- **Breaking change menor**: `cmcourier mock rvabrep` sin
  `--idrvi-source` ahora resuelve `reference-data/csv/MapeoRVI_CM.csv`
  en lugar del path viejo. Operadores que invocaban el comando
  desde un cwd que no tenga `reference-data/` deben pasar
  `--idrvi-source` explícito.
- **Specs históricas (001, 003, 035, 039, 050, 056, 061)** mantienen
  los paths viejos. No se reescribe history.
- **`CHANGELOG` entries pre-0.74** mantienen los paths viejos por
  la misma razón.
- Cero cambios en tests — ningún test referenciaba `docs/samples/`.

---

## [0.73.0] — 2026-05-15 — **Refactor cosmético: comentarios y docstrings en español + remoción de referencias al code-name antiguo**

Cambio puramente cosmético, sin modificaciones funcionales. Dos
transformaciones en un mismo refactor:

### Cambiado

- **Todos los comentarios y docstrings traducidos a español.**
  Cubre todos los archivos `.py` en `src/cmcourier/` y `tests/`
  (~184 archivos), más todas las specs bajo `specs/` (~213
  archivos `.md`), `README.md`, `docs/` (excluyendo el doc
  histórico de dominio) y la estructura del `CHANGELOG`.
  Nombres de identificadores (clases, funciones, variables,
  módulos) quedan en inglés. Términos técnicos sin traducción
  natural (`back-pressure`, `race condition`, `multipart`,
  `worker pool`, `bucket`, `chunk`, `AIMD`, etc.) quedan en
  inglés entre backticks dentro del texto español.
- **Removidas todas las referencias al code-name antiguo**
  (`REBIRTH §X.Y` y enlaces al archivo histórico). Esa
  convención ya no es relevante — el código se mantiene
  autocontenido sin esa muleta.

### Notas

- Cero cambios funcionales. La suite completa (1306 tests)
  pasa idénticamente. ruff + mypy limpios.
- `CHANGELOG.md`: headers y estructura traducidos. Los cuerpos
  de prosa de versiones individuales mantienen su idioma
  original (mezcla parcial inglés/español) — el "esqueleto
  navegable" es consistente; los detalles técnicos densos no
  se forzaron a una sola pasada.
- `docs/domain/CMCOURIER_REBIRTH.md` (1434 líneas) queda en
  inglés como documento histórico de handover. Si se necesita
  traducir, va en una spec separada.
- Strings literales (mensajes de log, errores, output del CLI,
  texto del TUI) quedan en inglés — eso es lo que el operador
  ve hoy en producción.

---

## [0.72.0] — 2026-05-15 — **Unificar el LaneController entre streaming + batched**

Operator-reported in the post-067 streaming run: UPLOAD tab's
LANES sub-block shows `queue 0` for both HEAVY and LIGHT — always,
never moves. BUCKET tab's LANES block shows the same data live and
correct.

Root cause: there were **two `LaneController` instances** in a
streaming run with `heavy_light_lanes.enabled: true`.
`StagedPipeline.__init__` (036) constructed one. The 065
`StreamingOrchestrator.__init__` constructed another. The
TUIDataProvider's `lane_snapshot` field reads the **pipeline's**
instance — the dead one in streaming mode. The streaming
orchestrator's dispatcher + consumers only wrote to its own
(live) instance.

Beyond the UI bug, this also silently broke AIMD's per-lane budget
steering in streaming mode since 065:
`StagedPipeline._on_pool_resize → pipeline.lane_controller.set_total_budget`
was setting the budget on the *idle* controller; the streaming
dispatcher's actual per-lane semaphore split never got rebalanced
by AIMD.

### Corregido

- **`StreamingOrchestrator` reuses `pipeline.lane_controller`**
  instead of constructing its own. The constructor no longer
  builds a `LaneController(...)`. `self._lane_controller` becomes
  a read-only property forwarding to `self._pipeline.lane_controller`,
  which keeps every existing call site (`self._lane_controller.start()`,
  `.set_queue_depth(...)`, etc.) working unchanged.
- The TUI `lane_snapshot` field, the BUCKET-tab `bucket.lane_snapshot`
  field, and the streaming dispatcher all now hit one controller
  instance per run.
- AIMD's `set_total_budget` reaches the live streaming-mode
  per-lane semaphores.

### Notas

- No behaviour change for batched mode.
- No behaviour change for streaming single-lane mode (lanes
  disabled).
- In streaming + lanes mode: UPLOAD-tab LANES block becomes live;
  AIMD-driven lane rebalance becomes effective.

---

## [0.71.0] — 2026-05-15 — **Sampler de bandwidth: distribuir bytes sobre la ventana real de transmisión**

Operator-reported during the same 068 staging run: even after AIMD
scales aggressively, the TUI shows `current 11 MB/s` one second
and `0 MB/s` the next. The sparkline doesn't look like sustained
transmission — it looks like spikes.

Root cause: `_BandwidthSampler.record_upload(size, completed_at)`
credited the **entire file size to the second of completion**. A
30 MB upload that took 3 seconds put all 30 MB in one bucket and
zero in the other two — even though the bytes were actively
transmitting during them. `current_mbps()` (which reads the
previous full bucket) became completion-event aliased: it flipped
between "spike" and "valley" depending on the bucket's luck.

### Corregido

- **`_BandwidthSampler.record_upload`** new signature
  `(size_bytes, *, started_at, completed_at)`. Distributes bytes
  uniformly across the second-buckets that overlap
  `[started_at, completed_at]`. For a 30 MB upload from t=10.5 to
  t=13.5: 5 MB → bucket 10, 10 MB → 11, 10 MB → 12, 5 MB → 13.
  `current_mbps`, `peak_mbps`, and the 60-bucket sparkline now
  reflect sustained throughput instead of completion aliasing.
- **`_BandwidthHandler.emit`** reads `duration_ms` off the
  `cmis_upload` log record (already emitted by
  `CmisUploader._emit_network`) and derives
  `started_at = completed_at - duration_ms / 1000`. Defensive
  fallback to crediting at completion when `duration_ms` is
  missing or zero.

### Notas

- `cumulative_bytes` is unchanged — still the authoritative
  running total. The change is only in how bytes are *distributed
  across the rolling window*, not in their sum.
- `peak_mbps` now caps at the actual sustained rate.
  Pre-069 a 30 MB completion in one second reported
  `peak 30 MB/s` even when sustained throughput was 10 MB/s.
  Post-069 it reports 10 MB/s — honest.
- One existing caller in tests
  (`tests/unit/tui/test_data_provider.py`) updated to the new
  signature.

---

## [0.70.0] — 2026-05-15 — **Crecimiento agresivo de AIMD + halve suave (cargas con archivos pesados)**

Operator-reported during a `mockfiles-mixed` streaming run against
Alfresco staging: bandwidth peak `<20 MB/s` despite a 300 Mbps
internet ceiling. UPLOAD-tab `pool capacity 4-8` for the whole
run despite `auto_tune.max_threads: 50`. AIMD never reached its
ceiling.

Root cause: the pre-068 AIMD was tuned for small, latency-sensitive
uploads. With heavy files (10-30 MB) the per-upload p95 has natural
variance — a single 40-second outlier (server commit + TLS
re-handshake + GC pause) trips the `1.2× target` halve threshold
and `÷2`s the pool. Then recovery is `+1 per 15s tick`, so a single
halve costs ~6 minutes of growth.

### Agregado

Three tunable knobs on `cmis.auto_tune`:

- **`growth_factor: float = 1.25`** (range [1.0, 4.0]) —
  multiplier per grow tick. Grow step is
  `max(current + 1, ceil(current * growth_factor))`. Default 1.25
  takes 6 → 50 workers in ~10 ticks (2.5 min at 15s/tick) vs the
  pre-068 11 minutes. Setting `1.0` recovers the additive `+1`
  shape (degenerate to the `+1` floor).
- **`halve_factor: float = 0.75`** (range [0.05, 1.0]) — multiplier
  per halve tick. Halve step is
  `max(min_threads, ceil(current * halve_factor))`. Default 0.75
  drops 25 % vs the pre-068 50 %. A false-positive halve no longer
  costs 6 min of growth. Setting `0.5` recovers the pre-068 shape.
- **`halve_threshold_ratio: float = 1.5`** (range [1.05, 10.0]) —
  the multiplier of `target_p95_ms` above which a halve fires.
  Default 1.5 means halve at `p95 > 1.5 × target` instead of
  `1.2 × target` — more tolerance for heavy-file p95 variance.
  Setting `1.2` recovers the pre-068 shape.

### Cambiado

- The `Decision.action` label changed from `"+1"` to `"+N"` —
  steps are no longer always 1. TUI `auto_tune_last_action` shows
  `"+N"` for grow ticks. Operators reading `state.yaml` or
  `auto_tune_decision` log records will see the new label.
- AIMD module header rewrote the **AI / MD** description as
  **MI (multiplicative increase) / Soft halve**.

### Notas sobre el impacto

* For your `mockfiles-mixed` 30 MB workload: capacity should
  reach the 50-thread ceiling within the first ~3 minutes and
  stay there. Bandwidth aggregate should rise from
  `<20 MB/s` peak toward whatever Alfresco + your 300 Mbps router
  can sustain — typically somewhere in the 30-150 MB/s range
  depending on Alfresco's per-request commit time. The next
  bottleneck is per-upload speed against the server, not Python.
* Operators wanting the pre-068 conservative shape can pin the
  three knobs to their pre-068 values in YAML.
* `min_threads` still floors the halve step — operators can also
  raise that defensively (e.g. `min_threads: 10`) to guarantee a
  hard floor on parallelism regardless of how many bad ticks
  AIMD sees.

---

## [0.69.0] — 2026-05-15 — **Fixes de bugs en TUI modo streaming: barra, timer, CHUNKS, cola de lane**

Operator-reported during the first end-to-end streaming run after
0.68.0 shipped. Four distinct bugs in `StreamingOrchestrator`'s TUI
binding surface — all because the orchestrator inherited the chunk-
based binding shape from `MultiBatchOrchestrator` but never
populated its live fields. All four fixes live entirely in
`streaming.py`; no changes to the renderer or the batched path.

### Corregido

- **UPLOAD-tab progress bar stuck at `count/count`.** The bar's
  target = `count + queue_depth`. The batched path calls
  `pool_stats.set_queue_depth(...)` per cycle; streaming never did,
  so queue_depth stayed at 0 and the bar showed
  `count/count` permanently. Now the orchestrator publishes
  `main_bucket.qsize() + heavy_queue.qsize() + light_queue.qsize()`
  to `pool_stats` after every `put`/`get` — the bar now shows
  `count / (count + currently-pending)`.
- **Chunk timer never started, avg speed always 0.** Synthetic
  `ChunkState.status` was hard-coded to `"PREP"` for the whole run.
  `_current_chunk_progress` only computes `elapsed_s` when status is
  `"UPLOAD"` or `"DONE"`. Streaming now seeds the synthetic chunk
  with `status="UPLOAD"` and both monotonic stamps at run start, so
  the per-chunk timer ticks immediately and `avg_mbps` populates as
  the recorder accumulates bytes.
- **CHUNKS tab frozen at zero during the run.** `s5_done`,
  `s5_failed`, `doc_count`, `prep_done` were only set in the final
  ChunkState update at end-of-run. New `_publish_chunk_state` helper
  is called from both consumer loops after every S5 outcome — the
  CHUNKS tab now shows live progress.
- **LANES queue counter monotonic-up, exceeded `bucket_size`.** The
  dispatcher maintained private `heavy_depth`/`light_depth` counters
  that only incremented. After 5000 docs the tab showed
  `queue 2500` even though the actual queue size never exceeds
  `bucket_size=200`. The fix reports
  `lane_queue.qsize()` (live occupancy) instead, and the consumer
  also reports on `get()` so the counter decrements. This also
  fixes the LaneController's drain-driven rebalance heuristic
  (`_heavy_first_empty_at` / `_light_first_empty_at` never
  triggered pre-067 because depth never hit zero).

### Notas

- The UPLOAD-tab "X/Y docs" denominator in streaming is now
  `count + currently-pending`, which is honest but not the same as
  "total trigger count". Knowing the *total* upfront requires a
  config knob (`--total` already exists as a CLI override).
  A future change can plumb that into the synthetic chunk_state
  for a true `count / total` bar when total is known.

---

## [0.68.0] — 2026-05-15 — **Ensamblado PDF S4 en ProcessPoolExecutor (paralelismo CPU real)**

Diagnosed during a real streaming run with
`prep_workers: 16` and a mixed 5000-doc source: BUCKET tab showed
all 16 producers "in flight", bucket level near zero (S5 fast),
yet aggregate PREP throughput was **< 5 docs/s**. Root cause: the
GIL serializes Python bytecode execution; S4 (PDF assembly via
`img2pdf` + `PIL` + `PyPDF2`) is CPU-bound and dominated by
C-extension calls that do not always release the GIL. The 16
threads existed but only one ran at any instant.

S1 (indexing), S2 (mapping), S3 (metadata) are all dict-lookup-in-
memory — they parallelize fine with threading and were not affected.

### Agregado

- **`processing.s4_use_processes: bool = True`** — NEW DEFAULT.
  When true, S4 runs in a `ProcessPoolExecutor`, bypassing the
  GIL completely. Set to `false` to restore the 063/064/065
  inline behaviour byte-identically.
- **`processing.s4_max_processes: int | None = None`** —
  process count for the pool. `null` resolves to `os.cpu_count()`.
- **`cmcourier.adapters.assembly.pool`** — new module exposing
  `_pool_init` (worker initializer), `_pool_assemble` (worker
  entry point), and `build_s4_process_pool` (factory). All
  module-level functions for picklability.
- **`StagedPipeline.__init__(..., s4_process_pool=None)`** — new
  optional dependency injected by the wiring layer.

### Cambiado

- **`StagedPipeline._s4_one`** routes through
  `pool.submit(_pool_assemble, doc).result()` when the pool is
  set, calls `self._assembler.assemble(doc)` directly otherwise.
  The thread blocks on `.result()` but releases the GIL during the
  wait — other producers run S1-S3 work in parallel.
- **Wiring layer** (`config/wiring.py`) constructs the pool when
  configured and registers shutdown via `atexit`. A follow-up can
  move it to a pipeline `close()` for explicit lifecycle.
- **`SourceFileMissingError.__reduce__`** +
  **`PDFAssemblyFailedError.__reduce__`** — required for these
  exceptions to round-trip through `pickle` when they cross the
  worker → main process boundary. Pre-066 the kwargs-only
  `__init__` signature broke pickle unmarshalling.
- **`ProcessPoolExecutor` uses `multiprocessing.get_context("spawn")`**
  explicitly. The parent has many threads (producers, S5 pool,
  AIMD controller, sampler); `fork()` in a multi-threaded parent
  can leave the child in inconsistent lock state and emits a
  `DeprecationWarning` on Python 3.12. `spawn` builds a fresh
  interpreter and re-runs `_pool_init` cleanly.

### Notas sobre el impacto

- Aggregate PREP throughput scales with `s4_max_processes` for
  CPU-bound workloads. With `os.cpu_count() = 8`, expect ~8x
  speedup for runs dominated by S4 on large PDFs.
- Per-doc overhead: `+1-5ms` for IPC + pickle. Negligible against
  multi-second PDF assembly.
- Memory: each worker process has ~30-50 MB RSS. 8 workers add
  ~250-400 MB total.
- First-doc latency: pool spin-up + worker imports take 200-500 ms,
  amortized to nothing over the rest of the run.

---

## [0.67.0] — 2026-05-15 — **Lanes heavy/light en modo streaming**

063 shipped streaming with a single S5 consumer pool — a single
heavy PDF could starve a flock of small ones queued behind it.
036's heavy/light lanes existed for batched mode but had not been
wired into the streaming path. 065 closes the gap by inserting a
**dispatcher** thread between the main bucket and the S5
consumers; the dispatcher routes each prepared item into a
per-lane queue by `staged_file.size_bytes >= heavy_threshold_bytes`.
Heavy and light lanes each get their own consumer pool gated by
the existing `LaneController` semaphores + drain-driven rebalance.

### Agregado

- **Streaming dual-lane path** — when
  `processing.mode == "streaming"` **and**
  `heavy_light_lanes.enabled == true`, `StreamingOrchestrator`
  builds a `LaneController` and spawns: 1 dispatcher thread +
  `_pool_ceiling()` heavy consumers + `_pool_ceiling()` light
  consumers.
- **`StreamingSnapshot.lane_snapshot`** — `LaneSnapshot | None`
  field surfaces per-lane budget/busy/queue depth + total budget
  to the TUI. `None` keeps the single-lane shape.
- **`StreamingOrchestrator.lane_controller`** — read-only handle
  for tests + the TUI.
- **BUCKET-tab LANES sub-block** — renders heavy/light budget,
  busy, queue depth, and total budget when dual mode is active.
  Hidden in single-lane mode.

### Cambiado

- **`StagedPipeline.streaming_upload_one(..., lane=None)`** — new
  kwarg threads the lane choice to `_upload_one`, which already
  knows how to acquire the per-lane semaphore (036 wiring intact).
- The 063 startup WARN about
  "heavy/light lanes deferred to spec 065" is removed — the
  combination now works.

### Notas

- The dispatcher is a single thread — one comparison + one
  `queue.put` per item. It is not a bottleneck for any realistic
  workload (millions of docs at >>1k items/s would still fit).
- The total in-flight upper bound becomes
  `3 × bucket_size` (main bucket + heavy queue + light queue), so
  the operator's memory knob remains `bucket_size`. Bumping it 3×
  to compensate is unnecessary because the lane queues drain
  symmetrically — they are not all full at once.
- Lane choice is at *consume time*, not *queue time*. Items
  arrive in the main bucket in the order PREP finishes; the
  dispatcher then routes. This eliminates head-of-line blocking
  on a single heavy doc.

---

## [0.66.0] — 2026-05-15 — **Tab BUCKET del TUI para modo streaming**

063 introduced streaming mode but the live TUI was built for the
batched model — its CHUNKS tab showed a single synthetic row in
streaming mode, useless for live observability. 064 fills the gap
with a dedicated BUCKET tab showing the back-pressure state of
the bounded buffer, throughput on both sides of the bucket, and
cumulative outcomes.

### Agregado

- **BUCKET tab** (`b` keybind) — bucket level vs cap with an
  ASCII bar, peak level since run start, 5s sliding-window
  throughput for both PREP (docs entering the bucket) and S5
  (docs leaving), live producer in-flight count, configured worker
  totals, cumulative `S5_DONE` / `S5_FAILED` / `S1_FILTERED` /
  `S1_SKIPPED`.
- **`StreamingOrchestrator.streaming_snapshot()`** + new
  `StreamingSnapshot` dataclass — single read of every BUCKET-tab
  field. Reusable in tests; not coupled to Textual.
- **`StreamingOrchestrator.bucket_level()` /
  `prep_in_flight()`** — granular read accessors for tests and
  ad-hoc probes.
- **`_ThroughputWindow`** — internal 5s sliding-window rate
  estimator backed by a `deque` + lock. PREP records on each
  bucket-put, S5 records after each upload outcome.
- **`TUIDataProvider.mode` + `bucket_provider`** — orchestration
  mode propagates to the snapshot; the bucket provider is a
  callable so the data provider stays decoupled from the
  orchestrator type.

### Cambiado

- The TUI always mounts both CHUNKS and BUCKET tabs. The renderer
  prints a one-line stub on the inactive tab pointing the operator
  at the active one — recomposing tabs at runtime is unsupported
  by Textual after mount.
- `cli/app.py` passes `mode=config.processing.mode` and the
  orchestrator's `streaming_snapshot` callable (only when
  `StreamingOrchestrator`) into `TUIDataProvider`.

### Notas

- PREP `in_flight` increments before `streaming_prep_one` and
  decrements in `finally`, so the counter never leaks across
  exceptions.
- The throughput window is rate-only (events / window-seconds);
  for a low-rate run it round-trips to zero correctly once the
  window expires. No EWMA, no smoothing — the operator gets a
  literal "what happened in the last 5 seconds" read.

---

## [0.65.0] — 2026-05-15 — **Orquestador streaming: pipeline producer-consumer basado en bucket**

The batched pipeline has two structural costs that hurt at the
20M-doc scale: memory peak grows as
`batch_size × batches_in_flight` (today ~200 docs at 100×2), and
there is an idle "valley" between chunks where PREP and UPLOAD
out-pace each other and one side blocks. 063 ships a new
**streaming mode** that runs adjacent to the batched mode (the
operator picks one via `processing.mode`), built around the
canonical producer-consumer pattern: a bounded **bucket** of
prepared docs sits between PREP (S1–S4 producers) and UPLOAD
(S5 consumers), so memory peak collapses to `bucket_size` and
neither side ever waits for the other to finish a "batch".

### Agregado

- **`processing.mode: "batched" | "streaming"`** — orchestrator
  selector. Default `"batched"` keeps every byte of pre-063
  behaviour intact. `"streaming"` activates the new
  `StreamingOrchestrator`.
- **`processing.streaming.bucket_size: int = 100`** — the
  bounded-buffer size between PREP and S5. Memory peak is
  `bucket_size` (independent of total trigger count).
- **`StreamingOrchestrator`** (`cmcourier.orchestrators.streaming`)
  — same `.run(...)` shape as `MultiBatchOrchestrator` for CLI
  parity. Returns a `MultiBatchRunReport` with a single synthetic
  `RunReport`. Internals: one batch_id per run, one global
  `MetricsRecorder`, `prep_workers` producer threads + S5 consumer
  threads sized to `_pool_ceiling()`, poison-pill shutdown.
- **`StagedPipeline.streaming_prep_one`** — public entry point
  for producers; runs S1→S4 on a single trigger and returns
  `(survivor, skipped_cross_batch, s1_filtered)`. Reuses the
  existing per-stage helpers so filter / skip / failure
  persistence stays identical to the batched path.
- **`StagedPipeline.streaming_upload_one`** — public S5 thin
  wrapper used by streaming consumers.

### Cambiado

- **Resume in streaming mode = a new run.** `--from-stage > 1`
  and operator-named `--batch-id` raise `ValueError` when
  `processing.mode == "streaming"`. 062's cross-batch
  `S1_SKIPPED` rows provide traceability for docs already
  uploaded in any prior run.
- The CLI orchestrator factory now branches on `processing.mode`.
  Both orchestrators expose the same TUI binding surface
  (`active_recorder`, `upload_recorder`, `chunks_snapshot`) so the
  TUI degrades gracefully — the CHUNKS tab shows a single
  synthetic row in streaming mode; spec 064 replaces it with a
  real BUCKET tab.

### Notas

- **Heavy/light lanes** (036) are deferred in streaming mode for
  this spec — the wiring emits a clear WARN when
  `heavy_light_lanes.enabled: true` is combined with
  `mode: "streaming"`. Spec 065 integrates per-item lane choice
  into the streaming consumer.
- **TUI BUCKET tab** is deferred to spec 064. The other tabs
  (PREP / UPLOAD / DETAIL) work unchanged — they read the single
  global recorder.
- AIMD operates against the same single recorder for the whole
  run; 061's `min_samples` guard handles the cold-start outlier
  exactly as in batched mode.

---

## [0.64.0] — 2026-05-15 — **Persistir docs filtrados en S1 + skipeados cross-batch a migration_log**

The operator inspected the DETAIL tab during a staging run and
correctly noticed two gaps: the per-doc detail didn't show which docs
were filtered at S1 (delete-coded at source, spec 051) and didn't show
the docs skipped by cross-batch idempotency. Both were
counted but never persisted, so neither the DETAIL tab nor
`analyze batch` nor `cmcourier batch show` could surface them.

### Agregado

- **`StageStatus.S1_FILTERED`** — terminal state for docs whose
  RVABREP row was delete-coded at source. The row's
  `rvabrep_txn_num` is synthetic
  (`FILTERED__{shortname}__{system_id}`) because the exception fires
  before any document txn_num is derived; `error_message` carries
  `deleted_at_source; deleted_count=N` from the exception.
- **`StageStatus.S1_SKIPPED`** — terminal state for docs already
  `S5_DONE` in a prior batch. Real `rvabrep_txn_num`,
  `error_message="cross_batch_uploaded"`.
- **`ITrackingStore.mark_stage_terminal(txn, batch, stage, error_message)`**
  — new port method. Terminal transition that is NOT a failure:
  accepts any `*_FAILED` / `*_FILTERED` / `*_SKIPPED` suffix and
  does NOT bump `retry_count`.

### Cambiado

- **The prior "silent skip" contract is intentionally reversed.**
  Cross-batch skipped docs previously left no trace in `migration_log`
  to keep re-run disk growth bounded. 062 trades that for full
  traceability: the second run of a 1000-doc migration now writes
  1000 `S1_SKIPPED` rows. On repeated re-runs the tracking DB grows
  linearly; if that becomes an issue, a follow-up spec can add
  `cmcourier tracking prune --older-than ...`.
- The DETAIL tab picks up both new statuses automatically — the
  existing `list_docs_for_batch` query and the `render_detail`
  status + reason columns don't change.

### Notas

- `analyze batch <id>` and `cmcourier batch show <id>` also see the
  new rows for free — both already pivot on `status` from
  `migration_log`.
- `S1_FILTERED` synthetic txn_nums collide cleanly across runs via
  the `(rvabrep_txn_num, batch_id)` unique index — each batch gets at
  most one row per `(shortname, system_id)` combination, even if the
  same trigger appears multiple times.

---

## [0.63.0] — 2026-05-14 — **Guardia min_samples de AIMD: dejar de halvear con outlier-con-pocas-muestras**

The operator reported that the AIMD controller **always** issued a
`halve` a few seconds after the first chunk's S5 upload began —
reproducible, deterministic, every run.

### Corregido

- **AIMD halved on a single cold-connection outlier in the first
  chunk.** With nearest-rank p95 and N=5..6 samples, one upload that
  paid the TCP+TLS+JSESSIONID handshake (8-12 s) becomes the p95
  itself. Empirically: 5 normal 1.5 s uploads + 1 handshake 12 s →
  p95 = 12000 ms; with `target_p95_ms=6000` that crosses the 1.2×
  upper band and AIMD halves the pool. Subsequent chunks didn't
  suffer because the connections stayed warm and all samples were
  uniform. The bug was a property of the AIMD algorithm interacting
  with a small-sample regime — standard AIMD implementations gate on
  a minimum sample count for exactly this reason.
- A new `cmis.auto_tune.min_samples` config field (default **20**)
  gates the decision: `decide()` short-circuits to a new action
  `insufficient_data` when the recorder has fewer samples, leaving
  workers and timeout where they are. The `p95_provider` callback now
  returns `(p95_ms, sample_count)` so the count is read atomically
  with the percentile.
- The TUI treats `insufficient_data` like `warmup` — it does NOT
  promote to `last_decision`, so the "last move" line keeps showing
  the most recent real decision instead of a noisy transient.

### Notas

- `MetricsRecorder.current_stage_p95_with_count(stage)` is the new
  primitive — atomic `(p95, count)`. The single-value
  `current_stage_p95` stays for the TUI and the analyzer.
- The analyzer's reported p95 is unchanged. The percentile itself is
  correct; only the AIMD's *gate* on small samples needed a guard.
- 20 was picked empirically: it's small enough that AIMD still reacts
  quickly to genuine sustained load, large enough that a single
  30-second outlier among 19 normal 1.5 s samples cannot dominate the
  p95.

---

## [0.62.0] — 2026-05-14 — **Cliente HTTP migrado a httpx[http2]**

The 058 staging diagnosis pinned the bottleneck OUTSIDE the program — at
~1.5 s p50 per CMIS POST, with documents averaging ~76 KB. For workloads
shaped like that ("many small uploads in parallel"), the canonical
remedy is HTTP/2 multiplexing: instead of N TCP connections each
carrying one request at a time, one TCP connection carries all N
requests simultaneously. `requests` does not speak HTTP/2; `httpx`
does, behind the same sync API.

### Cambiado

- **`CmisUploader` now uses `httpx.Client(http2=True)`.** HTTP/2 is
  negotiated via ALPN at TLS handshake; if the server only advertises
  HTTP/1.1 (Tomcat-direct staging) httpx falls back transparently —
  same wire protocol, same behaviour as pre-060. The benefit kicks in
  against the Apache-fronted Alfresco in production, where the N
  concurrent S5 workers share a single TCP connection and small-upload
  overhead drops dramatically.
- The `requests.Session` / `HTTPAdapter` machinery is replaced with
  `httpx.Client` + `httpx.Limits(max_connections, max_keepalive_connections)`;
  `requests_toolbelt.MultipartEncoder` is replaced with httpx's native
  multipart API (`files=`, `data=`). The streaming property is
  preserved — the staged file is still read from disk on demand, never
  buffered whole. `BandwidthLimiter` passes through unchanged.
- The retry policy (5xx exponential backoff, 401 re-warmup, 4xx
  fail-fast, Windows-10053 doubled delay) is byte-identical. The
  `httpx.RequestError` family (ConnectError / ReadError /
  RemoteProtocolError / TimeoutException) covers every transport
  failure that previously surfaced as `requests.exceptions.ConnectionError`.
- The 56 adapter integration tests now run on `respx` (httpx-native
  HTTP mock) instead of `responses` (requests-only).
- **Dependencies:** removed `requests`, `requests-toolbelt`,
  `responses`, `types-requests`; added `httpx[http2]` (main) and
  `respx` (dev).

### Notas

- Public surface (`IUploader.upload`, `CmisConfig`, wiring layer) is
  unchanged. Every caller is byte-compatible.
- HTTP/2 server push and stream prioritization are NOT used — default
  httpx behaviour is sufficient.
- The orchestrator still uses `ThreadPoolExecutor`. `httpx.Client`
  (sync) is thread-safe and multiplexes naturally; no need to migrate
  to `AsyncClient`.

---

## [0.61.0] — 2026-05-14 — **Fixes de tab DETAIL: persistir metadatos de archivo staged + panel scrolleable**

Two bugs on the DETAIL tab (spec 052) the operator hit during a real
staging run — the `size` column always read `—`, and chunks bigger
than the visible height could not be scrolled.

### Corregido

- **The `file_size_bytes` of every doc was never persisted.**
  `_build_record` takes the metadata from `item.staged_file`, but
  that field is `None` until S4 finishes assembling — and the row is
  first inserted in S1 with `INSERT OR IGNORE`. So the initial INSERT
  wrote `None`, the S4 INSERT was silently ignored (the row already
  existed), and no later `UPDATE` ever touched those columns. The
  DETAIL tab's `_human_size(0)` therefore rendered `—` forever. Same
  fate for `source_file_path` and `page_count`. A new port method
  `ITrackingStore.record_staged_file_metadata(...)` UPDATEs the
  existing row with the assembler's output; `_s4_one` calls it after
  every successful assemble (outside the `is_stage_done` guard, so
  resume runs also backfill pre-061 rows).
- **The DETAIL pane was not scrollable.** Its body was a `Static`
  inside a plain `Container`, which crops overflow instead of
  scrolling. Spec 052 had truncated the render at `_MAX_ROWS = 100` as
  a workaround. 061 wraps the body in a `VerticalScroll` (with
  `#detail_body { height: auto }`), so the operator can scroll past
  the fold; `_MAX_ROWS` is raised to `2000` — well above the default
  `batch_size` of 1000 — and the `… N more — full list: cmcourier
  batch show ...` hint stays for genuinely huge chunks.

### Notas

- The PREP / UPLOAD / CHUNKS panes are fixed-size dashboards that fit
  on screen — they keep the existing `Static.tab_body { height: 1fr }`
  CSS and are not scrollable. Only the DETAIL pane needs it.
- `record_staged_file_metadata` is idempotent — rewriting the same
  values is a no-op, so the resume-backfill is safe.
- Rows written before 0.61.0 still show `—` in DETAIL until they are
  re-run through S4 (or backfilled with a one-shot SQL UPDATE).

---

## [0.60.0] — 2026-05-14 — **Dimensionar el thread pool de S5 al techo de AIMD**

The operator watched the UPLOAD tab's pool capacity climb — 4 → 8 → 12
— while "in use" stayed pinned at 4. The auto-tune knob was
disconnected from the engine.

### Corregido

- **The S5 `ThreadPoolExecutor` was sized to the initial
  `cmis.workers`, not the AIMD ceiling.** Upload concurrency is gated
  by two stacked limiters: the `ThreadPoolExecutor` (the *hard* limit
  — only that many threads physically exist) and the
  `ResizableSemaphore` acquired *inside* each worker (the *soft* limit
  AIMD resizes, up to `auto_tune.max_threads`, default 50).
  `_stage_5_single` and `_stage_5_dual` built their executors with
  `max_workers=self._workers` (`cmis.workers`, fixed at construction).
  So only `cmis.workers` threads ever reached the semaphore — however
  high AIMD lifted it, there were no threads for the extra slots.
  `pool_in_use` stayed pinned at the initial count while the TUI's
  capacity (read from the semaphore) climbed; `idle` grew
  fictitiously. The auto-tune machinery (025/043) had been steering a
  disconnected knob.
- A new `_pool_ceiling()` returns the real upper bound —
  `max(cmis.workers, auto_tune.max_threads)` when AIMD is enabled,
  `cmis.workers` when it is not — and both S5 executors
  (`_stage_5_single` + the heavy/light pair in `_stage_5_dual`) are now
  sized to it. The `ResizableSemaphore` / `LaneController` thus become
  the *effective* limiter, which is what the `ResizableSemaphore`
  docstring ("resize without tearing down the underlying
  ThreadPoolExecutor") and `_stage_5_dual`'s ("sized to the TOTAL
  worker budget") always intended. Surplus threads sit parked on the
  work queue at near-zero cost.

### Notas

- With AIMD disabled, `_pool_ceiling()` returns `cmis.workers` — the
  pre-060 value — so behaviour is byte-identical when auto-tune is off.
- The TUI needed no change: `data_provider` already reads
  `pool_capacity` from the semaphore and `pool_in_use` from
  `WorkerPoolStats.busy`. Once real threads exist, `pool_in_use` rises
  on its own.
- The test gap that let this ship: the AIMD tests (043) assert the
  *semaphore* resizes — never that real threads exist to use the extra
  capacity. 060 closes it by capturing the actual `max_workers` passed
  to every `cmcourier-s5*` `ThreadPoolExecutor` and asserting it is the
  ceiling.

---

## [0.59.0] — 2026-05-14 — **Workers de prep configurables: paralelizar S2/S3/S4**

The assembly stage (S4) was crawling on the TUI — and it turned out
`_stage_s4`, like `_stage_s2` and `_stage_s3`, was a plain serial
`for item in items:` loop, one document at a time on a single thread,
while S5 has run on an N-thread pool since spec 025.

### Agregado

- **`processing.prep_workers`** — a YAML knob for how many threads the
  prep stages **S2 (mapping), S3 (metadata), S4 (assembly)** may use.
  Each per-item body was extracted into a helper; a shared dispatch
  runs them serially when `prep_workers == 1` (the default —
  byte-identical to pre-059) or on a fixed `ThreadPoolExecutor` when
  `> 1`. `pool.map` preserves input order, so the survivor list stays
  deterministic regardless of completion order. Deliberately *not* the
  S5 machinery — no AIMD auto-tune, no heavy/light lanes, no bandwidth
  limiter, no TUI panel. Just a thread count.

### Notas

- **S0/S1 stay serial by design.** `_stage_s0_s1` carries the
  cross-batch idempotency, the `resume_scope`, and the
  `RVABREPDeletedError` filtering (051) — ordered, stateful work where
  parallelism stops being simple and adds real risk. It is also not
  the stage that hurts.
- The collaborators were already concurrency-safe: the tracking store
  is the same one S5's threads drive today (async writer queue +
  reader lock), and S3's `DocumentCacheService` + its
  `SqliteDocumentCache` adapter are both `threading.Lock`-guarded —
  verified, no new locks needed.
- S2/S3/S4 are I/O-bound (file copies, reading page images, metadata
  source I/O), so the GIL is released during the work and threads
  scale — no `ProcessPoolExecutor` needed. The practical ceiling is
  the storage backing `source_root` (local disk vs. a network share),
  not CPU.
- Regression gate: the 6-doc pipeline fixture — with failures targeted
  at S2, S3 and S4 individually — runs byte-identical at
  `prep_workers = 1` and `prep_workers = 4` (same per-stage failure
  counts, same `S5_DONE` rows), which proves ordering + no
  double-counting in one parametrized test.

---

## [0.58.0] — 2026-05-14 — **Los eventos de network llevan el batch_id: arreglar los handlers de bandwidth + slow-op**

The root cause behind the dead UPLOAD tab. Spec 054 fixed *which
recorder* the snapshot reads — but the operator re-ran and it was
still empty, because **no recorder was receiving the events at all**.

### Corregido

- **Every `cmis_upload` network event was silently dropped.**
  `CmisUploader._emit_network` built the log record's `extra` with
  `kind`, `duration_ms`, `size_bytes`, `worker`, `url_prefix` — but
  **never `batch_id`**, and `upload()` never even received one. Both
  metrics handlers filter on it: `_BandwidthHandler.emit` and
  `_SlowOpHandler.emit` short-circuit on
  `record.batch_id != self._batch_id`. With `record.batch_id` always
  `None`, **100% of upload bandwidth + slow-op events were discarded**
  — in every recorder, on every run, since spec 042 added the filter
  to `_BandwidthHandler` (and 028 to `_SlowOpHandler`) assuming the
  events carried an id they never did. So the UPLOAD tab showed
  `0.00 MB/s`, peak `0.00`, a blank sparkline, and "(none yet)" slow
  ops. Proven with a repro: the *same* event with vs. without a
  `batch_id` field → `peak_mbps` `8.0` vs `0.0`.
- The fix threads the chunk's `batch_id` down the upload path —
  `IUploader.upload(..., *, batch_id)` (required keyword) →
  `CmisUploader.upload` → `_post_with_retries` → `_emit_network`,
  which now sets `extra["batch_id"]`. `_emit_upload_attempt` /
  `_emit_upload_failed` carry it too, so the `s5_upload_attempt` /
  `s5_upload_failed` diagnostic events in `network-*.jsonl` become
  batch-attributable as well.

### Notas

- This is also the omission that left `network-*.jsonl` records
  without a `batch_id` — the gap spec 053 worked around by associating
  the network/system tiers via time window. With the id restored at
  the source, a follow-up could let `analyze` go back to an exact
  `batch_id` filter; that simplification is out of scope here (053's
  time-window path keeps working unchanged).
- 055 + 054 together fix the tab: 055 delivers the events to the
  per-chunk recorders, 054 makes the snapshot read the right one.
- The test gap that let this ship: `test_cmis_uploader.py` mocked HTTP
  but never attached a live `MetricsRecorder`, and the data-provider
  slow-op test hand-built an `extra` *with* `batch_id` — a shape the
  real uploader never produced. 055 adds a regression test that runs
  the real `CmisUploader.upload()` under a real
  `MetricsRecorder.start_batch()` and asserts the sampler + aggregator
  actually receive the bytes.

---

## [0.57.0] — 2026-05-14 — **Cableado del recorder de la tab UPLOAD: terminar el split de 042**

On an N=2 staging run the UPLOAD tab was dead: bandwidth `0.00 MB/s`,
peak `0.00 MB/s`, a blank UPLOAD SPEED sparkline, SLOW OPS
"(none yet)" — and the per-chunk timer counted from a point long
before S5 started. Two bugs, both incomplete fallout from 042.

### Corregido

- **Bandwidth / peak / sparkline / slow ops read the PREP recorder.**
  042 split the TUI's recorder binding (`recorder_provider` follows the
  most-recently-started chunk; `upload_recorder_provider` stays on the
  chunk inside S5) and moved `_current_chunk_progress` to the upload
  side — but four fields in `TUIDataProvider.snapshot()`
  (`bandwidth_current_mbps`, `bandwidth_peak_mbps`, `bandwidth_series`,
  `slow_ops_all`) were left reading `self._metrics`. During chunk N's
  upload, with chunk N+1 already in PREP, `self._metrics` is N+1's
  recorder — whose per-batch `_BandwidthHandler` / `_SlowOpHandler`
  filter out batch N's `cmis_upload` events. So all four read empty.
  They now read `self._upload_metrics` (which falls back to
  `self._metrics` for single-batch runs — unchanged there).
- **The per-chunk timer measured from PREP start.**
  `_current_chunk_progress` derived the active chunk's `elapsed_s` from
  `prep_started_monotonic` — so the UPLOAD tab's "chunk elapsed"
  counted from roughly program launch for chunk 0, and
  `current_chunk_avg_mbps` was diluted by the entire PREP phase. It now
  resolves `elapsed_s` by chunk status: `UPLOAD` measures from
  `upload_started_monotonic`, `DONE` uses the frozen `upload_elapsed_s`,
  `PREP` is `0.0`. Single-batch (no active chunk) is unchanged.

### Notas

- The gap that let both bugs ship: no `TUIDataProvider` test wired
  **divergent** PREP and UPLOAD recorders, so `_metrics` and
  `_upload_metrics` could never diverge under test. 054 adds that shape
  — two distinct `MetricsRecorder`s, one fed PREP data, one fed UPLOAD
  data — as the regression gate.

---

## [0.56.0] — 2026-05-14 — **Clasificador de bottleneck: consciente de stages + asociación de logs por ventana temporal**

`cmcourier analyze batch` is supposed to answer *where the time went*
and *whether the bottleneck is inside the program or outside it*. On a
real 95-doc staging run — S5 (upload) was **26× the next stage** — it
reported `under-utilized`. Exactly wrong.

### Corregido

- **The classifier ignored the stage breakdown.**
  `classify_bottleneck` *received* the per-stage timing but it was
  marked `# noqa: ARG001` and never read — so the single clearest,
  batch-exact bottleneck signal was discarded and the verdict fell
  back to system metrics that weren't even populated (see below).
- **Network & system records were never associated with the batch.**
  `LogReader` filtered the `network-*.jsonl` / `system-*.jsonl` tiers
  by `rec["batch_id"] == batch_id`, but those records carry **no**
  `batch_id` — only a timestamp. So `network_summary` came back empty
  and `system_summary` was `None` on every run. The reader now derives
  the batch window `[ts − elapsed_s, ts]` from the `batch_summary`
  record and associates the non-tagged tiers by timestamp (`ts` for
  network, `ts_iso` for system).
- **Absolute thresholds masked the obvious.** `network-bound` was dead
  whenever `cmis_max_bandwidth_mbps == 0` (the default); the
  `cmis_upload p95 > 5000 ms` fallback never fired for a run whose S5
  dominated 26× but whose p95 was "only" ~1.1 s.

### Cambiado

- **Classification is stage-led.** The per-stage breakdown is the
  PRIMARY signal: when one stage holds ≥ 45% of total stage time it
  *is* the bottleneck. The verdict names the stage and whether the
  bottleneck is **INSIDE** the program (ours to optimise) or
  **OUTSIDE** it (the CMIS server + network — the client can only push
  more concurrency): `S5 → upload-bound` (outside);
  `S4 → assembly-bound`, `S3 → metadata-bound`, `S2 → mapping-bound`,
  `S1 → indexing-bound`, `S0 → trigger-bound` (inside).
- **System metrics refine, they don't gate.** cpu/mem/disk/network
  signals are appended as corroborating reasons to the stage verdict.
  They only become the *classification* when no stage dominates.
- **`worker-saturated` is a symptom, not a cause.** It is reported as
  a reason line alongside the verdict; it never overrides a stage
  verdict, and even in the no-stage fallback a real resource cause
  outranks it. `under-utilized` is returned only when no stage
  dominates *and* no system signal fires.

### Notas

- Out of scope: tagging network/system records with a real `batch_id`
  (contextvar plumbing through the S5 worker pool). Time-window
  association is exact for single-batch runs; for **overlapped (N=2)**
  runs the windows overlap and a record in the overlap may be
  attributed to either batch — a documented limitation. The
  batch-tagged per-stage breakdown is unaffected and remains the
  primary signal.

---

## [0.55.0] — 2026-05-14 — **Tab CHUNKS: rates live, timer congelado, drill-down**

Three gaps the operator hit on the TUI during a `--total 2000`
staging run.

### Agregado

- **Per-chunk throughput in the CHUNKS tab.** A new `RATE MB/s·d/s`
  column shows each chunk's UPLOAD-phase throughput (and a TOTAL
  row). Zero `upload_elapsed_s` renders a dash — no divide-by-zero.
- **DETAIL tab — per-chunk drill-down.** `[` / `]` move a chunk
  cursor, `d` jumps to the tab, and the pane lists every doc of the
  selected chunk: `txn_num`, `file_name`, size, status, and the
  fail/skip reason. The detail is read from the SQLite tracking
  store **on demand** — never held in memory for every chunk, so
  spec 050's bounded-memory guarantee holds. New
  `ITrackingStore.list_docs_for_batch` + `DocDetail` model; the
  table caps at 100 rows and points at `cmcourier batch show` for
  the full list.

### Corregido

- **The run timer never stopped.** `TUIDataProvider` measured
  `elapsed` to `time.monotonic()` on every snapshot, so the footer
  clock counted up forever after the last chunk finished.
  `mark_batch_complete` now stamps a frozen completion time and
  `snapshot()` measures to it — the timer **stops** when the run
  does.

### Notas

- Out of scope: a mouse-clickable `DataTable` rewrite of the CHUNKS
  tab (the `Static` + `[`/`]` cursor is lower-risk and sufficient
  for a live dashboard), and full post-mortem of a finished run
  (still the CLI's job — `cmcourier batch show` / `inspect`).

---

## [0.54.0] — 2026-05-14 — **"Filtrado en S1" es un outcome de primera clase**

A `--total 2000` staging run showed S1 processing 1000 triggers per
chunk but only ~943 reaching S2–S5 — ~57 docs per chunk **vanished
with zero traceability**. Operator: *"necesito mirar qué pasa, no
simplemente que desaparezca."*

Root cause: `IndexingService._enrich_known_row` returned `[]`
**silently** when a `RvabrepRowTrigger` / `LocalScanTrigger` carried a
delete-coded RVABREP row — no count, no log, no surface. The doc was
neither `done`, nor `skipped` (cross-batch), nor `failed`: a fourth
outcome the pipeline had no name for.

### Corregido

- **Delete-coded RVABREP rows are no longer dropped silently at S1.**
  `_enrich_known_row` now raises `RVABREPDeletedError` (consistent
  with `find_documents`, the `ClientTrigger` path). `_stage_s0_s1`
  counts it as **filtered** — a first-class outcome — and emits one
  structured INFO log per doc with `reason="deleted_at_source"`.

### Cambiado

- **`RVABREPDeletedError` is now a *filter*, not a *failure*** — for
  **both** trigger paths. A doc deleted at source is correctly
  excluded; it is not a pipeline failure. (Pre-054 the `ClientTrigger`
  path counted it as an S1 failure.) `RVABREPNotFoundError` — a
  trigger pointing at a non-existent RVABREP row — stays an S1
  failure.
- **`s1_filtered` threads through the report types.** `RunReport`,
  `MultiBatchRunReport` (aggregate), and `ChunkState.prep_filtered`
  all carry the count. `prep_chunk` returns a 7-tuple
  `(items, skipped, s1_done, s1_filtered, s2_failed, s3_failed,
  s4_failed)`.
- **Surfaced everywhere the operator looks.** The headless run
  summary line gains `s1_filtered=N`; the TUI PREP tab gains a
  `FILTERED (S1, deleted at source)` line; the CHUNKS tab's per-chunk
  `PREP d/s/f` breakdown becomes `d/s/f/x` (x = filtered).

### Notas

- Out of scope: `DirectRvabrepTriggerStrategy.acquire`'s blank-row
  filter (it drops malformed rows in S0 *before* they become
  triggers — not the observed gap; it already logs a summary), and
  the interactive per-chunk file drill-down in the TUI (a larger
  feature). 054 delivers the counts + per-doc log.

---

## [0.53.0] — 2026-05-14 — **Pipeline trigger en streaming (memoria acotada)**

The bank's real RVABREP table is ~20 million rows. The pipeline
materialized the **entire** trigger set in RAM before doing any work
— it would OOM long before the first upload. The trigger strategies
are all generators (lazy by construction); that laziness was
**defeated downstream** at four points. 053 lets it flow through:
triggers stream in `batch_size` chunks, each chunk runs S0→S5, its
memory is released before the next chunk is pulled. Peak in-flight
memory is now `O(batch_size × batches_in_flight)`, not
`O(total triggers)`.

### Corregido

- **`MultiBatchOrchestrator._run_overlapped` (N=2)** —
  `triggers = list(acquire(...))` + `chunk_list = list(chunked(...))`
  + the upfront `range(len(chunk_list))` chunk-state seeding all
  materialized the full set. Now the trigger **iterator** flows
  straight into the (already-lazy) `chunked()` helper; each chunk's
  state is seeded the moment it is pulled.
- **`MultiBatchOrchestrator._run_single` → fresh N=1 runs** — the
  `batches_in_flight=1` path called the monolithic
  `StagedPipeline.run()`, which `list()`-ed the whole source. Fresh
  N=1 runs now route through a new `_run_sequential` path that
  streams chunk-by-chunk (the N=1 shape of `_run_overlapped`, no
  producer-consumer overlap). **Resume / `--from-stage > 1`** still
  use `StagedPipeline.run()` — that path operates on a
  previously-created, already-bounded batch.
- **`TabularDataSource.get_all`** — `to_dict(orient="records")` built
  a full Python list of every row's dict before yielding anything.
  Now iterates row by row via `itertuples` (lazy).
- **`--total N`** — `triggers[:N]` sliced *after* the full list was
  materialized. Now `itertools.islice` pulls exactly N from the
  iterator.

### Cambiado

- The N=2 CHUNKS tab no longer shows the full chunk plan upfront —
  chunks appear as they enter PREP. Knowing the total chunk count
  *is* materializing the total trigger set, which 053 exists to
  avoid.

### Notas

- **The 20M production migration runs against
  `indexing.source.kind: as400`** — the live AS400 RVABREP table,
  queried per-lookup. The AS400 source already streams
  (`query_stream` / `fetchmany`); 053 makes the orchestrator stop
  defeating that. The **CSV source (`TabularDataSource`) is
  in-memory by design** (its random-access `get_by_fields` lookups
  require the whole table indexed in RAM) — it stays the
  testing / small-bank tool, bounded-memory by design, not by 053.
- Known limitation: resume / `--from-stage > 1` still re-iterates
  the full source in S0 to reconstruct the trigger set before
  `resume_scope` filters it. Resume operates on a recovery batch
  (≤ `batch_size`), not the 20M happy path — driving resume
  triggers straight from the tracking DB is a follow-up.

---

## [0.52.0] — 2026-05-14 — **Nombres de columna NIARVILOG configurables**

The bank runs CMCourier against several AS400 environments whose
`NIARVILOG` coordination table carries the **same 15 columns under
different physical names** (and different library / table names).
RVABREP column names were already per-environment configurable
(`indexing.columns`, since 048's `IndexingColumnsModel`); NIARVILOG
was not — its 15 names were hard-coded in every SQL statement of
`as400_niarvilog.py`. 049 closes that gap.

### Agregado

- **`tracking.as400_sync.columns`** — a `NiarvilogColumnsModel`
  logical→physical column map, symmetric to `indexing.columns`.
  Defaults equal the canonical names (`SISCOD`, `TRNNUM`, `STSCOD`,
  …), so a config that omits the block behaves exactly as pre-049.
  Partial overrides are allowed — omitted fields keep the canonical
  default.

  ```yaml
  tracking:
    as400_sync:
      enabled: true
      library: MIBIB
      table: MININARVILOG
      connection: {host: 10.0.0.1}
      columns:
        status_column: ESTADO
        txn_num_column: NUMTRX
  ```

### Cambiado

- `As400NiarvilogStore` builds every `SELECT` / `UPDATE` / `INSERT`
  from a `NiarvilogColumns` value object instead of hard-coded
  identifiers. With default columns the emitted SQL is
  byte-identical to pre-049.

### Seguridad

- **Identifier validation.** NIARVILOG column / library / table
  names are string-interpolated into SQL (a SQL identifier can never
  be a `?` bind-param), so every configurable identifier is now
  validated at config-load time against the DB2-for-i ordinary
  identifier grammar (`^[A-Za-z@#$][A-Za-z0-9@#$_]{0,127}$`). This
  also closes a pre-049 latent gap: `tracking.as400_sync.library` /
  `.table` were interpolated into `_full_table()` **unvalidated**.

### Notas

- RVABREP column names are **not** affected — for the AS400 RVABREP
  source the operator supplies the full `query` and
  `IndexingColumnsModel` maps the *result-set* dict keys (pandas
  level, no SQL interpolation, no injection surface).

---

## [0.51.0] — 2026-05-14 — **Fuente RVABREP pluggable**

The `rvabrep-pipeline` and the old `as400-trigger-pipeline` were never
two different pipelines — they ran the *same* stages over the *same*
RVABREP-shaped table. The only real difference was **where the table
came from**: a CSV file simulating RVABREP, or a live SQL query against
the AS400. 048 makes that the only thing that varies.

### Cambiado

- **`indexing.source` is now a discriminated union** (`kind: csv` or
  `kind: as400`). The CSV variant carries `csv_path`; the AS400 variant
  carries a `connection` block plus a `query` that returns an
  RVABREP-shaped result set (JOINs / filters may be baked into the
  query — the pipeline only cares about the output columns). The
  pipeline wiring builds an `IDataSource` from whichever variant is
  configured; every downstream stage is unchanged.
- The `rvabrep-pipeline` command now serves **both** sources. Pick the
  source in config, not in the command name.

### Eliminado

- **`as400-trigger-pipeline` command** and the `trigger.kind: as400`
  config kind. AS400 is a *source* choice, not a *trigger* kind. Configs
  using `trigger.kind: as400` now fail fast at load time with a
  migration hint: set `trigger.kind: rvabrep` and
  `indexing.source.kind: as400` with `connection` + `query`.
- `As400TriggerConfig`, `As400TriggerStrategy`, and
  `services/triggers/as400.py`.

### Notas

- NIARVILOG (AS400-level idempotency tracking, 034) is untouched — it
  remains a separate concern from the RVABREP source.
- Clean break, no back-compat shim: the project is pre-production and
  the loader rejects the removed kind with an actionable error.

---

## [0.50.0] — 2026-05-14 — **Persistir cm_object_id en S5_DONE**

The §L.3 step of the validation checklist ("GET a doc by objectId,
pulling the OID from the tracking DB") was non-functional:
``migration_log.cm_object_id`` was ``NULL`` for every row even after
a successful upload.

### Corregido

- **``cm_object_id`` never reached SQLite.** The orchestrator's S5
  path assigned the CMIS objectId onto the in-memory ``_StageItem``
  (``item.cm_object_id = cm_object_id``) but ``mark_stage_done`` —
  the call that actually writes to ``migration_log`` — only updated
  ``status`` + ``completed_at``. The ``cm_object_id`` column existed
  in the schema and was written by ``mark_stage_pending`` (as
  ``None`` at S1_PENDING time), but nothing ever back-filled it.
  ``mark_stage_done`` now accepts a keyword-only ``cm_object_id``
  and persists it on the S5_DONE transition. The AS400 path was
  already correct (``IdempotencyCoordinator.mark_uploaded`` forwarded
  the OID to ``As400NiarvilogStore.OBJIDN``); 047 brings the SQLite
  ``migration_log`` to parity.

### Cambiado

- ``ITrackingStore.mark_stage_done`` signature gains keyword-only
  ``cm_object_id: str | None = None``. S1..S4 callers pass nothing
  and the column is left untouched (the ``None`` path is
  byte-identical to pre-047). ``IdempotencyCoordinator.mark_uploaded``
  and the orchestrator's S5_DONE call thread the real OID through.

### Notas

- Historical batches uploaded before 0.50.0 keep ``NULL``
  ``cm_object_id`` — not back-filled. The value is recoverable from
  Alfresco via a children-walk if ever needed.

---

## [0.49.0] — 2026-05-13 — **Modelo Trigger polimórfico**

Closes the deepest architectural mismatch caught during the validation
checklist sweep: the pre-046 ``TriggerRecord`` was a fixed 3-tuple
``(shortname, cif, system_id)`` that every S0 strategy had to produce,
forcing S1 to re-query RVABREP and re-expand to all docs of the
trigger's client — the wrong granularity for every pipeline kind
except csv-trigger.

The §E.4 finding ("local-scan pool of 100 files produced 1860 uploaded
docs") was originally diagnosed as a missing-dedup bug. It wasn't —
it was the trigger shape forcing semantic over-broad expansion. 046
brings each pipeline its natural trigger shape and S1 dispatches per
subtype.

### Agregado

- ``cmcourier.domain.models.Trigger`` abstract base + three concrete
  subtypes:
  - ``ClientTrigger(shortname, cif, system_id)`` — csv-trigger,
    single-doc, as400-trigger. S1 expands by RVABREP lookup.
  - ``RvabrepRowTrigger(row, col_*)`` — rvabrep-direct. The row is
    already known; S1 wraps it in one ``RVABREPDocument`` without a
    query.
  - ``LocalScanTrigger(file_path, row, col_*)`` — local-scan. The
    matched RVABREP row attaches at S0 acquire time; S1 emits exactly
    one ``RVABREPDocument`` per scanned file (no over-broad expansion).
- ``Trigger.audit_row()`` projection used by ``_build_record`` to fill
  the ``migration_log`` ``trigger_*`` columns best-effort, regardless
  of trigger shape.
- ``IndexingService.enrich(trigger)`` polymorphic dispatcher that
  pattern-matches on the trigger subtype.
- ``MetadataResolution.healed_cif: str | None`` — captured explicitly
  so the document_cache persists CIF without inspecting the trigger
  subtype.

### Cambiado

- **local-scan semantics**: a scan pool of N files now uploads exactly
  N docs (one per file). Pre-046 each file was inflated to "all docs
  of this file's client". Operators dropping files into ``scan_path``
  finally get the obvious behavior.
- **rvabrep-direct semantics**: yields one trigger per non-deleted
  RVABREP row. Pre-046 the strategy deduplicated by
  ``(shortname, system_id)`` and S1 re-expanded — wasted work and
  the wrong granularity for "process THIS row".
- ``TriggerRecord = ClientTrigger`` backward-compat alias. Every
  pre-046 import keeps compiling unchanged. csv-trigger + single-doc
  flows are byte-identical to pre-046.
- ``MetadataService.resolve`` accepts ``Trigger`` (was ``TriggerRecord``).
  CIF self-healing now centralizes through ``_trigger_cif(trigger)``
  helper.
- ``StagedPipeline._stage_s0_s1`` calls ``enrich`` instead of
  ``find_documents``.
- ``As400NiarvilogStore`` signatures accept ``Trigger`` and project
  the audit triple via ``audit_row()``. Behavior identical for
  ``ClientTrigger`` inputs (the only ones the coordinator sees in
  production today).

### Corregido

- §E.4 "over-broad upload expansion" in local-scan
  (catalogued as a doc finding during the checklist sweep). The fix
  is architectural — local-scan now uploads exactly the files in
  the scan pool.

### Fuera de scope (diferido)

- ``single-doc --txn-num`` for per-doc CLI runs. Future spec.
- ``as400-trigger`` shape change. The operator-defined SQL may project
  any column aliases, so the strategy stays as ``ClientTrigger``
  until production calls for a per-doc as400 mode.

---

## [0.48.0] — 2026-05-13 — **Upload S5 idempotente ante conflicto 409**

Closes the last gap surfaced by §H.1's kill-mid-S5 verification.
After 0.47.0 fixed the resume detection logic, a real
``kill -9`` between a successful CMIS HTTP 200 and our SQLite
``mark_stage_done`` commit left the doc in Alfresco but absent
from the migration log. On resume, the orchestrator retried the
upload; Alfresco's ``cmis:name`` uniqueness constraint rejected
the duplicate with HTTP 409; that retry landed as ``S5_FAILED``
in the migration log even though the doc was already where it
needed to be (we observed 4 such cases in the §H.1 live verify).

045 brings the same idempotent-409 contract that has covered
folder creation since 025 to the document upload
path: on 409 we look the object up by ``cmis:name`` and treat the
upload as successful if a match exists.

### Corregido

- **Kill-race idempotency for S5 uploads.** ``CmisUploader.upload``
  now recovers from 409 conflicts by listing the target folder's
  children and matching ``cmis:name``. When a match is found, the
  upload returns the existing ``cmis:objectId`` and the
  orchestrator marks ``S5_DONE`` normally. When no child matches,
  the 409 propagates as a real failure (the conflict was for some
  other reason — different name collision, server-side ACL).

### Agregado

- ``CmisUploader._lookup_existing_object_id(folder_url, name)`` —
  internal helper that GETs ``cmisselector=children`` and returns
  the matching ``cmis:objectId`` or ``None``. Uses the children
  endpoint rather than ``cmisselector=query`` so the lookup is
  immune to Solr indexing lag (which we observed flakily in 040 +
  041 verifications).
- Three new structured network events for operator auditability:
  - ``s5_upload_409_recovery_attempt`` — 409 received, lookup
    starting.
  - ``s5_upload_409_recovered`` — match found, upload counted as
    done with the recovered objectId.
  - ``s5_upload_409_recovery_failed`` — lookup transport error or
    no matching child; original CMISClientError propagates.
- ``JsonFormatter.ALLOWED_EXTRA_FIELDS`` extended with
  ``recovered_object_id`` and ``detail`` so the recovery events
  land in ``network-YYYY-MM-DD.jsonl`` with all their context.

### Nota operacional

Recovered docs count as ``s5_done`` in the report — there is no
new outcome enum. To audit which docs were recovered after a
crash, grep for ``s5_upload_409_recovered`` in
``network-YYYY-MM-DD.jsonl``; the event carries ``document_name``
and ``recovered_object_id``.

### Housekeeping

The autouse fixture in ``tests/unit/observability/test_setup.py``
(added in 041) was extended to also reset ``propagate=True`` on
``cmcourier.metrics.*`` child loggers after each test, eliminating
cross-test logger-state bleed that previously caused intermittent
``TestUploadPayloadTraceEvents`` failures when the full suite ran
in one ``pytest`` invocation. ``pytest tests/unit tests/integration``
is now 1114/1114 green.

---

## [0.47.0] — 2026-05-13 — **Resume robusto tras kill -9 en medio de S5**

Live §H.1 verification against staging caught a class of bugs in
``_apply_resume`` that made the documented resume flow non-functional
after a real crash:

- After ``kill -9`` during S5 uploads, the bulk of in-flight docs
  sat at status ``S4_DONE`` (waiting for a worker pool slot) rather
  than ``S5_PENDING``. The resume detector only scanned for
  ``FAILED`` and ``PENDING`` rows so it reported the batch as
  "clean" and exited 0, silently abandoning the second half of the
  batch.
- ``--batch-id`` was silently dropped from the orchestrator's
  kwargs whenever ``--resume`` was absent, so the documented
  ``--from-stage N --batch-id X`` replay path failed with the
  cryptic ``ValueError("from_stage > 1 requires batch_id")``.
- When ``--resume`` was paired with an explicit ``--from-stage``,
  the "is clean" early-exit fired BEFORE the explicit-override
  check, so an operator could not force a replay of an outwardly-
  complete batch.

### Corregido

- **Resume now detects ``S{N}_DONE → S{N+1}`` gaps.** For each
  stage N<5, if any doc is at ``S{N}_DONE`` with no failure or
  pending marker upstream, the resolved ``from_stage`` is ``N+1``.
  The §H.1 staging scenario (kill mid-S5 leaving 543 docs at
  S4_DONE + 281 at S5_DONE) now correctly resolves to
  ``from_stage=5`` and uploads the remaining 543 docs.
- **``--batch-id`` always threads to the orchestrator.** The
  ``if resume_flag else None`` conditional in
  ``cli/app.py:711`` is dropped — any operator-named batch_id is
  the literal batch_id this run operates on. When set, the run is
  routed through the single-batch path so the orchestrator does not
  override it with auto-generated per-chunk ids.
- **Explicit ``--from-stage`` always wins.** ``_apply_resume`` now
  honors a non-default ``--from-stage`` BEFORE attempting auto-
  detection or emitting the "is clean" exit. The operator gets the
  replay they asked for.

### Cambiado

- ``_apply_resume`` algorithm order: validate inputs → honor
  explicit ``--from-stage`` → auto-detect (FAILED/PENDING priority
  → ``DONE`` gap fallback) → clean exit. Previously: detect-first,
  override-second, which lost the override on clean batches.

### Adiciones de tests

- ``tests/unit/cli/test_apply_resume.py`` covers every branch of
  the rewritten ``_apply_resume`` (failed/pending priority,
  ``S{N}_DONE`` gap detection at every stage, explicit override
  beating clean, unknown batch handling, quiet suppression).
- ``tests/integration/cli/test_pipeline_kinds.py``'s
  ``_seed_resume_batch`` helper gains a
  ``completed_through_stage`` parameter so tests can stage truly-
  complete batches when the new gap detection would otherwise
  classify a partial seed as "needs resume".

### Nota operacional

If a previously-killed batch was abandoned as "clean" pre-0.47 and
the operator wants to recover it, simply re-run with the same
``--batch-id`` and ``--resume``. The gap detector picks up where
the kill left off. The kill-race window where Alfresco received a
doc but the migration_log commit was lost (4-10 docs in our
staging test) still produces 409 conflicts on resume — that fix
is tracked separately.

---

## [0.46.0] — 2026-05-13 — **El auto-tune AIMD ve p95 real en modo multi-batch**

Live verification of 0.45.0 against staging
(``--total 200 --batches-in-flight 2``, F.4 of the validation
checklist) caught a silent regression introduced by spec 028: the
AIMD controller's ``p95_provider`` was bound to the pipeline's own
``MetricsRecorder``, which in multi-batch mode receives no S5
events (each chunk uses its own recorder). The controller observed
``p95 = 0`` for the entire 19-minute run, kept incrementing the
worker pool every 15 s, and saturated at ``max_threads=16`` with
zero down-throttles. The pool's elastic-protection property was
effectively disabled.

Same architectural class as 042 #3 — a consumer reading from the
"default" recorder instead of the upload-active one. Fix is a
small, surgical wire-up: 043 introduces a swappable p95 source on
the controller and the orchestrator points it at
``upload_recorder()`` before starting the controller thread.

### Corregido

- **AIMD ignored S5 latency in multi-batch mode.** The pipeline's
  ``self._metrics`` recorder never sees S5 events when chunks have
  their own recorders. ``staged.py:255``'s
  ``p95_provider=lambda: self._metrics.current_stage_p95("S5")``
  always read 0 ⇒ controller never down-throttled ⇒ workers grew
  to the cap and stayed there regardless of real latency. The
  multi-batch orchestrator now overrides this binding to read from
  the upload-active recorder.

### Agregado

- ``AutoTuneController.set_p95_provider(provider)`` — swap the p95
  source after construction. Atomic reference replacement; takes
  effect on the next ``adjustment_interval_s`` tick without
  restarting the controller thread.
- ``MultiBatchOrchestrator._upload_p95_observer()`` — reads
  ``self.upload_recorder().current_stage_p95("S5")`` with a
  ``0.0`` fallback for the warmup window. Wired into
  ``_run_overlapped._upload_loop`` before ``controller.start()``.

### Nota operacional

Single-batch (``batches_in_flight=1``) behavior is unchanged —
the controller keeps reading from ``self._metrics`` which still
receives every S5 event on that path. The fix is multi-batch-only.

---

## [0.45.0] — 2026-05-13 — **Métricas del TUI: aislamiento por-chunk + contadores UPLOAD live**

Live verification of 0.44.0 against the testserver Alfresco with
``batches_in_flight=2`` surfaced three multi-batch overlap bugs the
041 unit tests could not catch. Each fix is small and isolated; no
public APIs change except ``_BandwidthHandler.__init__`` (private —
the constructor now requires a ``batch_id`` kwarg, mirroring the
existing ``_SlowOpHandler`` signature).

### Corregido

- **Bandwidth bleed across overlapping chunks.** Pre-042, the
  per-chunk ``MetricsRecorder._bandwidth`` sampler was fed by a
  ``_BandwidthHandler`` that filtered only by ``kind=="cmis_upload"``
  and not by ``batch_id``. With ``batches_in_flight=2`` two handlers
  were attached to ``cmcourier.metrics.network`` simultaneously, and
  every ``cmis_upload`` event incremented both samplers — chunk N+1's
  ``cumulative_bytes`` ended up containing chunk N's bytes too. The
  final TUI frame would show ``S5 UPLOAD ... 77.3 MB / 40.4 MB``
  (uploaded > planned, impossible if isolation worked). The
  handler now carries ``batch_id`` and short-circuits when
  ``record.batch_id != self._batch_id``, matching ``_SlowOpHandler``.
- **CHUNKS row UPLOAD column stuck at ``0/0/0`` mid-flight.**
  ``MultiBatchOrchestrator._update_chunk_state(status="UPLOAD")``
  never wrote the live ``s5_done`` / ``s5_failed`` counters —
  ``ChunkState`` only got the totals on the DONE transition. While
  S5 was running, the row showed zero counters for minutes. The
  data provider now reads live counters from the upload-active
  recorder when ``status == "UPLOAD"`` and substitutes them into
  the chunks-state dict. The frozen ``ChunkState`` values remain
  the source of truth for DONE / FAILED rows.
- **S5 percentiles bound to the wrong chunk during PREP overlap.**
  ``MultiBatchOrchestrator._active_recorder`` was a single slot
  written by both ``_prep_loop`` and ``_upload_loop``. When chunk
  N+1 entered PREP while chunk N was still uploading, the active
  slot flipped to N+1 (with zero S5 data yet) and the UPLOAD tab's
  percentile block read from the wrong recorder. The orchestrator
  now keeps a separate ``_upload_active_recorder`` slot exposed via
  ``upload_recorder()``; the UPLOAD-tab binding reads exclusively
  from this slot, leaving ``active_recorder()`` for the PREP-tab
  (which already had correct semantics).

### Agregado

- ``MetricsRecorder.record_upload_done()`` /
  ``record_upload_failed()`` + their ``upload_done_count()`` /
  ``upload_failed_count()`` getters. Mirrors the
  ``record_upload_skipped`` pair from 041 Phase 3. Wired into both
  ``_stage_5_single`` and ``_stage_5_dual``.
- ``MultiBatchOrchestrator.upload_recorder()`` callback.
- ``TUIDataProvider(upload_recorder_provider=...)`` kwarg.

### Nota operacional

No config changes. Existing staging configs work unchanged. The
fixes are observable: with the TUI on and ``batches_in_flight=2``,
the per-chunk MB ratio now stays within ``X ≤ Y`` and the CHUNKS
row's ``UPLOAD d/s/f`` ticks up live instead of jumping from 0/0/0
to its final value at the DONE transition.

---

## [0.44.0] — 2026-05-13 — **TUI: dashboard limpio + progreso MB + breakdown CHUNKS**

Three operator-visible TUI improvements surfaced during the
staging dry-run. None changes pipeline semantics — they all make
the live dashboard usable when a real chunked run is in flight.

### Agregado

- ``cmcourier.observability.setup.configure(..., tui_active: bool)``.
  When ``True`` (set by the CLI right before the Textual app
  starts) the stderr ``StreamHandler`` is **not** attached to the
  ``cmcourier`` logger, so log lines no longer tear the dashboard
  frame. The rotating ``FileHandler`` continues to receive every
  event — operators tail
  ``observability.log_dir/app-YYYY-MM-DD.log`` from a separate
  terminal to follow log output during a TUI run.
- ``TUISnapshot.current_chunk_bytes_uploaded /
  _bytes_total / _elapsed_s / _avg_mbps / _eta_s``. Drives the
  UPLOAD tab's new ``MB uploaded / MB planned`` segment + chunk
  timer line + naive linear ETA (hidden until > 5 % progress).
- ``MetricsRecorder.bandwidth.cumulative_bytes()`` — per-chunk
  cumulative S5 byte counter (the existing rolling window decays
  at 60 s and isn't suitable for a chunk-scoped MB display).
- ``MetricsRecorder.upload_skipped_count()`` and
  ``record_upload_skipped()`` — track S5 ``"skipped"`` outcomes
  (previously dropped on the floor). Surface on the CHUNKS tab as
  ``UPLOAD ... skip`` counts.
- ``ChunkState`` per-stage breakdown fields:
  ``doc_count``, ``total_bytes``, ``prep_done``, ``prep_skipped``,
  ``prep_failed``, ``upload_skipped``, ``prep_started_monotonic``,
  ``prep_elapsed_s``, ``upload_started_monotonic``,
  ``upload_elapsed_s``. The orchestrator freezes ``*_elapsed_s``
  when a chunk leaves the stage; the TUI computes live elapsed
  for in-flight stages from the matching ``*_started_monotonic``.

### Cambiado

- ``render_upload`` now puts the MB segment on the right of the
  progress bar (``S5 UPLOAD  ████░░  9 / 22 docs   127.3 MB / 312.8 MB``)
  and inserts a second line with chunk-scoped wall-clock + avg
  MB/s + ETA. The doc-count bar shape stays the same so operators
  who already read it that way are not jarred.
- ``render_chunks`` rewritten as a wider table with per-stage
  ``done/skip/fail (elapsed)`` cells and a ``TOTAL`` aggregate
  row. QUEUED rows render ``—/—/—`` in the stage columns to
  prevent zero-vs-not-yet confusion. Default width raised from
  76 → 92 to fit the breakdown comfortably.
- ``MultiBatchOrchestrator._update_chunk_state`` now takes the
  new field set as optional kwargs with "None means keep
  previous". Allows partial transitions (e.g. "we just entered
  PREP — record the start timestamp, leave totals at zero").

### Nota operacional

The TUI's main thread no longer competes with logging for the
terminal. If you need to watch app logs during a run, open a
second terminal and ``tail -F sample/logs/app-$(date +%F).log``.

---

## [0.43.0] — 2026-05-13 — **Compatibilidad CMIS Alfresco**

Closes the gap that prevented CMCourier from running end-to-end
against Alfresco Community 23.x. Four targeted fixes inside the
``CmisUploader`` + observability + doctor — the staging dry-run
now ships 0 failures from doctor through pipeline upload.

### Agregado

- ``CmisUploader._service_url(suffix)`` helper. When
  ``CmisConfig.repo_id`` is set, emits the IBM-CM path form
  ``{base}/{repo_id}/{suffix}``; when empty, emits the Alfresco
  form ``{base}/{suffix}`` without a doubled slash.
- New ``CmisConfig.repo_id`` semantics: empty string is now a
  first-class value meaning "the base_url already encodes the
  repository id" (Alfresco). Any non-empty string preserves the
  pre-040 IBM-CM behavior byte-for-byte.
- 7 new uploader tests: ``TestServiceUrl`` (4 unit cases) +
  ``TestAlfrescoStyleUrls`` (3 integration cases confirming
  emitted URLs contain no doubled slashes).

### Cambiado

- ``CmisUploader.test_connection`` unwraps Alfresco's wrapped
  ``repositoryInfo`` response (``{"<repo_id>": {...}}``) so the
  doctor ``cmis_connectivity`` check passes against both servers.
- ``CmisUploader._build_multipart_for_upload`` omits the explicit
  ``cmis:contentStreamMimeType`` property when ``repo_id=""``.
  Alfresco rejects that property as read-only (mime inferred from
  the multipart Content-Type); IBM CM requires it explicitly per
  the legacy ``cmis_services.py`` notes.
- ``JsonFormatter.ALLOWED_EXTRA_FIELDS`` extended with the 038
  payload trace fields (``event``, ``url``, ``object_type_id``,
  ``document_name``, ``mime_type``, ``content_bytes``,
  ``properties_json``, ``status_code``, ``response_body``,
  ``curl_equivalent``). Without this, ``s5_upload_attempt`` and
  ``s5_upload_failed`` events landed in ``metrics.jsonl`` with
  only ``ts/level/logger/msg`` — every diagnostic field promised
  by the 038 spec was silently dropped at serialization time.
  The caplog-based 038 tests passed because caplog reads
  ``record.__dict__`` before the formatter runs.
- ``doctor._check_cm_type_alignment`` now uses
  ``m.cmis_type or m.cm_object_type`` for the unique-types set.
  Mirror of the upload-time selection — without this, every
  ``CMISType`` override row was double-counted as the derived
  ``$t!-...v-1`` form, breaking the cm-targets pre-flight when
  the operator was using 035's override.
- ``scripts/staging/config-staging.yaml.template`` documents the
  IBM-CM-vs-Alfresco distinction inline on the ``cmis`` section.
- ``docs/how-to/local-staging-simulation.md`` uses ``repo_id: ""``
  in the example config.

### Verificación en vivo

Against the testserver Alfresco staging on Tailscale:

```
doctor                          → 9 PASS / 2 SKIP / 0 FAIL relevant
doctor --check cm-targets       → 3 PASS (types + folders + properties)
csv-trigger-pipeline run --total 5
  → 5 triggers / 107 docs / s5_done=26 / s5_failed=0 / 3.91s
27 docs queryable on Alfresco under /cmcourier-staging/CA* per Zipf.
```

See ``specs/040-alfresco-url-compat/`` for the full proposal.

---

## [0.42.0] — 2026-05-13 — **Generador sintético de CSV RVABREP**

Closes the scale gap between the 10-row hand-curated fixtures the
repo ships and the bank's real RVABREP exports. Operators can now
produce a deterministic CSV at any scale (100, 50 000, 1 000 000)
that chains directly into the existing ``mock generate`` (031) for
file-tree materialization.

### Agregado

- ``cmcourier mock rvabrep`` subcommand under the existing
  ``mock`` group. Flags: ``--rows``, ``--output``, ``--seed``,
  ``--idrvi-source``, ``--idrvi-top``, ``--image-mix``,
  ``--date-from``, ``--date-to``, ``--clients``,
  ``--delete-rate``, ``--cif-rate``. Defaults sized for a
  staging dry-run (50000 rows, 5000 clients, 5% delete rate,
  95% CIF presence, 20 IDRVIs).
- ``cmcourier.services.mock.rvabrep_generator`` — streaming
  generator (``csv.writer``-based, bounded memory for 1M rows),
  per-column pickers and a ``_validate_row`` invariant check
  that runs before each write.
- ``docs/how-to/mock-rvabrep-generator.md`` — operator runbook
  with the per-column rules, scaling characteristics, the
  chained ``mock generate`` flow, and the ``--idrvi-source``
  caveat against CMIS-target type registration.

### Forma de columnas

Output uses **ABA codes** (``ABABCD``, ``ABAANB``, ``ABAHCD``, ...)
to match ``IndexingColumnsModel`` defaults so the CSV is consumed
by ``mock generate`` and every downstream pipeline without a
config override.

### Reglas por columna

- ``ABABCD`` shortname: pool of ``--clients`` distinct identifiers
  from a banking lexicon + 2-digit suffix.
- ``ABAACD`` system_id: 70/15/10/5 mix of "1"/"5"/"2"/"3".
- ``ABAANB`` txn_num: deterministic 6-char base32 from row index
  (1G distinct values possible).
- ``ABACST`` delete_code: "D" with prob ``--delete-rate``.
- ``ABACCD`` index2 / CIF: one stable CIF per client; present with
  prob ``--cif-rate``.
- ``ABAHCD`` index7 / IDRVI: Zipf-weighted draw from the top
  ``--idrvi-top`` IDRVIs lex-sorted from the source CSV.
- ``ABABST`` image_type: B/O/C per ``--image-mix``.
- ``ABAJCD`` file_name: prefix letter aligned with image_type,
  random 7-char body, correct extension.
- ``ABAADT`` creation_date / ``ABABDT`` last_view_date: CYYMMDD,
  uniform in ``[--date-from, --date-to]``.
- ``ABABUN`` total_pages: 1 for PDF; for paged 70% [1,5], 25%
  [6,50], 5% [51,540].

### Performance

100 rows in < 0.5s; 50 000 rows in ~3s; 1 000 000 rows in ~50s on a
laptop. Streaming write keeps memory bounded — never materializes
the full dataset.

### Tests

14 unit cases + 4 integration scenarios = 1071 tests green.
Determinism asserted via byte-identical re-runs at the CLI level.
End-to-end chain into ``mock generate`` materializes physical files
without config overrides.

See ``specs/039-mock-rvabrep-generator/`` for the full proposal.

---

## [0.41.0] — 2026-05-13 — **Pre-flight de destino CMIS + trace de payload de upload**

Closes three gaps that previously surfaced as mid-batch S5 failures:

1. **Pre-flight gate on the CMIS target.** Two new doctor checks
   join the existing ``cm_type_alignment`` under a new
   ``cm-targets`` group — ``cmis_folders_exist`` verifies every
   ``CMISFolder`` declared in MapeoRVI_CM is a ``cmis:folder`` on
   the server, ``cmis_properties_alignment`` cross-references
   every ``(CMISType, CMISPropertyId)`` pair against the type's
   ``propertyDefinitions``.
2. **Folder-creation surface removed from the upload path.** The
   bank's CMIS administrators own the folder tree; CMCourier
   deposits documents only. ``IUploader.ensure_folder`` is
   replaced by ``IUploader.verify_folder_exists`` (read-only).
3. **Wire-level visibility on every upload attempt.** Every S5
   POST now writes an ``s5_upload_attempt`` event into
   ``metrics.jsonl`` (PII-masked). Failures add an
   ``s5_upload_failed`` event carrying the status code, truncated
   response body, and a runnable ``curl_equivalent``.

### Agregado

- Two optional columns of the split mapping CSVs are now consumed:
  - ``MapeoRVI_CM.CMISFolder`` → ``CMMapping.cmis_folder``. When
    set, overrides the derived ``cm_folder`` in S5's upload URL.
  - ``MetadatosCM.CMISPropertyId`` → ``CMMapping.cmis_property_ids``,
    a friendly-name → wire-level CMIS-id catalog. ``MetadataService.resolve``
    translates keys at emission, falling back to canonical for
    uncatalogued keys.
- ``cm-targets`` doctor group with three checks (existing
  ``cm_type_alignment`` + new ``cmis_folders_exist`` +
  ``cmis_properties_alignment``).
- ``s5_upload_attempt`` / ``s5_upload_failed`` structured events
  emitted by the uploader into ``cmcourier.metrics.network``.
- ``ObservabilityConfig.unmask_pii: bool = False`` — when ``true``,
  payload events emit raw values. Doctor emits a
  ``unmask_pii_active`` WARN at the top of every report while the
  flag is set.
- ``observability/pii.py`` gains ``is_pii_name`` and ``mask_dict``
  helpers covering wire-level CMIS property ids
  (``clbNonGroup.BAC_CIF``, ``cmcourier:Nombre_Cliente``).
- ``docs/how-to/cmis-target-preflight.md`` — operator runbook.

### Cambiado

- **BREAKING (port contract):** ``IUploader.ensure_folder(path) → None``
  is replaced by ``IUploader.verify_folder_exists(path) → bool``.
  Read-only — never creates a folder.
- ``CmisUploader.upload`` no longer calls ``ensure_folder``. S5
  trusts ``doctor --check cm-targets``; missing folders now surface
  through the 4xx + ``s5_upload_failed`` path.
- ``orchestrators/staged.py`` S5 URL builder consumes
  ``mapping.cmis_folder`` when set, ``mapping.cm_folder`` otherwise.

### Eliminado

- ``CmisUploader._create_folder_segment`` — folder creation was the
  only consumer and is now out of scope.
- ``CmisUploader._folder_cache`` / ``_folder_lock`` — no longer
  needed without on-demand creation.

### Fixtures de muestra

- ``docs/samples/csv/MapeoRVI_CM.csv`` gains a ``CMISFolder``
  column; the ``CN01`` row is populated with
  ``D:cmcourier:bacDoc`` + ``/cmcourier-staging/CN01`` as the
  staging exemplar.
- ``docs/samples/csv/MetadatosCM.csv`` gains a ``CMISPropertyId``
  column; the five ``CN01`` rows are populated with
  ``cmcourier:*`` property ids matching the custom Alfresco model.

### Tests

- 1053 unit + integration tests pass. mypy + ruff clean.

See ``specs/038-cmis-target-preflight/`` for the full proposal.

---

## [0.40.0] — 2026-05-11 — **Override de object_type_id CMIS + scaffolding de dry-run de staging**

S5 now uses ``mapping.cmis_type`` as the upload's
``object_type_id`` when that field is set (carried in from
``MapeoRVI_CM.CMISType`` since 035). When empty, falls back to the
existing derived ``cm_object_type`` pattern (``$t!-N_BAC_…v-1``).
Lets CMCourier upload against non-IBM-CM repositories — Alfresco
staging today, or a future bank type that doesn't match the
hardcoded pattern.

### Agregado

- ``scripts/staging/`` scaffolding for a self-contained
  Alfresco-in-Docker dry-run environment:
  - ``alfresco-compose.yml`` — Alfresco Community 23.x + Postgres
    + Solr + ActiveMQ.
  - ``cmcourier-model.xml`` — Alfresco Content Model declaring
    ``cmcourier:bacDoc`` + the metadata properties we emit (so
    Alfresco accepts the upload).
  - ``config-staging.yaml.template`` — full staging config with
    every knob commented.
  - ``README.md`` — quick reference.
- ``docs/how-to/staging-dry-run.md`` — generic 7-step runbook
  applicable to any CMIS staging (bank-provided or our simulation).
- ``docs/how-to/local-staging-simulation.md`` — runbook for the
  Alfresco-on-Compu-B setup specifically.

### Cambiado

- ``orchestrators/staged.py``: ``_stage_s5`` computes
  ``object_type_id = mapping.cmis_type or mapping.cm_object_type``
  before each upload.

### Compatibilidad hacia atrás

Empty ``cmis_type`` (the historical default) preserves the
pre-039 derived-type behavior byte-for-byte. Test fixtures that
omit ``CMISType`` from MapeoRVI_CM keep landing on the IBM CM
pattern. All 1000 pre-039 tests pass.

### Por qué no hay spec/ formal

Pure micro-op + documentation. The override is one line of
production code; the rest is operator runbook + container files.
See ``scripts/staging/README.md`` for the surface area.

---

## [0.39.0] — 2026-05-11 — **Sizing del connection pool CMIS + warm-up eager (POST-MVP §10.2)**

S5 stops paying the TCP + TLS + JSESSIONID handshake on the
critical path of the first N uploads. The CMIS uploader now:

- Mounts an explicit ``requests.adapters.HTTPAdapter`` with
  ``pool_connections`` and ``pool_maxsize`` matching the highest
  worker count the pipeline could reach (`cmis.workers` or
  `cmis.auto_tune.max_threads`, whichever is greater when AIMD is
  enabled). Replaces urllib3's default `pool_maxsize=10` which
  silently re-opens TCP every dispatch when workers > 10.
- Exposes ``CmisUploader.warm_connection_pool(n)`` — N concurrent
  ``repositoryInfo`` GETs that prime the pool with warm
  keep-alive connections + JSESSIONID cookies before the first S5
  upload submits.
- ``StagedPipeline.run()`` invokes the warmup right after the AIMD
  controller starts, so the first S5 batch ships against already-
  open connections instead of paying ~100-400 ms per worker on the
  TLS handshake.

### Agregado

- ``CmisConfig.pool_size: int = 10``.
- ``CmisUploader.warm_connection_pool(n) -> int`` — returns the
  number of successful warmups; individual failures only log.
- Structured log event ``cmis_pool_warmed`` with
  ``requested`` + ``succeeded`` counters.

### Cambiado

- ``CmisUploader.__init__`` configures ``HTTPAdapter`` on both
  ``http://`` and ``https://`` schemas with ``max_retries=0`` so
  our own retry policy stays authoritative.
- ``config/wiring.py`` derives the effective pool size from the
  config and passes it into ``CmisConfig``.

### Compatibilidad hacia atrás

Pool size defaults to 10 (the urllib3 baseline). Behavior is
strictly additive: configs that did not set ``cmis.workers`` to
more than 10 see no change. Warmup raises nothing — a cold pool
just means the original lazy-warmup path runs.

### Spec

Shipped without a formal `specs/` entry — pure micro-optimization
from the §10 watchlist (item 2: "Connection pool warm-up at
process start").

---

## [0.38.0] — 2026-05-11 — **Tabla cross-batch document_cache (POST-MVP §9)**

S3 (Metadata Resolution) gains an optional cross-batch cache so
re-runs of the same document skip the resolver and reuse previously
resolved properties + healed trigger CIF. Storage is a SQLite table
in the same DB as the tracking log. Default off — single-batch
behavior is byte-identical to pre-037.

### Agregado

- `MetadataCacheConfig` nested under `MetadataConfigModel.cache`:
  `enabled: bool = False`, `ttl_minutes: int = Field(default=60,
  gt=0, le=43200)` (cap: 30 days).
- `IDocumentCache` port + `CacheKey` / `CacheEntry` / `CacheStats`
  frozen dataclasses in `cmcourier.domain.ports`.
- `document_cache` table + `cached_at` index added to the SQLite
  schema migration (created unconditionally, idempotent).
- `SqliteDocumentCache` adapter (WAL, threading.Lock, JSON
  properties payload, ON CONFLICT upsert).
- `DocumentCacheService` (clock injection, TTL logic, in-memory
  hit / miss counters, structured `document_cache_hit` /
  `document_cache_miss` log events). Key derivation:
  `compute_fields_hash(fields)` = SHA-256 of the sorted comma-joined
  list. Mapping evolution invalidates by construction.
- `cmcourier cache` CLI group: `stats` (text/json) and `clear`
  (`--txn`, `--all`, `--older-than <minutes>`, exactly-one-of).
- `docs/how-to/document-cache.md` operator guide.

### Cambiado

- `StagedPipeline.__init__` gains optional `document_cache`. When
  set, `_stage_s3` consults the cache before
  `MetadataService.resolve`; on hit short-circuits + restores the
  healed CIF on the trigger; on miss runs the resolver and upserts.
- `config/wiring.py` builds the service iff
  `metadata.cache.enabled` and points `SqliteDocumentCache` at
  `tracking.db_path`.

### Compatibilidad hacia atrás

`metadata.cache.enabled = false` (the default) → cache reference is
`None`, S3 always invokes the resolver, and the `document_cache`
table stays empty. All 986 pre-037 tests keep passing.

### Fuera de scope (diferido)

- AS400-backed cache for §4 environments (single-host SQLite is
  enough until multi-host deployments demand otherwise).
- Partial-overlap reuse (sub-set of required fields counts as hit).
  All-or-nothing on `fields_hash` keeps the correctness story
  simple.
- Auto-vacuum / compaction. Operators rely on
  `cache clear --older-than` for housekeeping.

### Spec

- `specs/037-document-cache/`: spec.md, plan.md, tasks.md.

---

## [0.37.0] — 2026-05-11 — **Lanes adaptativos heavy / light de upload (POST-MVP §1)**

S5 gains an optional dual-lane mode that splits documents by size
and runs each lane on its own slice of the worker budget. AIMD owns
the TOTAL worker count; the new `LaneController` owns the heavy /
light split. A daemon thread migrates capacity to whichever lane has
work when the other has drained. **Default off** — single-lane
behavior is byte-identical to pre-036.

### Agregado

- `HeavyLightLanesConfig` block under `ProcessingConfig`:
  `enabled` (default `false`), `heavy_threshold_bytes` (10 MB),
  `heavy_lane_min_batch` (50), `heavy_initial_ratio` (0.2),
  `rebalance_interval_s` (10.0), `idle_threshold_s` (15.0).
- `services/lane_splitter.py`: pure `split()` function returning
  `LaneAssignment` (heavy / light / is_single_lane). Three exit
  rules: small batch, degenerate (all-heavy or all-light), bimodal.
- `services/lane_controller.py`: `LaneController` owning two
  `ResizableSemaphore`s + two `WorkerPoolStats`. `set_total_budget`
  is the AIMD hook (redistributes preserving the current ratio
  while keeping ≥ 1 per lane). Drain-driven rebalance daemon
  migrates ALL capacity to the active lane (drained side keeps the
  sem floor of 1 — harmless because no items mean no acquires).
  Each migration emits a structured `lane_rebalance` log event.
- `StagedPipeline.__init__` accepts `heavy_light_lanes`; when on
  and the splitter says not-single-lane, S5 dispatches through
  TWO `ThreadPoolExecutor`s (one per lane) — avoids the starvation
  that a single shared executor would suffer when threads block on
  the wrong semaphore.
- TUI `UPLOAD` tab swaps the single WORKERS panel for stacked
  HEAVY / LIGHT sub-panels when `lane_snapshot is not None`.
  Single-lane runs render byte-for-byte identical to pre-036.
- `docs/how-to/heavy-light-lanes.md`: operator guide with knob
  tuning hints, TUI / log expectations, and honest performance
  characterization.

### Cambiado

- AIMD `on_pool_resize` now dispatches between
  `concurrency_limit.set_capacity` (single-lane) and
  `lane_controller.set_total_budget` (dual-lane). AIMD's
  `current_workers_provider` reports the lane controller's total
  budget when dual mode is active.
- `_stage_s5` is now a thin dispatcher to `_stage_5_single` (legacy)
  or `_stage_5_dual` (036).

### Performance — contabilidad honesta

The POST-MVP §1 acceptance criterion wrote ≥ 30 % throughput. With
our actual implementation and a synthetic bimodal batch
(30 × 1 MB + 5 × 50 MB, `N=4` workers), dual-lane wins ~5-10 % of
wall-clock — the tail is set by heavy uploads either way. The real
operator-visible win is **per-doc latency**: light docs ship without
queueing behind a heavy slot. The slow integration test
`test_dual_lane_at_least_5pct_faster_than_single` asserts the
modest wall-clock improvement; production heuristics will be tuned
during the real-data dry-run phase.

### Compatibilidad hacia atrás

`heavy_light_lanes.enabled = false` (the default) preserves the
pre-036 S5 single-pool path byte-for-byte. All 944 pre-036 tests
keep passing. The shared `BandwidthLimiter` from 029 is reused
across both lanes — total bytes/sec stays under
`cmis.max_bandwidth_mbps` (covered by 029's
`test_throttles_via_shared_bucket`).

### Fuera de scope (diferido)

- Production tuning of `heavy_threshold_bytes`, `idle_threshold_s`,
  `heavy_initial_ratio`. Operator-tuned after the dry-run.
- TUI `notify()` flash on rebalance events. The structured log line
  + `cmcourier analyze` already cover post-mortem; live flash is
  cosmetic.
- Per-lane retry budgets. Both lanes share the existing CMIS retry
  policy (Tenacity).
- Per-lane bandwidth quota — that is POST-MVP §8, separate change.

### Spec

- `specs/036-heavy-light-lanes/`: spec.md, plan.md, tasks.md.

---

## [0.36.0] — 2026-05-11 — **Split de CSV de mapping (MapeoRVI_CM + MetadatosCM) + columna CMISType**

Aligns CMCourier with the bank's **production** Modelo Documental
format. `MappingConfig` now accepts either the legacy consolidated
CSV (`csv_path`) or the production split pair
(`rvi_cm_csv_path` + `metadatos_csv_path`). When operating in split
mode, the service joins `MapeoRVI_CM.csv` and `MetadatosCM.csv` by
`IDCM ↔ IDCorto` and populates `CMMapping.cmis_type` from the new
`CMISType` column. This unblocks the AS400 `NIARVILOG.TIPIDN` field
introduced in 034 (no longer always empty in production).

### Agregado

- `MappingConfig.rvi_cm_csv_path` + `metadatos_csv_path` +
  `model_validator` enforcing exactly-one-of with `csv_path`.
- `MappingConfig.cmis_type_column` exposed in the pydantic schema
  (gap left by 034).
- `MappingColumnsConfig` split-mode column-name fields with
  defaults matching the real bank headers (`IDRVI`, `IDCM`,
  `IDClaseDocumental`, `CMISType`, `IDCorto`, `Metadato`,
  `Requerido`) plus `required_marker = "Yes"`.
- `MappingService(source, columns, metadata_source=...)`: when
  `metadata_source` is set, the service runs the split-mode loader
  (join by `IDCM ↔ IDCorto`, filter `Requerido` truthy values
  case-insensitively, set `clase_name = clase_id`).
- `cmcourier.config.wiring.build_mapping_service(MappingConfig)` —
  single factory dispatching on mode and managing source
  open/close. Consumed by `wire_services_from_config`,
  `cli.doctor._check_mapping_completeness`,
  `cli.doctor._check_cm_type_alignment`,
  `cli.commands.inspect.inspect_mapping`,
  `cli.commands.inspect.inspect_mapping_stats`.
- `docs/samples/csv/MapeoRVI_CM.csv` gains the `CMISType` column
  (empty placeholder values — the bank fills these at deployment).

### Cambiado

- `MappingConfig.csv_path` becomes `FilePath | None` (was
  required) to allow the alternative split mode.
- `MappingService` no longer takes ownership of its sources'
  lifecycle in production paths — `build_mapping_service` closes
  them after the cache loads.
- `docs/how-to/as400-sync.md` `TIPIDN` row updated; the
  known-limitation note ("empty until 035 ships") removed.

### Compatibilidad hacia atrás

All 857 pre-035 tests keep passing. The legacy consolidated test
fixture `tests/fixtures/services/modelo_documental.csv` continues
to drive `MappingConfig(csv_path=...)`. The Java parallel
migrator's append-only read of `MapeoRVI_CM.csv` is preserved
(`CMISType` is added as a trailing column).

### Fuera de scope

- Reading the production `MapeoRVI_CM.csv` with `CMISType` values
  populated — the bank owns that file.
- Migrating test fixtures to split format. They stay consolidated
  to exercise the legacy mode.
- Changing `clase_name` representation in CLI output or logs —
  split mode uses `clase_id` (production CSV has no name column,
  confirmed by the bank).

### Spec

- `specs/035-mapping-csv-split/`: spec.md, plan.md, tasks.md.

---

## [0.35.0] — 2026-05-11 — **Idempotencia distribuida AS400 NIARVILOG (POST-MVP §4)**

Adds a toggleable distributed-idempotency layer on top of the
existing `SQLiteTrackingStore`. When
`tracking.as400_sync.enabled=true`, the pipeline coordinates
cross-batch idempotency with the bank's centralized
`RVILIB.NIARVILOG` table — enabling parallel-Java evaluation
and multi-workstation operation without double-upload risk.
When disabled (the default), behavior is byte-identical to
pre-034.

### Agregado

- **`tracking.as400_sync`** Pydantic block with the toggle +
  connection + retry policy. Cross-field validator: enabling
  the toggle without a connection raises `ValidationError`.
- **`As400NiarvilogStore`** (`adapters/tracking/as400_niarvilog.py`):
  atomic `try_claim` (UPDATE STSCOD='I' WHERE STSCOD='N' with
  INSERT fallback for first-time rows), `mark_uploaded`,
  `mark_failed`, `read_state` (full PK lookup),
  `read_state_by_txn` (TRNNUM-only for pre-flight + CLI),
  `mark_uploaded_by_txn` (for `--prefer-local` workflow),
  `cleanup_stale_in_progress`.
- **`IdempotencyCoordinator`** (`services/idempotency.py`):
  composes `SQLiteTrackingStore` (always) with
  `As400NiarvilogStore` (optional). Dispatches read/write
  per the documented rules:
  - `is_uploaded`: AS400 when active (`STSCOD='O'`), else
    SQLite.
  - `try_claim`: always `True` when AS400 disabled; atomic
    claim when active.
  - `mark_uploaded` / `mark_failed`: SQLite first (in-process
    resume anchor), then AS400 (operator-visible state).
  - `preflight_sync`: cleanup stale + reconcile each
    txn_num. Returns `SyncReport` with
    `imported_from_as400`, `conflicts`, `stale_cleaned`.
    Optionally raises `IdempotencyConflictError`.
- **`cmcourier sync` CLI** with two subcommands:
  - `cmcourier sync status` — read-only stale cleanup +
    connectivity check.
  - `cmcourier sync resolve <txn>
    --prefer-as400 | --prefer-local --cm-object-id <id>` —
    operator-driven resolution.
- **Doctor check** `as400_sync`: SKIPs when disabled,
  validates connection + table existence when enabled.
- **Retry / backoff** (`As400UnreachableError`): transient
  `pyodbc.OperationalError` triggers exponential backoff
  (`base, base*2, base*4, …` capped at 300s) for
  `retry_attempts` total. `IntegrityError` is never retried
  (race detection signal for `try_claim`).
- **Field mapping** (locked, documented in
  `docs/how-to/as400-sync.md`):
  - `SISCOD ← trigger.system_id`,
    `TRNNUM ← document.txn_num`,
    `DOCFRM ← document.index7` (= RVABREP ABAHCD),
    `IMGARC ← document.file_name` (first-page),
    `IMGTIP ← document.image_type`,
    `CTECIF ← trigger.shortname`,
    `CTENUM ← int(trigger.cif or 0)`,
    `STSCOD ← N/I/O/F` (state-machine derived),
    `IDNBAC ← mapping.id_corto` (= IDCM),
    `TIPIDN ← mapping.cmis_type` (populated from
    `MapeoRVI_CM.CMISType` in split mode — 035),
    `OBJIDN ← record.cm_object_id`,
    `NUMREI ← record.retry_count`,
    `EERRMSG ← record.error_message`.

### Cambiado

- **`CMMapping`** gains `cmis_type: str = ""` field. The
  mapping service reads `CMISType` column when present,
  defaults to empty string when not. Backwards-compatible
  with the consolidated test fixture.
- **`StagedPipeline.__init__`** accepts an optional
  `coordinator: IdempotencyCoordinator | None = None`
  parameter. When `None`, the pipeline runs the legacy
  SQLite-only path — byte-identical to pre-034. When set,
  `_upload_one` routes through the coordinator's
  `try_claim` / `mark_uploaded` / `mark_failed`.
- **`build_pipeline`** constructs the coordinator from the
  YAML's `tracking.as400_sync.enabled`.

### Tests

- 6 new schema tests covering defaults, ranges,
  cross-field validator, integration with `TrackingConfig`.
- 18 store tests including:
  - try_claim N-row update / INSERT fallback / race losing.
  - mark_uploaded ok + zero-rows warning.
  - mark_failed numrei increment + 1024 truncation.
  - read_state + read_state_by_txn present / absent.
  - cleanup_stale rowcount semantics.
  - Error wrapping (Coordination vs Unreachable).
  - 4 retry tests: transient retry succeeds, exhausted →
    Unreachable, IntegrityError not retried, backoff
    sequence respects base.
- 15 coordinator tests (disabled path, enabled path,
  preflight_sync three branches).
- 7 CLI sync tests (help, status, prefer-as400 happy +
  not-found, prefer-local happy + missing cm-object-id
  guard, mutually-exclusive flags).
- 1 doctor SKIP test for `as400_sync`.
- 1 CMMapping test for `cmis_type` default.
- **857 total green** (up from 829), mypy + ruff + format
  clean across the six phases.

### Documentación

- New `docs/how-to/as400-sync.md` with the full picture:
  when to enable, YAML snippet, field mapping table, status
  transition diagram, concurrency model, pre-flight
  reconciliation, conflict resolution playbook, retry
  semantics, known limitations.

### Notas

- **One row per txn**: per the bank's operational convention,
  NIARVILOG has at most one row per `TRNNUM` (the first
  page's `IMGARC`). Multi-page docs share a single row.
  Confirmed with the operator during spec.
- **`sync resolve --prefer-as400` doesn't write SQLite
  directly**. It prints the AS400 state; operator re-runs
  the pipeline with `--resume` so the in-process resume
  logic picks up `STSCOD='O'` and skips. Avoids extending
  `ITrackingStore` with a write-by-txn surface.
- **`sync resolve --prefer-local` requires
  `--cm-object-id`** explicit. Operator gets it from
  `cmcourier batch show`.

---

## [0.34.0] — 2026-05-11 — **Pulido de Tier 1: flag `--total` + docs de integración CI**

Two small operational ergonomics wins bundled into one change.
Closes the Tier 1 polish queue.

### Agregado

- **`--total <N>` flag** on every pipeline run command
  (`csv-trigger`, `rvabrep`, `as400-trigger`, `local-scan`,
  `single-doc`). Caps the number of triggers processed after
  the S0 acquire. Useful for validating a config + environment
  by running a tiny subset before the full migration.
  - Threaded through `StagedPipeline.run(..., total=N)` and
    `MultiBatchOrchestrator.run(..., total=N)`. Both N=1 and
    N=2 paths respect it uniformly.
  - `--total 0` rejected by Click's `IntRange(min=1)`.
  - `--total <larger-than-source>` is a no-op (no truncation).
- **CI / PR integration section** in
  `docs/how-to/log-analysis.md`. Covers minimum-viable
  regression check (bash `case` on `bottleneck.classification`),
  GitHub Actions and GitLab CI yaml templates, useful `jq`
  filters for throughput / p95 / slow-op extraction, exit-code
  contract for the analyzer, and known CI limitations
  (no real CMIS, small `--total` masks worker-saturation).

### Tests

- 5 new integration tests covering `--total`: caps N=1 path,
  caps N=2 multi-chunk path, larger-than-source is a no-op,
  zero rejected, `--help` lists the flag on every pipeline.
- 748 total green (up from 743), mypy + ruff clean.

### Notas

- Skipped version `0.32.0` reserved for the parallel change
  **031 mock-file-generator** developed on a separate branch.
- This change closes the Tier 1 (operator polish) queue. Next
  pending work needs real data (dry run staging) or external
  confirmation (§4 AS400 tracking pending bank decision).

---

## [0.33.0] — 2026-05-11 — **Auto-completion de shell (`cmcourier completion`)**

> Skips 0.32.0 — that version is reserved for the parallel
> change 031 (HTML report for `cmcourier analyze`) being
> developed on a separate branch.



The CLI surface area is now ~17 subcommands across 5 pipelines,
4 batch ops, 3 inspect targets, 3 analyze sub-modes, plus
doctor/background/as400-query. Tab-completion stops being a
nice-to-have and becomes a real DX win.

### Agregado

- **`cmcourier completion <bash|zsh|fish>`** subcommand. Emits
  the shell-completion script on stdout. Backed by Click's
  built-in :mod:`click.shell_completion` (auto-tracks every
  subcommand + option that ships in the future without
  maintenance).
- Install instructions documented in the new subcommand's
  docstring — one-line `eval` in `.bashrc`/`.zshrc`, or a
  redirect to `~/.config/fish/completions/cmcourier.fish` for
  fish.

### Tests

- 6 new CLI integration tests: every shell's script renders,
  unknown shells rejected by `click.Choice`, `--help` lists
  the subcommand and the supported shells.
- 743 total green (up from 737), mypy + ruff clean.

### Notas

- Zero impact on existing functionality — `cmcourier`
  invocations without `completion` behave identically.

---

## [0.31.0] — 2026-05-11 — **Vista multi-batch del TUI (tab `CHUNKS`)**

The producer-consumer overlap shipped in 028 had a UX caveat:
when `--tui` was enabled, the orchestrator forced
`batches_in_flight=1` because the TUI was tightly bound to a
single `MetricsRecorder`. 030 lifts that restriction. The TUI
now renders multi-batch runs faithfully and gains a third
**`CHUNKS`** tab that lists every chunk's state in real time.

### Agregado

- **`ChunkState`** dataclass and orchestrator-level state
  machine (`MultiBatchOrchestrator.chunks_snapshot()` +
  `MultiBatchOrchestrator.active_recorder()`). Each chunk
  transitions `QUEUED → PREP → UPLOAD → DONE` (or `FAILED`)
  with thread-safe state updates from the prep / upload
  worker threads.
- **`TUIDataProvider`** accepts an optional
  `recorder_provider` callable that returns the
  currently-active chunk's recorder. The provider's
  `_metrics` accessor live-binds to whatever the
  orchestrator says is "current" — PREP and UPLOAD tabs
  render coherent data as chunks transition.
- **`TUIDataProvider`** accepts an optional
  `chunks_provider` callable; `TUISnapshot.chunks_state`
  is the rendered list.
- **`CHUNKS` tab** (`cmcourier/tui/chunks_tab.py`,
  shortcut `[C]`): counts header + per-chunk row with
  index, batch_id, status glyph, s5_done, s5_failed.

### Cambiado

- `cli/app.py::_run_with_optional_tui` no longer forces
  `batches_in_flight=1` when `--tui` is on. `--resume`
  still forces N=1 (resume is inherently single-batch).
- `cli/_tui_runner` renamed `run_pipeline_with_tui` →
  `run_orchestrator_with_tui`. The worker thread now runs
  `orchestrator.run(**kwargs)` (returns
  `MultiBatchRunReport`).
- `TUIDataProvider.__init__` keeps its old positional
  surface — `metrics_recorder` is now the **fallback**
  recorder used when no `recorder_provider` is supplied.
  Pre-030 callers keep working without changes.

### Tests

- 4 new orchestrator state-machine tests (chunks_snapshot
  empty, after run, marks failed, active_recorder lifecycle).
- 5 new CHUNKS-tab render tests (empty placeholder,
  single-DONE, mixed states, FAILED counted, long batch_id
  truncated).
- 737 total green (up from 728), mypy + ruff clean.

### Notas

- Operator runs that pass `--tui --batches-in-flight 2` now
  get the multi-batch flow with live updates. Operators who
  prefer the single-batch view can pass
  `--batches-in-flight 1` explicitly.

---

## [0.30.1] — 2026-05-11 — **fix: `BandwidthLimiter` compartido (cap real enforzado)**

A latent bug surfaced by 025's concurrent S5 worker pool: the
pre-029 `BandwidthLimiter` was constructed **per upload call**,
so each worker thread had its own token bucket. With
`cmis.workers=4` and `cmis.max_bandwidth_mbps=100`, the
effective network ceiling was `~400 Mbps` — four times the
configured value. The configured cap was meaningless.

### Corregido

- **`TokenBucket`** extracted from `BandwidthLimiter` as a
  thread-safe, process-shared bucket. `CmisUploader.__init__`
  builds one bucket from `cfg.max_bandwidth_mbps` and reuses
  it for every upload. Concurrent `consume()` calls serialize
  on an internal lock so the configured rate is the **global**
  ceiling.
- **`BandwidthLimiter.__init__(stream, bucket)`** — the
  limiter is now a thin file-like wrapper that defers
  throttling to the shared bucket. No per-instance token math.
- **`cmcourier analyze`** `network-bound` heuristic is now
  meaningful: the comparison against `cmis.max_bandwidth_mbps`
  reflects an actual enforced ceiling.

### Tests

- New `TestTokenBucket` group (3 tests): zero-mbps no-op,
  single-thread throttle, **property test proving 4
  concurrent workers cannot exceed the cap** (`wall_elapsed
  > expected_at_global_rate`).
- Existing `TestBandwidthLimiter` adapted to the new
  `(stream, bucket)` constructor — behavior for single-stream
  cases unchanged.
- 727 total green (up from 724), mypy clean, ruff clean.

### Notas

- Not on the POST-MVP roadmap (it was a latent bug, not a
  feature). The roadmap §1 (heavy/light lanes) explicitly
  required this fix as a prerequisite — that work is now
  unblocked.

---

## [0.30.0] — 2026-05-11 — **Orquestador multi-batch (POST-MVP §7, N=2)**

The "siempre dos lotes en vuelo, uno preparándose y otro
cargándose" model from POST-MVP §7 — turns out it was never
implemented. The pre-028 `pipeline.run()` did S0→S5 in one
sequential pass over the full trigger source. 028 introduces
a producer-consumer orchestrator that chunks the source and
overlaps prep + upload of consecutive chunks.

### Agregado

- **`ProcessingConfig`** Pydantic block under
  `pipeline.processing` with `batches_in_flight: int = Field(
  default=2, ge=1, le=2)`. Top-level
  `pipeline.processing.batches_in_flight`.
- **`cmcourier.orchestrators.chunked`** — pure
  `chunked(items, size)` helper.
- **`cmcourier.orchestrators.multi_batch.MultiBatchOrchestrator`**
  — wraps a `StagedPipeline` and runs multiple chunks with
  producer-consumer overlap. For `N=1` it's a thin
  pass-through (byte-identical to pre-028). For `N=2` it
  spawns one prep thread (S0..S4) and one upload thread
  (S5) communicating via a bounded `queue.Queue`.
- **`MultiBatchRunReport`** dataclass — aggregates per-chunk
  `RunReport`s plus a `failed_chunks` list.
- **`--batches-in-flight <N>` CLI flag** on every pipeline run
  command. Defaults to `config.processing.batches_in_flight`.
  `--resume` and `--tui` both force `N=1`.
- **Per-chunk MetricsRecorder** — each chunk gets its own
  recorder so per-chunk `batch_summary` events + slow-ops
  files stay isolated. The shared S5 worker pool +
  AutoTuneController + tracking store are reused across
  chunks.

### Cambiado

- **`_SlowOpHandler`** now filters log records by
  `record.batch_id` so multiple concurrent
  MetricsRecorders don't cross-pollinate slow ops. Records
  without a `batch_id` extra are dropped.
- **Stage methods** (`_stage_s0_s1`, `_stage_s2..s5`) accept
  an optional `recorder` keyword so the orchestrator can
  route per-chunk timings to per-chunk recorders. Default
  remains `self._metrics` for the legacy single-batch path.
- **CLI output**: when more than one chunk runs, per-chunk
  lines + a TOTALS line. When one chunk runs (or `N=1`),
  the legacy single-line summary is preserved verbatim.

### Tests

- 6 new schema tests for `ProcessingConfig`.
- 8 new chunker unit tests.
- 3 new MetricsRecorder isolation tests (handlers filter by
  batch_id; bandwidth sampler still sees everything).
- 7 new orchestrator unit tests (N=1 pass-through, N=2
  overlap, wall-clock proof of overlap, exception isolation,
  N=3 rejection, empty source, resume forces N=1).
- 5 new CLI integration tests covering `--batches-in-flight`.
- 724 total green (up from 695 in 027), mypy clean, ruff
  clean.

### Documentación

- New `docs/how-to/multi-batch.md` with the
  producer-consumer model, output format, failure
  semantics, and memory-budgeting guidance.

### Notas

- **N > 2 deferred**. The original POST-MVP §7 spec listed
  N up to 5. Supporting N>2 requires per-chunk shared-pool
  semantics for the S5 ResizableSemaphore + AutoTune
  controller that would significantly inflate this change.
  Documented as a future change.
- **TUI multi-batch view deferred**. The TUI currently
  shows one batch at a time. When `--tui` is on, the
  orchestrator forces `N=1` so the operator's view stays
  coherent.

---

## [0.29.0] — 2026-05-11 — **Analizador de logs offline (POST-MVP §3)**

Closes the second-half of the §17.4 story: now that tier 5 is
on disk (026), operators have a first-class way to *read* it.
The `cmcourier analyze` subcommand suite consumes the five
log tiers and produces per-batch reports, pairwise deltas, and
trend series — all deterministic, all read-only.

### Agregado

- **`cmcourier analyze batch <batch_id>`** — full per-batch
  report: header, per-stage table (count/p50/p95/p99),
  network table (per kind), system table (when tier 5 is
  available), top-5 slow ops, and a bottleneck verdict line
  with confidence + reasoning.
- **`cmcourier analyze compare <a> <b>`** — side-by-side
  delta: throughput delta, elapsed delta, per-stage p95
  delta, and a one-line bottleneck-class comparison.
- **`cmcourier analyze trends [--last N] [--pipeline <name>]`**
  — throughput + S5 p95 over the last N `batch_summary`
  events, optionally filtered by pipeline. Default `--last 10`.
- **`--format text|json`** on every subcommand. JSON is
  deterministic (sorted keys, 2-space indent, no embedded
  timestamps).
- **`--config <path>`** or **`--log-dir <path>`**: read
  from a YAML (to derive `log_dir` + `cmis.max_bandwidth_mbps`
  + worker count for the classifier) or skip the YAML and
  read raw.
- **`cmcourier.services.analyze`** module exposing
  `LogReader`, `BatchReport`, `BottleneckClassification`,
  `NetworkSummary`, `SystemSummary`, `CompareReport`,
  `TrendRow`, `build_batch_report`, `classify_bottleneck`,
  `compare_batches`, `compute_trends`, and the six
  formatter functions. All pure, all importable as a library.
- **Bottleneck classifier** with five classes
  (`cpu-bound`, `memory-bound`, `disk-bound`,
  `network-bound`, `worker-saturated`) + an `under-utilized`
  fallback. Rules + thresholds documented in
  `docs/how-to/log-analysis.md`.
- **Resilient JSONL reader** — malformed lines are logged
  WARNING and skipped; missing files yield empty record
  lists; cross-midnight rotated files are merged
  transparently by glob.

### Tests

- 16 new unit tests for `LogReader`, `classify_bottleneck`,
  and `build_batch_report` (tier reads + each bottleneck
  class + tie-break + no-samples fallback + aggregation).
- 7 new CLI integration tests covering every subcommand
  (text + JSON, deterministic output, trends filter, compare
  delta).
- 695 total passing (up from 672 in 026).

### Documentación

- New `docs/how-to/log-analysis.md` — when to use each
  subcommand, full bottleneck-rule table with thresholds,
  sample terminal output, and an operator playbook
  ("did doubling workers actually help?", "are we drifting
  over time?").

### Notas

- HTML report rendering listed in the POST-MVP §3
  acceptance criteria was explicitly **deferred** to a
  future follow-up. The current text + JSON pair is enough
  for terminal + CI + jq workflows.
- The analyzer is read-only — it never touches the
  pipeline's running state, the tracking SQLite, or any
  remote service. Safe to run mid-batch.

---

## [0.28.0] — 2026-05-11 — **Nivel-5 de métricas de sistema (POST-MVP §2)**

Closes the last `psutil`-shaped gap on the §17.4 observability
surface. When a pipeline runs, a daemon thread snapshots
host- and process-level metrics every 5 seconds (configurable)
and appends one JSON line per sample to
`./logs/system-{date}.jsonl`. This is the data input that
unblocks the offline log analyzer (POST-MVP §3) and lets us
validate the AIMD target the 025 auto-tune controller assumes.

### Agregado

- **`SystemMetricsSampler`** in
  `cmcourier/observability/system_metrics.py`. Daemon
  `cmcourier-syssampler` thread. Idempotent `start()` /
  `stop()`. First-sample delta fields are `0.0` (no baseline
  yet); subsequent samples compute MB/s from byte counters.
  Errors from `psutil` are caught, logged WARNING, and
  skipped — the thread never dies.
- **`SystemSample` dataclass** with the full tier-5 field
  set: `ts_iso`, `cpu_pct`, `ram_used_mb`, `ram_total_mb`,
  `disk_read_mbps`, `disk_write_mbps`, `net_in_mbps`,
  `net_out_mbps`, `process_pid`, `process_threads`,
  `process_cpu_pct`, `process_rss_mb`, and `active_workers`
  (live from `WorkerPoolStats.snapshot().busy`).
- **`SystemMetricsConfig`** Pydantic model under
  `observability.system_metrics`: `enabled: bool = True`,
  `sample_interval_s: float = 5.0` (range 1.0–60.0). The
  `_STRICT` model enforces extra-forbid like every other
  config block.
- **Legacy-bool coercion**: pre-026 YAMLs that wrote
  `observability.system_metrics: false` keep loading
  (`field_validator(mode="before")` lifts the bool into
  `{"enabled": <bool>}`).
- **Pipeline lifecycle hook**: `StagedPipeline` accepts a
  `sampler` kwarg, late-binds it to the worker pool stats,
  starts it in `run(...)`, and stops it in a `finally:`
  block so pipeline exceptions never leak the thread.
- **`build_sampler(observability_cfg, log_dir)`** factory in
  `observability.system_metrics`. Returns `None` when
  disabled; constructed (not started) sampler otherwise.

### Cambiado

- `ObservabilityConfig.system_metrics` switches from
  `bool = False` to a nested `SystemMetricsConfig` model.
  The pre-026 rejection validator (`_reject_system_metrics`)
  is removed.
- `config/wiring.py::build_pipeline` builds the sampler from
  the observability config and threads it into
  `StagedPipeline(sampler=...)`.

### Tests

- 6 new schema tests (REQ-004): structured-true,
  structured-false, structured-custom-interval, legacy
  bool-false coerced, legacy bool-true coerced, interval
  out-of-range rejected, unknown-field rejected.
- 10 new sampler unit tests (REQ-017): disabled→no-op,
  start/stop idempotent, first sample has zero deltas,
  second sample computes deltas correctly with patched
  psutil counters, `active_workers` propagation
  (None + WorkerPoolStats), late-binding via
  `attach_pool_stats`, JSONL write to today's file.
- 2 new integration tests (REQ-018): full
  `csv-trigger-pipeline` produces `system-<today>.jsonl`
  with valid JSON lines; `enabled: false` skips the
  sampler entirely.
- 672 tests total green (up from 655 in 025).

### Performance

- **Measured cost**: +0.10% CPU at the default 5 s interval
  over a 60 s window on the dev workstation (12 samples
  written, ≈1 sample/5 s). Spec target was <1%.

### Dependencias

- New runtime dep: `psutil>=5.9,<7.0`.
- New mypy stub dep: `types-psutil>=5.9,<7.0` in
  `.pre-commit-config.yaml`.

---

## [0.27.0] — 2026-05-10 — **TUI live + worker pool S5 + auto-tune AIMD**

The S5 (CMIS upload) stage moves from a sequential loop to a real
`ThreadPoolExecutor` worker pool, gains a textual two-tab live
TUI, and grows an AIMD (Additive-Increase / Multiplicative-
Decrease) auto-tune controller. This is the "TUI by default"
commitment realized end-to-end.

### Agregado

- **`ThreadPoolExecutor`-based S5** in `StagedPipeline._stage_s5`.
  The pool size comes from `cmis.workers` (default 4, range
  1..32). Each task acquires a `ResizableSemaphore` slot before
  uploading, so the AIMD controller can raise/lower the *active*
  cap without draining the pool.
- **`AutoTuneController`** (`services/auto_tune.py`). Runs on a
  daemon thread, polls the recorder's `current_stage_p95("S5")`
  every `cmis.auto_tune.interval_s` seconds, and applies AIMD:
  observed p95 < target → +1 worker; observed p95 > target →
  `*0.5` workers + bump upload timeout; in-band → noop. Honors
  a warmup window so the first decision waits for stable
  measurements. All decisions are logged with structured extras
  (`workers_before/after`, `timeout_before_s/after_s`,
  `p95_observed_ms`, `p95_target_ms`, `action`).
- **Textual two-tab TUI** (`src/cmcourier/tui/`). PREP tab shows
  S0..S4 progress bars + slow-op listings. UPLOAD tab shows S5
  progress, a WORKERS panel (capacity/in-use/idle/timeout/
  last-move/next-tick), a NETWORK panel + 60-bucket 1Hz
  bandwidth sparkline (y-axis 0 → `cmis.max_bandwidth_mbps`,
  auto-scale when ceiling is 0), and a RUN COMPLETE overlay.
  Tabs are switched with `[P]`/`[U]`; `[Q]` exits.
- **`--tui / --no-tui` CLI flag** on every pipeline run command
  (`csv-trigger`, `rvabrep`, `as400-trigger`, `local-scan`,
  `single-doc`). Default `tui=True`. When stderr is not a TTY
  (cron, CI, pytest), the TUI auto-disables silently. An
  *explicit* `--tui` in a non-TTY context exits **2** with a
  clear `ConfigurationError`. The `background` command does not
  accept `--tui` — unattended runs are always headless.
- **Worker label in network events** (`worker` field, e.g.
  `cmcourier-s5_3`). Whitelisted in
  `observability/formatter.py::ALLOWED_EXTRA_FIELDS` and
  surfaced in the TUI's slow-op rows.
- **`auto_tune` config block** (Pydantic-validated). Fields:
  `enabled`, `target_p95_ms`, `tolerance_ms`, `interval_s`,
  `warmup_s`, `min_workers`, `max_workers`,
  `min_timeout_s`, `max_timeout_s`. Cross-field validation
  enforces `min_workers ≤ max_workers` and
  `min_timeout_s ≤ max_timeout_s`.

### Cambiado

- **`CmisUploader._timeout_s` is now mutable** so the auto-tune
  controller can adjust the upload timeout. `CmisConfig` stays
  frozen — the per-instance override happens in the uploader.
- **Thread-safety on the hot path**: `MetricsRecorder._StageBucket`
  and `SlowOpAggregator._candidates` now hold a `threading.Lock`;
  `SQLiteTrackingStore` opens with `check_same_thread=False` and
  serializes reads through `_reader_lock`. `CmisUploader` gains
  `_folder_lock` + `_warm_lock` so concurrent workers can't
  double-warm or double-mkfolder.
- **Circular import broken**: `cmcourier/config/__init__.py` now
  resolves `build_pipeline` via a lazy `__getattr__` so
  `orchestrators.staged` can import the observability stack
  without re-entering config wiring.

### Tests

- 12 new unit tests for `WorkerPoolStats` + `ResizableSemaphore`.
- 10 new unit tests for the AIMD `decide()` function +
  `AutoTuneController`.
- 25 new TUI tests (chart sparkline, data provider, both tabs).
- 7 new integration tests for the S5 worker pool end-to-end.
- 4 new CLI tests for `--tui` / `--no-tui` semantics including
  the explicit-tui-in-non-TTY → exit 2 branch.
- 655 tests total green, mypy clean, ruff clean.

### Notas

- Slow / fast S5 lanes remain explicitly post-MVP —
  they aren't in 025 by design. The current
  pool is a single resizable pool sized by `cmis.workers`.
- The bandwidth chart uses the operator-configured
  `cmis.max_bandwidth_mbps` rather than an autodetected
  interface speed. Honest and fragile-detection-free.

---

## [0.26.0] — 2026-05-10 — **Runner background**

Cron-friendly entry point for unattended pipeline execution.
Closes the last operationally-meaningful CLI gap ahead of the
real dry run.

### Agregado

- **`cmcourier background --pipeline <kind>`** — single
  dispatcher for unattended execution. Accepts the four
  production pipelines (`csv-trigger`, `rvabrep`,
  `as400-trigger`, `local-scan`); `single-doc` is intentionally
  rejected by Click's `Choice` (it's an ad-hoc tool, not a
  cron use case).
- **Per-config exclusive lock** via
  `cmcourier.cli.commands._lock.acquire_config_lock`. Lock file
  lives at `${XDG_RUNTIME_DIR:-/tmp}/cmcourier/<sha256(config_path)[:12]>.lock`.
  `fcntl.flock(fd, LOCK_EX | LOCK_NB)` — non-blocking. Second
  invocation on the same config exits **75** (`os.EX_TEMPFAIL`,
  cron-conventional "transient, retry later") and emits a
  WARNING `background_lock_held` log line.
- **`LockHeldError` exception** — raised by
  `acquire_config_lock` on contention. Carries the lock path
  for diagnostics. Released by the kernel on process exit
  including `SIGKILL` (fd-close semantics).
- **Quiet-on-success output**. The background runner suppresses
  the `_emit_summary` stdout line on success — only the
  structured observability tiers record the run. Cron stays
  silent on green; the operator's mailer only fires when
  something is wrong.
- **Failure stderr summary**. On `report.s5_failed > 0`,
  emits a single line:
  `pipeline=<kind> batch_id=<id> s5_failed=<n> exit_code=1`.
  Cron forwards this to the operator.
- **`--log-level WARNING` default** (interactive runs default
  to `INFO`). Same WARNING threshold as the rest of cron-aware
  Unix tooling.
- **5 background integration tests** + **9 unit tests** for
  the lock module:
  - Lock unit tests cover: roundtrip release, contention
    raises, deterministic path, XDG / /tmp fallback, PID +
    timestamp content, low-level fcntl semantics.
  - Background CLI tests cover: help lists flags, unknown
    pipeline rejected, quiet success, lock contention exits
    75, lock released after run.

### Cambiado

- **`_run_pipeline_command` in `cli/app.py`** gains a
  keyword-only `quiet: bool = False`. The interactive
  pipelines pass `quiet=False` by default (unchanged
  behavior); `background_command` passes `quiet=True`.
- **`_apply_resume`** gains `quiet: bool = False` for
  symmetry: when set, the "Nothing to resume" stdout echo is
  suppressed (still exits 0).
- **`cli/app.py`** registers the new `background_command` via
  `main.add_command(background_command)` next to the other
  top-level commands.

### Verificación

- `pytest --cov`: **587 / 587 pass** in ~100 s (+14 net new).
- Coverage: total **94 %**;
  `cli/commands/_lock.py` at **100 %**,
  `cli/commands/background.py` at **100 %**.
- `ruff check` / `ruff format --check`: clean.
- `mypy src/cmcourier`: clean (50 source files).
- `pre-commit run --all-files`: ruff + ruff format + mypy all
  pass.
- Smoke: `cmcourier --help` lists `background` next to the
  existing 9 commands. `cmcourier background --help` lists
  every flag.

### Justificación

Until 024, the only way to schedule a CMCourier pipeline run
was to call `csv-trigger-pipeline run` (or one of its
siblings) from cron. That worked but leaked two problems:

1. **No instance lock.** Two overlapping cron runs would race
   on the tracking store. SQLite WAL keeps rows correct but
   the batch lifecycle (`start_batch` / `mark_stage_*` /
   `complete_batch`) interleaves badly enough to corrupt
   per-stage counts. The kernel-enforced flock guarantees
   only one runner per config at a time. Second runner exits
   immediately with `EX_TEMPFAIL` (75) — cron's
   `MAILTO=...` doesn't fire (success), cron's retry
   semantics resume on the next tick.
2. **Stdout chatter on success.** Cron emails on any
   stdout/stderr output by default. The interactive command
   prints a one-line `s5_done=N` summary on every successful
   run. With a daily cron that's a spam email a day. The
   structured logs (app log + metrics + slow-ops) already
   capture everything an operator needs — terminal output
   adds zero value to unattended runs.

**Architectural decisions:**

1. *fcntl over PID files.* PID files are operator-overrideable
   (`echo 0 > /var/run/cmcourier.pid`) and leak on SIGKILL.
   `fcntl.flock` is kernel-enforced and released
   automatically when the fd closes — including on `SIGKILL`.
   The lock file does store the PID + ISO timestamp for
   debugging, but operators MUST NOT use it for process
   control (the flock is authoritative).
2. *Per-config locks, not per-host.* Two configs targeting
   the same tracking store would still collide; that's an
   operator misconfiguration, not a runner bug. Lock keyed
   on `sha256(config_path.resolve())[:12]` means two
   invocations on the same config file collide
   deterministically.
3. *Reuse over reinvention.* `background_command` doesn't
   reimplement pipeline orchestration — it acquires the
   lock, then dispatches into `_run_pipeline_command` (the
   same helper the interactive commands use), with
   `quiet=True`. Auto-doctor + `--resume` work identically.
4. *Single-doc not supported.* `single-doc` requires
   `--shortname`/`--system`/`--cif` per invocation — that's
   ad-hoc, not scheduled. Click's `Choice` rejects it
   explicitly so operators don't accidentally schedule one.
5. *`os.EX_TEMPFAIL` not custom code.* The sysexits.h
   convention (75 = "transient failure, retry later") is
   what cron and systemd-timer + supervisor tools expect.
   Using the documented constant means no operator surprise.

---

## [0.25.0] — 2026-05-10 — **Menús CLI de operador completos**

Closes the operator-CLI menus with three small commands. After
this change the only entries still missing are the `background`
runner and the TUI — both depend on a TUI design that's a
separate change. Operators now have the full read-only triage +
offline-analysis surface.

### Agregado

- **`cmcourier inspect trigger [--source <descriptor>] [--limit N]`**
  — preview the first N triggers a source would emit. When
  `--source` is omitted, builds the strategy from
  `config.trigger` via the existing wiring helper. When
  `--source csv:<path>` is given, builds a one-off
  `CsvTriggerStrategy` over the path. When
  `--source single_doc:<short>,<sys>[,<cif>]` is given,
  builds a one-off `SingleDocTriggerStrategy`. Other schemes
  (`rvabrep`, `as400`, `local_scan`) require richer config —
  the command rejects with a clear hint pointing operators
  at the YAML.
- **`cmcourier inspect mapping-stats`** — structured summary of
  the Modelo Documental:
  - `Total mappings: <n>`
  - `Distinct document classes: <n>`
  - `Mappings with ID Corto: <n> / <total>`
  - `Distinct CM object types: <n>`
  - `Distinct CM folders: <n>`
  - Top-5 classes by mapping count (tie-break alphabetical).
- **`cmcourier batch export-report --batch <id> --format csv|json
  [--output <path>]`** — dump a batch's full state for offline
  analysis. CSV emits a flat S0..S5 table with batch metadata
  repeated on every row; JSON emits the full `BatchDetails`
  payload (stage_counts + failed_records nested). Default
  writes to stdout; `--output` writes a file plus a
  confirmation line.
- **`cmcourier.cli.commands._source_descriptor`** — new helper
  module owning the `csv:<path>` / `single_doc:<...>` parser.
  Pure function + frozen dataclass. Unit tested independently
  of Click.
- **18 new tests** across:
  - `tests/unit/cli/commands/test_source_descriptor.py` (10
    tests: scheme parsing + rejection paths).
  - `tests/integration/cli/test_inspect.py` (7 trigger + 3
    mapping-stats tests).
  - `tests/integration/cli/test_batch.py` (5 export-report
    tests).

### Cambiado

- **`cli/commands/inspect.py`** grew from 2 commands to 4
  (rvabrep, mapping, trigger, mapping-stats). Module size
  ~290 LOC.
- **`cli/commands/batch.py`** grew from 3 commands to 4
  (list, show, retry-failed, export-report). Module size ~290
  LOC.
- **`inspect trigger` "permissive secrets" path**: when
  building a strategy from `config.trigger`, CMIS env vars
  aren't required (only AS400 trigger kinds need
  `AS400_USERNAME` / `AS400_PASSWORD`). The fallback to an
  empty `Secrets` bundle lets csv-trigger / single-doc configs
  work without exporting CMIS creds — a real ergonomics win
  for read-only inspection.

### Verificación

- `pytest --cov`: **573 / 573 pass** in ~96 s (+25 net new
  across the change cycle).
- Coverage: total **94 %**;
  `cli/commands/_source_descriptor.py` at **95 %**,
  `cli/commands/batch.py` at **96 %**,
  `cli/commands/inspect.py` at **92 %**.
- `ruff check` / `ruff format --check`: clean.
- `mypy src/cmcourier`: clean (48 source files).
- `pre-commit run --all-files`: ruff + ruff format + mypy all
  pass.
- Smoke: `cmcourier inspect --help` lists `trigger`,
  `rvabrep`, `mapping`, `mapping-stats`. `cmcourier batch
  --help` lists `list`, `show`, `retry-failed`,
  `export-report`.

### Justificación

Before 023 an operator who wanted to know "what would the
trigger source emit?" had to spin up a tiny pipeline run.
"How many CM classes does the Modelo Documental have?" meant
opening Excel. "Send me this batch's report" meant taking a
screenshot of the terminal. Three small commands close all
three gaps — none of them need new ports or schema changes.

**Architectural decisions worth flagging:**

1. *Reuse, don't fork.* `inspect trigger` without `--source`
   reuses the wiring's `_build_trigger_strategy`. `inspect
   mapping-stats` reuses `MappingService.get_all()` /
   `count()`. `batch export-report` reuses `get_batch_details`
   from 021. No service was modified; only new CLI surfaces.
2. *Descriptor parser in its own module.* Click subcommands
   call a pure function; the function is unit-testable
   without spinning up a CLI runner. Future schemes (when
   they're worth supporting via CLI args) land here.
3. *CSV stays flat; JSON nests.* `batch export-report`'s two
   formats serve two audiences. CSV is for Excel /
   spreadsheet workflows that hate nested data. JSON is for
   tooling that wants the full structured payload. No
   `--include-failed-records` flag — the format chooses for
   you.
4. *Inspect commands don't auto-doctor.* Unlike pipeline run
   commands (022), inspect commands are read-only and offline
   (they don't touch CMIS). Running doctor would just waste
   the operator's time during triage.
5. *`_strategy_from_config` falls back to empty secrets.*
   Inspect is read-only. CMIS isn't touched. If the operator
   hasn't exported CMIS env vars, that's fine for inspect.
   The full pipeline-run path keeps the strict secrets check
   it always had.

---

## [0.24.0] — 2026-05-10 — **Flags de seguridad de pipeline**

Closes the pre-dry-run safety polish: pipelines auto-run doctor
before doing work, `--resume` infers the right `--from-stage`
from tracking state, and `doctor --check <group>` lets the
operator run a single check during triage.

### Agregado

- **Auto-doctor before every pipeline run.** Every
  `*-pipeline run` command (csv-trigger, rvabrep,
  as400-trigger, local-scan) plus `single-doc run` now calls
  `run_doctor(config, secrets)` after config + observability
  setup and before constructing the pipeline. FAIL → exit 2
  with the doctor report printed. PASS/WARN → proceeds.
- **`--skip-doctor` flag on every pipeline run command.**
  Bypasses the auto-doctor for dev iteration or trusted configs.
  When passed, no doctor output appears.
- **`--resume` flag on every pipeline run command.** Requires
  `--batch-id`. Queries the tracking store via
  `get_batch_details(batch_id)` (shipped in 021), inspects
  `stage_counts`, finds the lowest stage with
  `FAILED + PENDING > 0`, and uses that as `--from-stage`.
  Behaviors:
  - `--resume` without `--batch-id` → exit 2.
  - `--resume <unknown id>` → exit 1 with "Batch not found".
  - `--resume <clean batch>` → exit 0 with "Nothing to resume".
  - `--resume <mid-flight>` → resolves and runs; emits a
    `resume_resolved` event with the inferred stage.
  - `--resume` AND `--from-stage <non-default>` → `--from-stage`
    wins; WARNING log surfaces the override.
- **`doctor --check <name>` selective filter** with values
  `connections | mapping | metadata | cm-types | all`
  (default `all`). Group mapping:
  - `connections` → `log_dir_writable`, `cmis_connectivity`,
    `as400_connectivity`, `tracking_openable`
  - `mapping` → `mapping_completeness`
  - `metadata` → `metadata_sources`, `sample_dry_run`
  - `cm-types` → `cm_type_alignment`
  - `all` → every check (current behavior — regression)
  Auto-doctor (called from pipeline commands) always uses
  `selected="all"`; the filter only applies to standalone
  `cmcourier doctor` invocations.
- **`_run_auto_doctor` and `_apply_resume` helpers** in
  `cli/app.py` keep the per-command bodies thin (the heavy
  lifting lives in named helpers, the commands just dispatch).
- **`_CHECK_GROUPS` + `_selected` helper** in `cli/doctor.py`
  gate each `results.append(...)` line on group membership.
  `cm_type_alignment` SKIP fallback preserved when
  `cmis_connectivity` was run and FAILed within the same
  invocation.
- **14 new integration tests**:
  - 3 in `test_cli.py::TestAutoDoctor` — auto-doctor PASS,
    FAIL blocks pipeline, `--skip-doctor` bypasses.
  - 4 in `test_pipeline_kinds.py::TestResumeFlag` — missing
    batch id, unknown batch, clean batch, mid-flight resume.
  - 7 in `test_doctor.py::TestDoctorCheckFilter` — each
    group filter + `all` regression + CLI help + invalid
    value rejection.

### Cambiado

- **Every pipeline run command signature** gains `--skip-doctor`
  and `--resume` flags. `_run_pipeline_command` central helper
  picks them up. `single_doc_run_command` (the only outlier)
  applies the same logic inline.
- **`run_doctor`** signature extends with a keyword-only
  `selected: str = "all"`. Backwards-compatible: every existing
  call uses the default.
- **Existing CLI tests** were updated to pass `--skip-doctor`
  on every `*-pipeline run` invocation that doesn't specifically
  test the auto-doctor path. This preserves their original
  intent (exercise pipeline behavior, not doctor scaffolding).
  Scope: ~14 invocations across `test_cli.py`,
  `test_pipeline_kinds.py`, `test_pipeline_emits.py`.

### Verificación

- `pytest --cov`: **548 / 548 pass** in ~101 s (+14 net new).
- Coverage: total **94 %**; `cli/app.py` at **89 %**,
  `cli/doctor.py` at **88 %**.
- `ruff check` / `ruff format --check`: clean.
- `mypy src/cmcourier`: clean (47 source files).
- `pre-commit run --all-files`: ruff + ruff format + mypy all
  pass.

### Justificación

Before 022 the operator could forget to run `doctor` before a
pipeline and only discover a broken CMIS auth 30 s into a run —
exactly when feedback hurts most. After 022 it's the other way
around: every pipeline run starts with a 5–10 s pre-flight and
either proceeds confidently or fails loud and early. The
`--skip-doctor` flag preserves the dev-iteration ergonomics:
when you trust your config and want fast feedback, opt out.

`--resume` solves the operator-math problem. Today's resume
flow requires `cmcourier batch show <id>` to find the lowest
stage with pending/failed work, then mental-math the
`--from-stage <n>` to pass back to the pipeline command. With
`--resume`, the tooling does the math: query → infer → run.
Edge cases (no batch_id, unknown batch, clean batch) all exit
cleanly with operator-readable messages. Explicit `--from-stage`
still wins — `--resume` is sugar, never a constraint.

`doctor --check <group>` is the triage shortcut. When the
operator already knows CMIS is fine but suspects Modelo
Documental, running the full 7-check suite (~10 s) just to
confirm wastes seconds. The group names come straight from
the operator-CLI surface; the internal check names map cleanly onto them.

**Key architectural decisions:**

1. *Auto-doctor uses the FULL check set.* Even though the
   doctor command now supports group selection, the
   pre-pipeline auto-doctor always runs everything. Selective
   checks are an *operator triage tool*, not a way to bypass
   safety during a real run.
2. *Explicit beats implicit.* Whenever the user gave both
   `--resume` and `--from-stage`, the explicit number wins.
   A WARNING log line surfaces the override so the operator
   knows their `--from-stage` overrode the inferred value.
3. *No port additions, no schema changes.* Everything lives
   in `cli/app.py` and `cli/doctor.py`. The port method that
   `--resume` consumes (`get_batch_details`) shipped in 021;
   this change just wires it through a new CLI surface.
4. *Test-suite hygiene.* Adding `--skip-doctor` to ~14
   existing tests instead of stubbing every doctor check is
   the right tradeoff: those tests are about pipeline
   behavior, not pre-flight validation. The new
   `TestAutoDoctor` class explicitly exercises the
   pre-flight path.

---

## [0.23.0] — 2026-05-10 — **Esenciales del CLI de operador**

Adds the six commands an operator needs between pipeline runs:
batch lifecycle (list/show/retry-failed), preview commands (inspect
rvabrep/mapping), and a raw AS400 query escape hatch. Pure
additions on top of the existing pipelines + doctor + single-doc.
No CLI surface that previously worked has changed.

### Agregado

- **`cmcourier batch list [--status in_progress|completed]`** —
  enumerate batches with status + counts, newest first.
- **`cmcourier batch show <batch_id>`** — per-stage counts
  (S0..S5 × DONE/FAILED/PENDING) + failed records with their
  error messages.
- **`cmcourier batch retry-failed --batch <id> [--stage Sn]`** —
  reset `*_FAILED` rows in `migration_log` back to `*_PENDING`
  so the next pipeline run picks them up. Idempotent; reports
  count reset.
- **`cmcourier inspect rvabrep <shortname> <system_id>`** — print
  the RVABREP rows S1 would produce for one trigger. Reads
  through `IndexingService` to mirror real pipeline behavior.
- **`cmcourier inspect mapping <id_rvi>`** — print the CM mapping
  (folder + object type + required metadata fields) for one ID
  RVI from the Modelo Documental.
- **`cmcourier as400-query "<SQL>"`** — raw SQL against the AS400
  configured in YAML (preferring `trigger.as400_connection`,
  falling back to first `metadata.sources[*]` of kind `as400`).
  Result cells truncated to 80 chars per column. Debug-only.
- **3 new ITrackingStore port methods**: `list_batches`,
  `get_batch_details`, `retry_failed`. Implemented in
  `SQLiteTrackingStore` via the existing reader connection
  (writes use REPLACE on the status column for safety).
- **3 new domain dataclasses**: `BatchInfo` (with derived
  `status` property), `FailedRecord`, `BatchDetails` (with
  predictable `S0..S5 × DONE/FAILED/PENDING` shape).
- **`cmcourier.cli.commands` subpackage** — new home for the
  expanding CLI surface so `cli/app.py` stays a registry, not
  a kitchen sink.
- **23 new tests**: SQLite store (4 list/3 details/4 retry),
  batch CLI (3 × 4), inspect CLI (3 + 3), as400-query CLI (4).

### Cambiado

- **`cli/app.py`** registers the new groups + standalone command
  via `main.add_command(...)`. No change to existing pipeline
  commands.
- **`ITrackingStore`** gains 3 abstract methods. Only
  `SQLiteTrackingStore` implements the port in production; the
  `__abstractmethods__` test in `tests/unit/domain/test_ports.py`
  was extended to reflect the new contract.

### Verificación

- `pytest --cov`: **534 / 534 pass** in ~91 s (+32 net new across
  the change cycle).
- Coverage: total **94.21 %**; `cli/commands/batch.py` at **96 %**,
  `inspect.py` at **95 %**, `as400_query.py` at **79 %** (error
  branches not exercised in tests; targeted by the doctor
  smoke), `_formatting.py` at **68 %** (edge cases like empty
  headers).
- `ruff check` / `ruff format --check`: clean.
- `mypy src/cmcourier`: clean (47 source files).
- `pre-commit run --all-files`: ruff + ruff format + mypy all
  pass.
- Smoke: `cmcourier --help` lists 9 commands (was 6).
  `cmcourier batch list --help`, `cmcourier inspect rvabrep
  --help`, `cmcourier as400-query --help` all render correctly.

### Justificación

Before 021 an operator who wanted to know "which batch failed and
why?" had to open SQLite manually. "Retry the failed S5 uploads?"
meant writing UPDATEs by hand. "What does S1 think of this
trigger?" required spinning up Python. These three workflows are
the daily bread of any migration in flight; making them ergonomic
is the difference between a dry run that uncovers issues and a
dry run that gets bogged down in tooling.

**Architectural decisions worth flagging:**

1. *Port extension, not direct SQLite from CLI*. Constitution I
   says adapters are behind ports. The temptation was strong to
   read SQLite directly from `batch list` for speed — resisted.
   Three new methods on `ITrackingStore`, three new SQLite
   implementations, and the CLI talks to the port. If a future
   AS400-backed tracking store lands, every operator command
   keeps working.

2. *`REPLACE(status, '_FAILED', '_PENDING')` for retry*. Safe
   because the only `_FAILED` substring in any `StageStatus`
   value is the suffix. A regression test pins this invariant.
   The alternative (parse + reassemble in Python before UPDATE)
   was strictly more code for no benefit.

3. *Predictable `stage_counts` shape*. The pivot helper always
   emits all six stages × three outcomes, even when zero. The
   CLI rendering is dumb because the data is consistent;
   adding a stage in the future is one-line change.

4. *`cli/commands/` subpackage*. Each new command family gets
   its own module. The directory was empty since project
   bootstrap — 021 finally uses it.

5. *Per-command observability*. Every new command calls
   `configure_observability(config.observability, "INFO")`
   after `load_config`. Batch ops, inspect previews, and
   raw queries all leave audit trails in `app-{date}.log`.

6. *`as400-query` warns about PII*. The command emits a WARNING
   to the observability log noting that raw cells may contain
   PII. Operators are responsible for what they query; the log
   captures the SQL prefix (≤80 chars) for after-the-fact
   review.

---

## [0.22.0] — 2026-05-10 — **Niveles de observabilidad 1-4**

Full-MVP observability surface. Operators now get structured JSON
logs, per-batch pipeline timing percentiles (p50/p95/p99), per-request
network latency for AS400 + CMIS, and a top-N slow-ops report — all
toggleable from YAML, all PII-masked by a central filter, all
parseable by `jq` or any log shipper. The dry run is no longer blind.

### Agregado

- **New package `src/cmcourier/observability/`** — peer to
  adapters/services. Modules: `formatter.py` (JsonFormatter),
  `pii.py` (PiiMaskingFilter + denylist), `metrics.py`
  (StageTimer, BatchSummary, MetricsRecorder, SlowOpAggregator,
  NetworkEvent), `setup.py` (`configure(config, log_level)`).
- **`ObservabilityConfig`** in `config/schema.py` with the
  observability fields: `enabled`, `pipeline_metrics`, `network_metrics`,
  `system_metrics`, `log_dir`, `log_format`, `rotation_mb`,
  `retention_days`, `slow_op_threshold_ms`, `slow_op_top_n`.
  `system_metrics=true` raises ValidationError — deferred to
  POST-MVP §2. `PipelineConfig.observability` defaults to a
  sane block so existing YAMLs keep validating.
- **Tier 1 — application log** (`logs/app-{date}.log`): JSON
  Lines, every record from the `cmcourier` logger hierarchy.
  `RotatingFileHandler` with configurable `rotation_mb` cap +
  5 backups. Always on when `enabled=True`.
- **Tier 2 — pipeline metrics** (`logs/metrics-{date}.jsonl`):
  one batch-summary line per pipeline run with
  `{pipeline, batch_id, total_docs, elapsed_s,
  throughput_docs_per_s, stages.{S0..S5}.{count, p50_ms, p95_ms,
  p99_ms, sum_ms}}`. Toggle via `pipeline_metrics`.
- **Tier 3 — network metrics** (`logs/network-{date}.jsonl`):
  per AS400 query + per CMIS HTTP request, with `kind`
  (`as400_query` / `cmis_upload` / `cmis_post` / `cmis_get`),
  `duration_ms`, plus shape-specific fields (`sql_prefix`,
  `row_count`, `size_bytes`, `status`, `url_prefix`). Toggle via
  `network_metrics`.
- **Tier 4 — slow-ops report** (`logs/slow-ops-{batch_id}.jsonl`):
  top-N slowest operations per batch, ranked descending,
  thresholded by `slow_op_threshold_ms`. Collected in-memory by
  a custom `_SlowOpHandler` attached to `cmcourier` +
  `cmcourier.metrics.network` at `start_batch`; flushed to disk
  at `close_batch`.
- **PII masking** via `PiiMaskingFilter` installed on every
  handler. Denylist: `cif`, `customer_name`, `account_number`,
  `nombre`, `phone`, `email`, `address`, `dni`; plus prefix
  `pii_*`. Values replaced with `***`. Constitution Principle
  VIII enforced at the formatter layer — callers pass PII via
  `extra={...}` and the filter catches it before any handler
  formats the record.
- **`StagedPipeline` instrumentation**: per-doc `stage_complete`
  events (S0..S5) emitted to the `cmcourier` logger at INFO
  with `extra={pipeline, stage, batch_id, txn_num, outcome,
  duration_ms}`. Aggregation flows into the per-batch summary.
- **Adapter instrumentation**: `As400DataSource.query` /
  `query_stream` emit AS400 network events. `CmisUploader`
  emits network events for warmup (GET), type-definition (GET),
  folder create (POST), and document upload (POST). 1-2 lines
  per request path; if `network_metrics=false`, the dedicated
  logger is silenced (level above CRITICAL) — emission cost is
  one level check.
- **Doctor check `log_dir_writable`**: probes
  `observability.log_dir` for create + write before the rest of
  the pre-flight runs. FAIL surfaces unwritable paths with a
  clear `OSError` detail. Runs first because if logging is
  broken, every other check's output is invisible.
- **`observability.setup.configure(config, log_level)`** — the
  primary entry point. Idempotent: removes existing handlers,
  resets propagation and levels, installs fresh. CLI entry
  points call this after `load_config()`. The legacy
  `cli/logging_setup.configure(level)` shim stays for pre-config
  paths (e.g., doctor's early failure path).
- **15 net new tests** across 4 files (`test_formatter.py`,
  `test_metrics.py`, `test_pipeline_emits.py`, doctor + schema
  additions). E2E asserts the four files materialize on disk
  with the expected JSON shape; PII regression confirms no CIF
  value reaches any handler output.

### Cambiado

- **`cmcourier/cli/logging_setup.py`** is now a 4-line shim that
  delegates to `observability.setup.configure(stderr_only=True)`.
  Backwards-compatible signature.
- **`cmcourier/cli/app.py`** calls
  `observability.setup.configure(config.observability, log_level)`
  after parsing in every entry point (run + doctor + single-doc).
- **`cmcourier/orchestrators/staged.py`** accepts optional
  `metrics_recorder` and `pipeline_name`. Per-stage work
  wrapped in `with StageTimer(...): ...`. `mark_failed()` on
  caught exception paths so the recorded outcome reflects
  reality. Batch lifecycle wraps `recorder.start_batch(...)` →
  stages → `recorder.close_batch(...)`.
- **`cmcourier/config/wiring.py`** builds a `MetricsRecorder`
  from `config.observability` and threads it into
  `StagedPipeline`. New `pipeline_name` kwarg defaults to
  `csv-trigger`.
- **`cmcourier/adapters/sources/as400.py`** times each
  `query`/`query_stream` call (including stream completion) and
  emits a network event.
- **`cmcourier/adapters/upload/cmis_uploader.py`** times the
  warmup, type-definition, and retry-loop POST paths. A new
  `_emit_network` helper centralizes the structured logging
  call.

### Verificación

- `pytest --cov`: **502 / 502 pass** in ~65 s (+35 net new across
  the change cycle; the headline target was ≥15 new tests for
  observability itself).
- Coverage: total **94.92 %**; `observability/__init__.py` at
  **100 %**, `formatter.py` at **100 %**, `pii.py` at **100 %**,
  `setup.py` at **98 %**, `metrics.py` at **96 %**.
- `ruff check` / `ruff format --check`: clean.
- `mypy src/cmcourier`: clean (43 source files).
- `pre-commit run --all-files`: ruff + ruff format + mypy all
  pass.

### Justificación

The MVP was running on a single stderr text handler — fine for
unit-test feedback, blind for a real dry run. The multi-tier
surface was specified up front; 020 ships the four cheap tiers
and explicitly defers the expensive one (`psutil` sampling).
With these tiers an operator can answer the questions that
matter during a real migration:

* "Why was this batch slow?" → `metrics-{date}.jsonl` shows
  which stage dominated. p95 vs p50 reveals tail latency.
* "Which document took the longest?" →
  `slow-ops-{batch_id}.jsonl` ranks top-N.
* "Is the upload network bound?" →
  `network-{date}.jsonl` per-request timings make this trivial
  to chart.
* "Did the pipeline really finish stage S2 for all docs?" →
  `app-{date}.log` has per-doc `stage_complete` events.

**Key architectural decisions** worth remembering:

1. *Logger-name routing*. Each tier has a named logger
   (`cmcourier.metrics.pipeline`, `.network`, `.slow_ops`). The
   handler/formatter/filter wiring lives in `setup.py`. Caller
   code uses normal `logger.info(...)` with `extra={...}` —
   blissfully unaware of where the bytes go.
2. *Slow-ops via handler interception*. A custom
   `_SlowOpHandler` is attached at `start_batch` to `cmcourier`
   + `cmcourier.metrics.network`. Any record with `duration_ms`
   above threshold becomes a candidate. No constructor changes
   to adapters — they just emit, the handler catches.
3. *PII at the formatter boundary*. The denylist filter mutates
   `record.__dict__` BEFORE the formatter runs. Even if a
   caller accidentally passes a CIF as `extra={"cif": "..."}`,
   the disk only sees `***`. The `name` key is intentionally
   absent from the denylist (it collides with
   `LogRecord.name` — masking it triggers an infinite
   audit-log recursion).
4. *State leak resistance*. `_reset_all_handlers` resets level
   to NOTSET and propagation to True for every monitored logger
   before installing fresh handlers. Tests share the process
   logging state; without the reset, propagate=False from one
   test would silently break caplog in the next.

---

## [0.21.0] — 2026-05-10 — **Limpieza de higiene de port en adapters**

Closes a Constitution Principle I (hexagonal architecture) deuda:
the last two adapters that implemented their ports structurally
(duck-typed) now declare formal inheritance. Pure declarative
cleanup — zero behavioral changes.

### Agregado

- **`PdfAssembler` now inherits from `IAssembler`**. The class
  declaration is `class PdfAssembler(IAssembler):`. Python's ABC
  machinery now guards against any future drift: if a required
  abstract method were ever removed, `PdfAssembler(...)` would
  raise `TypeError` at instantiation.
- **`CmisUploader` now inherits from `IUploader`**. Same guarantee:
  `ensure_folder`, `upload`, `test_connection`,
  `get_type_definition` are now formally overrides validated by
  mypy.
- **2 new conformance tests**:
  - `tests/integration/adapters/test_pdf_assembler.py::TestPortConformance::test_pdf_assembler_is_iassembler`
  - `tests/integration/adapters/test_cmis_uploader.py::TestPortConformance::test_cmis_uploader_is_iuploader`
  Each instantiates the adapter and asserts `isinstance(adapter,
  port)` returns `True`. They fail loudly if a future change
  drops the inheritance.

### Cambiado

- **Adapter import blocks**: `pdf_assembler.py` and
  `cmis_uploader.py` each gained one import line
  (`from cmcourier.domain.ports import IAssembler` /
  `IUploader`). No other source edits.
- **`__mro__`**: `PdfAssembler.__mro__` now contains `IAssembler`;
  `CmisUploader.__mro__` now contains `IUploader`. This is the
  observable runtime side of the change — `isinstance` checks
  work, registries that filter by port type work, doctor /
  diagnostic code can rely on it.

### Verificación

- `pytest --cov`: **467 / 467 pass** in ~69 s (+2 net new).
- Coverage: total **94.79 %** (unchanged);
  `adapters/assembly/pdf_assembler.py` at **98 %**;
  `adapters/upload/cmis_uploader.py` at **94 %**.
- `ruff check` / `ruff format --check`: clean.
- `mypy src/cmcourier`: clean (38 source files). Validated the
  override signatures match the port abstract methods. No new
  errors surfaced — signatures were already aligned, the
  declaration just made the alignment formal.
- `pre-commit run --all-files`: ruff + ruff format + mypy all
  pass.

### Justificación

Constitution Principle I demands a strict port/adapter split. The
project had been 60 % consistent (`TabularDataSource`,
`As400DataSource`, `SQLiteTrackingStore`, and all 5 S0 strategies
already inherited their ports). The two outliers were the
assembler and uploader — both worked because Python uses duck
typing at runtime, but neither was guarded against signature drift
and neither passed `isinstance(adapter, port)` checks.

This change closes the gap with minimal surface area: 2 imports,
2 class declarations, 2 tests. mypy now validates every override,
and Python's ABC instantiation check guards against missing
methods. A future port-signature change (e.g., adding a parameter)
will now surface at the adapter override instead of at the call
site — a much earlier and more actionable failure point.

The change is also a pedagogical artifact for new contributors:
the ports/adapters split is no longer "mostly enforced, sometimes
implicit" — every adapter says, at the top of its class
declaration, which port it implements.

---

## [0.20.0] — 2026-05-10 — **Override de query AS400 por-fuente**

Closes the production-data scale gap left by 015. AS400 metadata
sources can now use a custom `SELECT ...` query (with filtering and
column projection) instead of `SELECT * FROM <table>`. The
MetadataService prefetch is untouched — the adapter wraps the
query in a derived-table alias so the full `IDataSource` contract
(`get_all`, `count`, `get_by_fields*`) keeps working transparently.

### Agregado

- **`As400MetadataSourceConfig.query: str | None`** — new optional
  field. Operators specify a complete `SELECT ...` statement scoped
  to the data the migration actually needs (e.g.,
  `SELECT CIF, NAME FROM CUSTOMERS WHERE ACTIVE = 'Y'`). Pydantic's
  `min_length=1` rejects empty strings.
- **`As400MetadataSourceConfig.table: str | None`** — now optional.
  An `@model_validator(mode="after")` enforces exactly-one of
  `table` / `query`. Both-set and neither-set both raise
  `ValidationError` at load time.
- **`As400DataSource` constructor accepts `query: str | None`** —
  new keyword-only argument. Mutually exclusive with `table`. The
  adapter computes `self._source_expr = f"({query}) AS T" if query
  else table` and uses that expression in every generated SQL
  template.
- **Derived-table alias (`AS T`)** wraps the operator query
  whenever it's used as a `FROM` source. DB2/AS400 requires the
  alias; using a single-letter `T` keeps generated SQL minimal.
- **3 new schema tests**: query mode loads correctly, both-set
  rejected with "exactly one" message, neither-set rejected.
- **7 new adapter tests**: construction validation
  (both/query-only/neither), and query-mode SQL templates for
  `get_all` (subquery alias), `count`, `get_by_fields`, plus a
  table-mode regression test asserting no subquery wrapping when
  `table` is used.
- **1 new wiring integration test**: query-mode YAML builds a
  pipeline whose metadata registry contains an `As400DataSource`
  with the expected `_source_expr`.

### Cambiado

- **`As400DataSource._table` attribute renamed to `_source_expr`**.
  The new name reflects that the value may be either a bare table
  identifier (table mode) or a parenthesized derived-table
  expression (query mode). All internal SQL templates updated.
- **`As400DataSource.__init__` signature**: `table` now defaults to
  `""` (was required). Backwards-compat preserved — all existing
  call sites pass `table=...` explicitly.
- **Constructor rejects (both `table` AND `query` set)** with
  `ConfigurationError` at the adapter boundary. Schema validation
  catches the same case earlier, but the adapter check enforces the
  invariant at every call site (defense in depth).
- **`_build_metadata_sources` in `config/wiring.py`** and the
  doctor's `_open_metadata_source` helper pass `query=src_cfg.query`
  through to the adapter. Falsy `table` defaults to `""`.

### Verificación

- `pytest --cov`: **465 / 465 pass** in ~70 s (+11 net new: 7
  adapter, 3 schema, 1 wiring).
- Coverage: total **94.79 %**; `adapters/sources/as400.py` at **88
  %** (unchanged from 015).
- `ruff check` / `ruff format --check`: clean.
- `mypy src/cmcourier`: clean (38 source files). Caught a real
  callsite (`doctor.py`) where `table: str | None` had to be
  coerced — fixed by passing `source_cfg.table or ""`.
- `pre-commit run --all-files`: ruff + ruff format + mypy all
  pass.
- Smoke: YAML loader parses
  `metadata.sources[].query` correctly, `table` becomes `None`
  when absent.

### Justificación

015 enabled AS400 metadata sources but only supported `SELECT *
FROM <table>`. Production AS400 tables can have millions of rows
and dozens of columns the migration never touches. Without query
filtering, operators were forced to pre-stage the data into CSVs —
defeating the value of native AS400 sources. 018 closes this gap
without changing the prefetch model (1 source = 1 cached dataset);
the operator simply scopes the query.

The "per-field" framing in earlier roadmap notes was a misnomer.
Per-field query overrides would break the shared-prefetch model
(each field would need its own dataset). 018 settles on
**per-source** — one query feeds one alias, which many fields can
reference. This is consistent with the 015 source-registry
architecture and keeps Constitution I (hexagonal architecture)
intact.

The derived-table alias (`(query) AS T`) is the key invariant: it
lets the existing `IDataSource` methods (`count`,
`get_by_fields`, `get_by_fields_in`) issue `... FROM (subquery) AS
T WHERE ...` without knowing whether the source is table- or
query-backed. The MetadataService, doctor, and every other caller
sees a single polymorphic adapter.

---

## [0.19.0] — 2026-05-10 — **single-doc-pipeline (superficie diagnóstica)**

Completes the pipeline surface: 4 production pipelines + 1 diagnostic
pipeline. Operators can now push a specific shortname/system/cif
through the full S1..S5 chain from the CLI without scanning a batch
— useful for re-pushing a single failed doc or smoke-testing a new
config against a known target.

### Agregado

- **`cmcourier.services.triggers.single_doc.SingleDocTriggerStrategy`**
  — minimal S0 strategy that yields exactly one `TriggerRecord` built
  from constructor args (`shortname`, `system_id`, optional `cif`).
  Empty-string `cif` is normalized to `None`. No data source; the
  trigger is carried in-process.
- **`SingleDocTriggerConfig(kind: Literal["single_doc"])`** — new
  schema member in `TriggerConfigUnion`. No extra fields — the
  trigger comes from CLI args, not YAML.
- **`cmcourier single-doc run`** — new Click sub-group + command:
  `--config <yaml> --shortname X --system Y [--cif Z]`, plus the
  standard `--batch-id`, `--from-stage`, `--batch-size`,
  `--log-level` flags. Verifies `config.trigger.kind == "single_doc"`
  and exits 2 on mismatch.
- **`build_pipeline(config, secrets, *, trigger_strategy_override=None)`**
  — keyword-only override that bypasses the schema-driven dispatch.
  The CLI uses it to inject the pre-built strategy; the
  `_build_trigger_strategy` branch for `SingleDocTriggerConfig`
  raises `ConfigurationError` so non-CLI callers fail loudly.
- **Doctor SKIP branch**: `_check_sample_dry_run` returns SKIP
  (`reason="trigger_kind_single_doc_requires_cli_args"`) when
  `trigger.kind == "single_doc"`. Without this, the dry-run would
  fail at construction time and confuse operators.
- **7 new unit tests** for `SingleDocTriggerStrategy` (single yield,
  cif None / empty-string / present, `S0Strategy` protocol, empty
  shortname raises, empty system_id raises, `source_descriptor`
  ignored).
- **2 new schema tests** (`kind=single_doc` loads to
  `SingleDocTriggerConfig`; extra fields rejected).
- **2 new wiring tests** (`build_pipeline` without override raises;
  with override returns a `StagedPipeline` whose
  `_trigger_strategy is` the override).
- **3 new CLI tests** (`single-doc run --help`, happy path with
  mocked CMIS, kind mismatch).
- **1 new doctor test** (sample_dry_run returns SKIP for
  `kind=single_doc`).

### Cambiado

- **`_TriggerKind` Literal in `cli/app.py`** extended to include
  `"single_doc"`.
- **`__all__` in `cmcourier.config.schema`** adds
  `SingleDocTriggerConfig`.
- **`__all__` and module docstring in
  `cmcourier.services.triggers.__init__`** updated to re-export
  `SingleDocTriggerStrategy` and acknowledge the 5th strategy
  (4 production + 1 diagnostic).
- **Root `--help`** now lists six command groups: 4 pipelines +
  `single-doc` + `doctor`.

### Verificación

- `pytest --cov`: **454 / 454 pass** in ~65 s (+15 net new: 7
  strategy, 2 schema, 2 wiring, 3 CLI, 1 doctor).
- Coverage: total **94.73 %**;
  `services/triggers/single_doc.py` at **100 %**.
- `ruff check` / `ruff format --check`: clean.
- `mypy src/cmcourier`: clean (38 source files).
- `pre-commit run --all-files`: ruff + ruff format + mypy all pass.
- Smoke: `cmcourier --help` lists 6 commands;
  `cmcourier single-doc run --help` lists all required flags.

### Justificación

Closes the pipeline catalog: four production pipelines
(csv-trigger, rvabrep, as400-trigger, local-scan) + one diagnostic
pipeline (single-doc). The override pattern keeps the schema layer
honest — `_build_trigger_strategy` still raises for any caller that
tries to wire single-doc without injecting a strategy, so the only
legitimate entry point remains the dedicated CLI command. This
preserves Constitution V (config validated at startup) while opening
a narrow, well-documented seam for CLI-driven dispatch.

---

## [0.18.0] — 2026-05-10 — **local-scan-pipeline (4ª pipeline de producción)**

Closes the production-pipeline set. With 016, the project covers
every committed trigger source mode: csv, direct rvabrep, as400,
local_scan.

### Agregado

- **`cmcourier.services.triggers.local_scan.LocalScanTriggerStrategy`**
  — real implementation. Lists `scan_path` non-recursively, filters
  to `*.PDF` (case-insensitive) and `*.001` (paged-doc first page
  for the paged-doc first page), and for each file queries the RVABREP source
  via `get_by_fields({file_name_column: name})`. Yields one
  `TriggerRecord` per matched row. Files with no RVABREP match are
  logged at WARNING (`file_name`, `scan_path` in `extra`) and
  dropped.
- **`LocalScanTriggerConfig(kind: Literal["local_scan"], scan_path: DirectoryPath)`**
  — new schema member in the `TriggerConfigUnion` discriminated
  union. Pydantic's `DirectoryPath` validates that the path exists
  at load time.
- **`cmcourier local-scan-pipeline run --config <yaml>`** — new
  Click command. Identical surface to the other pipeline commands
  minus `--triggers` (no CSV override for local_scan). Verifies
  `config.trigger.kind == "local_scan"` and exits 2 on mismatch.
- **`RvabrepColumnsConfig.file_name_column: str = "ABAJCD"`** — new
  field on the existing dataclass. Drives the local_scan strategy's
  per-file query into RVABREP. Default matches the RVABREP
  physical name; production configs override to the friendly name.
- **10 new unit tests** for `LocalScanTriggerStrategy` covering:
  happy path, non-trigger filename filtering (`.002` / `.txt` /
  `.tmp` ignored), WARNING on unmatched file, missing `scan_path`
  raises, blank shortname dropped, case-insensitive `.PDF` match,
  empty CIF → None, empty directory yields zero triggers, S0Strategy
  protocol check, default columns config.
- **2 new schema tests** for `kind=local_scan` (loads to
  `LocalScanTriggerConfig`; rejects missing `scan_path`).
- **3 new CLI tests** (`--help`, happy path with mocked CMIS, kind
  mismatch).
- **1 new wiring test** verifying `LocalScanTriggerStrategy`
  dispatch.

### Cambiado

- **`cmcourier.services.triggers.stubs` module DELETED**. With
  `LocalScanTriggerStrategy` promoted, no stubs remain. The
  `__init__.py` re-export is updated.
- **`tests/unit/services/test_trigger_strategies.py::TestStubStrategies`
  removed**. The class was testing the stub's `NotImplementedError`
  behavior; the new `TestLocalScanStrategy` covers the real
  implementation.
- **`_TriggerKind` Literal in `cli/app.py`** extended to include
  `"local_scan"`.
- **`__all__` in `cmcourier.config.schema`** adds
  `LocalScanTriggerConfig`.

### Verificación

- `pytest -v`: **439 / 439 pass** in ~64 s (+12 net new: 10 strategy
  + 2 schema + 3 CLI + 1 wiring − 3 obsolete stub tests).
- `pytest --cov=src/cmcourier`: total branch coverage **94.94%**.
  `services/triggers/local_scan.py` at **100%**.
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.*`: clean across 37 source files.
- `pre-commit run --all-files`: clean.
- Smoke: `cmcourier --help` lists **5 commands**
  (csv-trigger-pipeline, rvabrep-pipeline, as400-trigger-pipeline,
  **local-scan-pipeline**, doctor).

### Justificación

- **Closes the production-pipeline set**. Four trigger source
  modes were committed up front; 016 ships the fourth. No more stubs in the
  trigger strategies module — `services/triggers/stubs.py` is
  retired entirely.
- **One trigger record per matched ROW, not per FILE**. A single
  filesystem entry might map to multiple RVABREP rows in pathological
  cases (e.g., the same filename re-archived for a different
  shortname). The downstream `IndexingService` dedupes by
  `(shortname, system_id)` already; emitting per row preserves
  information.
- **`*.PDF` + `*.001` filter is hard-coded**: paged
  documents always have a `.001` first page, native PDFs end in
  `.PDF`. Custom filename patterns (e.g., `.JPG` directly archived)
  are out of scope; operators curate the folder.
- **No `cif_lookup_source` parameter**. The original stub had it as
  a hint at the "cif must be resolved" requirement. Today's
  metadata service handles CIF self-healing centrally — the
  strategy doesn't need its own CIF lookup.
- **Non-recursive scanning**. Recursive support is a one-line
  `Path.rglob` future change; the MVP keeps the iteration surface
  small.
- **CLI omits `--triggers` flag** because local_scan has no
  CSV-trigger override concept. Operators point at a different
  folder by editing the YAML.

---

## [0.17.0] — 2026-05-10 — **Fuentes de metadatos AS400**

Closes the gap left by 014. Pipelines with `as400:<alias>` source
types in `metadata.field_sources` now work end-to-end. The MVP is
fully production-ready: every adapter, every pipeline, every
metadata source kind.

### Agregado

- **`CsvMetadataSourceConfig`** + **`As400MetadataSourceConfig`** —
  two concrete schema classes that tag the `MetadataSourceConfig`
  discriminated union by `kind`. The CSV shape is unchanged in
  semantics (just gains a `kind: Literal["csv"] = "csv"` default).
  The AS400 shape carries `alias`, `as400_connection`, and `table`
  (the prefetch target — `SELECT * FROM <table>` runs at
  `MetadataService` construction).
- **`_build_metadata_sources(sources, secrets) -> dict[str, IDataSource]`**
  helper in `cmcourier.config.wiring`. Dispatches by `kind` and
  builds the right concrete data source (`TabularDataSource` for
  csv, `As400DataSource` for as400). Required AS400 credentials are
  validated at this point — missing values raise
  `ConfigurationError("AS400 credentials required for as400
  metadata source", missing_vars=[...])`.
- **Doctor `_open_metadata_source(source_cfg, secrets)`** helper.
  The existing `_check_metadata_sources` check now opens both csv
  and as400 sources via this dispatcher; the connectivity probe is
  the same `count()` call regardless of kind.
- **9 new tests** across schema, wiring, and doctor (5 schema for
  the discriminated union, 2 wiring for the kind-dispatch + missing-
  secret branch, 2 doctor for mixed-source happy paths).

### Cambiado

- **`_inject_default_trigger_kind` renamed to `_inject_default_kinds`**
  and extended to inject `kind: "csv"` into each `metadata.sources[i]`
  that omits it. Existing 012/013 configs continue to load
  unchanged.
- **`config.wiring._reject_unsupported_source_types` REMOVED**.
  `as400:*` source types in `field_sources` are now legitimate —
  the metadata source registry provides the backing data source,
  and `MetadataService`'s alias-validation catches dangling
  references (unchanged behavior). No prior consumer relied on the
  guard; the removal is safe.
- **`_check_metadata_sources(config, secrets)`** signature gained
  `secrets` so the AS400 branch can supply credentials when opening
  the connection. The csv branch ignores the new argument.
- **`MetadataSourceConfig`** is now a `Annotated[Csv... | As400...,
  Field(discriminator="kind")]` type alias. Existing imports
  (including `MetadataSourceConfig` directly) keep working — the
  alias preserves the name. The legacy single-class shape is now
  `CsvMetadataSourceConfig` and is re-exported under
  `__all__`.

### Verificación

- `pytest -v`: **427 / 427 pass** in ~52 s (421 from earlier + 9
  net new tests across schema/wiring/doctor; 3 obsolete tests
  removed: the `_reject_unsupported_source_types`-era test in
  `test_wiring.py`).
- `pytest --cov=src/cmcourier`: total branch coverage stays above
  95%.
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.*`: clean across 37 source files.
- `pre-commit run --all-files`: clean.
- Smoke: `cmcourier --help` lists 4 commands (unchanged from 014);
  the new metadata-source schema is opt-in (operators add `kind:
  as400` entries when they want).

### Justificación

- **MetadataService unchanged**. The prefetch loop already iterates
  `sources_registry.values()` and calls `IDataSource.get_all()`.
  Both `TabularDataSource` and `As400DataSource` implement
  `IDataSource.get_all()`; the cache key shape
  `(alias, key_column, key_value, value_column)` is naturally
  source-agnostic. No code change, no test change to the service.
  Constitution Principle I (hexagonal architecture) pays off
  exactly here: a new adapter slots in without rippling.
- **Prefetch AS400 sources by default** (per user direction).
  the original `metadata_prefetch_exclude: ["RVABREP"]` excludes
  AS400 by default; 015 deviates because the operator-controlled
  table is usually `CLIENTS` or `ACCOUNTS` (~10s of thousands of
  rows, ~5-50 MB in RAM). A future change can add a per-source
  `prefetch: bool` flag if memory becomes a constraint.
- **Per-field `as400_query` deferred**. The schema supports custom
  SQL per field (`as400_query: "SELECT NOMBRE FROM RVILIB.CLIENT_TABLE
  WHERE CIF = ?"`). 015 simplifies: each AS400 metadata source maps
  to ONE table. Operators who need joins / filters can pre-export
  to a CSV and use a `csv:<alias>` source instead. Custom-SQL
  support is a follow-up change.
- **`_reject_unsupported_source_types` removal is safe**. The
  guard was a placeholder added in 011 because no consumer existed
  for `as400:*` yet. 015 ships the consumer. The MetadataService's
  existing alias-validation catches misconfiguration: a `field_sources[X].sources[i].source_type == "as400:typo"` referencing an
  alias not in `metadata.sources` raises
  `ConfigurationError("unknown CSV alias")` at prefetch time. (The
  error message's "CSV" text is now slightly stale; a cleanup
  rename is queued for a follow-up.)
- **Operator-facing change is small**. A config with a single
  csv-only metadata source needs zero edits (loader injects
  `kind: "csv"`). A config with an AS400 metadata source needs only:
  ```yaml
  metadata:
    sources:
      - kind: as400
        alias: customers
        as400_connection:
          host: 10.x.x.x
        table: CLIENTS
  ```
  plus the existing `AS400_USERNAME`/`AS400_PASSWORD` env vars
  (from 014).

---

## [0.16.0] — 2026-05-10 — **multi-pipeline + AS400 listo para producción**

Largest change of the project. Five thrusts in one PR.

### Agregado

- **`cmcourier.adapters.sources.as400.As400DataSource`** — concrete
  `IDataSource` over pyodbc. Lazy `import pyodbc` inside `_connect()` so
  importing this module never crashes in environments without unixODBC
  headers (failure surfaces on first real call). All pyodbc.Error
  exceptions are wrapped in `IndexingError` with SQLSTATE extracted from
  `exc.args[0]` when the format matches. IN-list queries chunked at 1000
  values. `query_stream` uses `fetchmany(500)`. Single connection per
  instance (thread-local connections deferred + change
  010's single-threaded decision).
- **`cmcourier.services.triggers.as400.As400TriggerStrategy`** —
  real implementation replacing the 006 stub. Runs a configured SQL
  query and yields `TriggerRecord` per row. Blank rows dropped with an
  INFO log of the count. Lives in its own module
  (`services/triggers/as400.py`); the stub at `stubs.py` is removed.
- **`cmcourier rvabrep-pipeline run --config <yaml>`** — new CLI
  command. Verifies `trigger.kind == "rvabrep"` after load_config;
  mismatch exits 2.
- **`cmcourier as400-trigger-pipeline run --config <yaml>`** — new
  CLI command. Same shape; verifies `trigger.kind == "as400"`.
- **Doctor `as400_connectivity` check** — runs when `trigger.kind ==
  "as400"`, opens the AS400 connection + `SELECT 1`. SKIPped when
  kind is csv or rvabrep. Inserted between `cmis_connectivity` and
  `tracking_openable` so connectivity failures cluster at the top.
- **`As400ConnectionConfig`** new Pydantic schema block (host, port,
  database, driver, table). Credentials still env-only.
- **22 new tests**: ~14 AS400 adapter tests with mocked pyodbc, ~5
  schema discriminated-union tests, ~3 wiring + CLI tests for the new
  pipelines, ~1 new doctor test.

### Cambiado

- **`CsvTriggerPipeline` → `StagedPipeline`**. Module renamed via
  `git mv` (`orchestrators/csv_trigger.py` → `orchestrators/staged.py`).
  Class is now generic — the S0 strategy is injected, no longer csv-
  specific. Constitution III rule of three: with the 2nd pipeline
  landing, the abstraction is earned. Every test file referencing the
  old name updated in-place.
- **`TriggerConfig` discriminated union**. `trigger.kind` is the
  discriminator (`csv` | `rvabrep` | `as400`). Three concrete schema
  classes: `CsvTriggerConfig`, `RvabrepTriggerConfig`,
  `As400TriggerConfig`. `TriggerCsvConfig` kept as a backwards-compat
  alias. The loader injects `kind: "csv"` into trigger blocks that
  omit it, so existing change 012 configs continue to load
  unchanged.
- **`build_pipeline` dispatches on `config.trigger.kind`**. Three
  branches: csv (existing), rvabrep (DirectRvabrepTriggerStrategy
  over the existing indexing source), as400 (new As400DataSource +
  As400TriggerStrategy). The as400 branch requires
  `secrets.as400_username` and `secrets.as400_password` to be set;
  missing values raise `ConfigurationError`.
- **CLI `app.py` refactored**. Extracted `_run_pipeline_command(...,
  *, expected_kind=X)` helper used by all three pipeline commands.
- **`As400TriggerStrategy` stub removed** from
  `services/triggers/stubs.py`. The real strategy now lives at
  `services/triggers/as400.py`. The stubs module retains only
  `LocalScanTriggerStrategy`.

### Verificación

- `pytest -v`: **421 / 421 pass** in ~51 s (395 from earlier + 26
  net new across AS400, schema, wiring, CLI, doctor).
- `pytest --cov=src/cmcourier`: total branch coverage stays above
  95%. `adapters/sources/as400.py` ≥ 90%, `services/triggers/as400.py`
  100%, `orchestrators/staged.py` ≥ 96% (renamed but untouched
  logically).
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.*`: clean across 37 source files.
- `pre-commit run --all-files`: clean.
- Smoke: `cmcourier --help` lists 4 commands; each pipeline's
  `--help` lists its flags.

### Justificación

- **AS400 unblocks every `as400:*` consumer**. Even without
  `MetadataService.as400:<alias>` support shipping today, the
  adapter is the gate. Once 014 merges, the next change adds the
  metadata fetch path in ~1 hour.
- **Generic StagedPipeline beats subclassing**. The 5 per-stage
  methods are identical across pipelines; only S0 differs. One
  class + injected strategy is the simplest correct abstraction.
  Subclasses or a mixin would be ~30 LOC of indirection for zero
  added expressiveness.
- **Discriminated union over a single fat `TriggerConfig`**: gives
  operators a clear schema error ("unknown kind: ftp") instead of
  silently accepting fields the wiring won't use. Backwards-compat
  via loader default keeps 012's YAMLs valid.
- **AS400 metadata source deferred to a follow-up**. The wiring
  rejects `as400:*` source types at `build_pipeline` time; the YAML
  schema permits the prefix (operators can document AS400 sources
  before the consumer ships). Splitting MetadataService's
  `_fetch_as400` into its own change keeps 014's blast radius
  bounded.
- **pyodbc lazy import** means the project's CI can install the
  package without ODBC system libraries. Real connection attempts
  fail at the call site with a clear error, not at import time.
- **AS400 retry is the same as CMIS retry**: deferred. The IDataSource
  port doesn't currently mandate a retry policy; AS400 query
  failures bubble up as `IndexingError` and the orchestrator's S1
  trigger-level error handling logs at WARNING and continues.

---

## [0.15.0] — 2026-05-10 — **Comando `doctor` pre-flight**

Operators get a fast pre-flight check before the first real
`csv-trigger-pipeline run`. A mis-configured pipeline that previously
failed 5-30 s in (after side effects had started) now fails in under
5 seconds with a structured report naming the specific check.

### Agregado

- **`cmcourier doctor --config <yaml>`** — new top-level Click command
  (sibling of `csv-trigger-pipeline`). Runs 6 checks in order and
  prints a `[STATUS] check_name — message` line per check, indented
  details (`key=value`), and a summary line. Exit codes: 0 if no
  FAIL (PASS/WARN/SKIP allowed), 1 on any FAIL, 2 on config error,
  3 on unhandled exception.
- **`cmcourier.cli.doctor`** module with:
  - `CheckStatus` (`enum.StrEnum`): `PASS` / `FAIL` / `WARN` / `SKIP`.
  - `CheckResult` (frozen+slots): `name`, `status`, `message`,
    `details: Mapping[str, str]`.
  - `DoctorReport` (frozen+slots): `results`, `elapsed_seconds`, plus
    `passed_count` / `failed_count` / `warn_count` / `skip_count` /
    `has_failures` properties.
  - `run_doctor(config, secrets) -> DoctorReport` — entry point that
    never raises; per-check exceptions become `FAIL` results.
  - 6 private `_check_*` functions covering:
    1. **`cmis_connectivity`** — warmup + repositoryInfo + non-empty
       `repository_id`.
    2. **`tracking_openable`** — `SQLiteTrackingStore` opens at the
       configured `db_path` and closes cleanly.
    3. **`mapping_completeness`** — Modelo Documental has ≥1 row
       (WARN if zero, FAIL on adapter exception).
    4. **`metadata_sources`** — every CSV alias has ≥1 row.
    5. **`cm_type_alignment`** — every distinct `cm_object_type` in
       the mapping resolves via CMIS `getTypeDefinition`. Surfaces
       ALL missing types in one pass. SKIPped if check 1 FAILed.
    6. **`sample_dry_run`** — manually walks S1→S2→S3→S4 on the first
       trigger's first doc, no upload. Cleans up the staged PDF on
       success. SKIPped if zero triggers or zero docs.
- **`IUploader.get_type_definition(object_type_id) -> Mapping[str, Any]`**
  — new abstract method. `CmisUploader` implements via
  `GET {base_url}/{repo_id}?cmisselector=typeDefinition&typeId=<id>`.
  Bypasses the retry loop — pre-flight prefers fail-loud over
  retry-quietly. Raises `CMISClientError` on 4xx (typically 404 for
  missing types) and `CMISServerError` on 5xx.
- **12 integration tests** in `tests/integration/cli/test_doctor.py`
  covering happy path, every check's failure mode, and CLI exit
  codes. Plus 3 new `TestGetTypeDefinition` tests in
  `tests/integration/adapters/test_cmis_uploader.py`.

### Cambiado

- `IUploader` port gains one abstract method (`get_type_definition`).
  `tests/unit/domain/test_ports.py` updated to include it in the
  abstract-method set.
- `src/cmcourier/cli/app.py` gains the `doctor` command + a small
  `_emit_doctor_report(report)` helper.

### Verificación

- `pytest -v`: **395 / 395 pass** in ~65 s (380 from earlier + 15
  new: 12 doctor + 3 type-definition).
- `pytest --cov=src/cmcourier`: total branch coverage **95.94%**.
  `cli/doctor.py` at **93%**; `adapters/upload/cmis_uploader.py`
  at **94%**.
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.*`: clean across 35 source files.
- `pre-commit run --all-files`: clean.
- Smoke: `cmcourier doctor --help` lists `--config` and `--log-level`.

### Justificación

- **Pre-flight or pre-flight**: every operational failure mode
  reachable at validation time is. A 5-second SKIP at the
  trigger-CSV-empty case beats a 60-second batch-load that aborts
  mid-S2.
- **`get_type_definition` bypasses retry**. A 5xx during pre-flight
  is worth surfacing immediately; if it's flaky, the operator
  re-runs doctor — that's the equivalent of "retry once" but with
  human judgement attached. Production uploads still benefit from
  the retry policy.
- **`cm_type_alignment` surfaces ALL missing types** (not
  short-circuit). Operators fix multiple gaps in one round-trip
  instead of running doctor seven times.
- **`sample_dry_run` walks S1-S4 manually**, NOT via the
  orchestrator. The orchestrator would open the tracking store,
  call `start_batch`, etc. — irrelevant side effects for a
  read-only validation. The dry-run accesses the pipeline's
  private collaborator fields (`_trigger_strategy`,
  `_indexing_service`, etc.) — an intentional internal coupling
  that the doctor pays for in exchange for not duplicating the
  full wiring logic from `build_pipeline`.
- **Staged PDF cleaned up**. `contextlib.suppress(OSError)` around
  the unlink keeps doctor as close to "leaves no artifacts" as the
  filesystem allows.
- **No `--skip-doctor` flag on `run`**. Doctor is opt-in. Forcing
  it into the run loop adds latency to every iteration and couples
  two commands; operators run doctor when they want, not as a
  retried side-effect.
- **AS400 connectivity SKIPped**. The adapter doesn't exist yet;
  silently reporting SKIP is honest. When AS400 lands, doctor
  picks up the check by reading `config.cmis` vs a future
  `config.as400`.

---

## [0.14.0] — 2026-05-10 — **CLI del MVP usable de punta a punta**

This release ships the operator-facing layer. With `cmcourier
csv-trigger-pipeline run --config <yaml>`, the MVP pipeline is invokable
without writing Python. Four new modules wrap change 011's orchestrator
under a Pydantic v2 schema, a YAML loader, an adapter factory, and a
Click command. Credentials live exclusively in environment variables.

### Agregado

- `cmcourier.config.schema` — Pydantic v2 model graph for the full
  pipeline. Every model `ConfigDict(frozen=True, extra="forbid")`.
  `FilePath` for required-exists inputs, `Path` for outputs.
- `cmcourier.config.loader` — `load_config(path)` via `yaml.safe_load`
  + `model_validate`; `load_secrets()` reads CMIS_USERNAME /
  CMIS_PASSWORD (required) + AS400_* (optional). Both raise
  `ConfigurationError` with structured context.
- `cmcourier.config.wiring.build_pipeline(config, secrets)` — pure
  factory that opens every TabularDataSource and wires the orchestrator.
  Three private converters translate Pydantic models to the services'
  existing dataclass-based configs.
- `cmcourier.cli.app` — Click root group with one `csv-trigger-pipeline
  run` command. Flags: --config (req), --batch-id, --from-stage,
  --batch-size, --triggers, --log-level. Exit codes 0/1/2/3 per spec.
- `cmcourier.cli.logging_setup.configure(level)` — single stderr
  handler on the root logger; idempotent.
- 43 new tests across schema/loader/wiring/CLI.
- `pyproject.toml`: PyYAML>=6.0,<7.0 runtime; types-PyYAML>=6.0,<7.0 dev.
- `.pre-commit-config.yaml`: types-PyYAML in mypy hook's additional_dependencies.

### Cambiado

- `SQLiteTrackingStore` now explicitly inherits `ITrackingStore`
  (nominal typing for mypy strict at the wiring layer).
- `cmcourier.config.__init__` re-exports PipelineConfig, Secrets,
  load_config, load_secrets, build_pipeline.

### Verificación

- pytest: 380/380 pass in ~62 s.
- coverage: 96.63% total. config/schema.py, config/loader.py,
  config/wiring.py, cli/logging_setup.py all 100%. cli/app.py 86%.
- ruff / mypy / pre-commit: clean.
- Smoke: `cmcourier --help` and `cmcourier csv-trigger-pipeline run
  --help` list the expected commands and flags.

### Justificación

- Pydantic v2 without pydantic-settings (per user direction). Env
  vars read manually — one less dep, zero magic.
- Schema enforces `extra="forbid"` so mis-configuration fails at load
  time, not 30 seconds into a real run.
- Wiring layer owns the schema → service-config translation. Services
  and adapters never import Pydantic — Constitution Principle I.
- `as400:*` rejected at wiring, not at schema. The schema accepts the
  prefix (documentation / future-proofing); the wiring layer enforces
  "do we have an adapter for this?".
- Single stderr logger (tier-based config is a future focused change).
- `SQLiteTrackingStore` now inherits `ITrackingStore` — duck-typing
  worked for tests but tripped mypy at the wiring boundary. The
  remaining adapters (`PdfAssembler`, `CmisUploader`) have the same
  gap and will be cleaned up in a follow-up.

---

## [0.13.0] — 2026-05-10 — **Pipeline MVP de punta a punta**

---

## [0.13.0] — 2026-05-10 — **Pipeline MVP de punta a punta**

This release ships the **first runnable MVP migration pipeline**. With
`CsvTriggerPipeline`, all of S0..S6 are wired against real adapters and
services — no stubs, no placeholders. The orchestrator IS the wiring;
every collaborator it imports has been on `main` since changes 003-010.

### Agregado

- **`cmcourier.orchestrators.csv_trigger.CsvTriggerPipeline`** — the first
  runnable orchestrator. Implements the `csv-trigger-pipeline`
  composition: `S0(csv) → S1 → S2 → S3 → S4 → S5 → S6 (transversal)`.
  Constructor takes the seven collaborators by keyword (`trigger_strategy`,
  `indexing_service`, `mapping_service`, `metadata_service`, `assembler`,
  `uploader`, `tracking_store`); `run()` returns a `RunReport` with
  per-stage counters and elapsed time.
- **`cmcourier.orchestrators.csv_trigger.RunReport`** — frozen+slots
  dataclass with `batch_id`, `total_triggers`, `total_docs`, per-stage
  `_done` / `_failed` counters, `s1_skipped_cross_batch`, and
  `elapsed_seconds`. Counter invariant: `s(N)_done + s(N)_failed == s(N-1)_done`.
- **Cross-batch idempotency**: docs that are already at
  `S5_DONE` in any **prior** batch are skipped silently — no
  `migration_log` row in the new batch, no CMIS calls. Counts toward
  `RunReport.s1_skipped_cross_batch` with an INFO log carrying
  `reason="cross_batch_uploaded"`. If the doc is already at S5_DONE in
  the **current** batch (idempotent rerun), the cross-batch skip does
  NOT fire and the doc flows through stages with per-stage skip-checks.
- **Stage-by-stage resume**: `run(batch_id=..., from_stage=N)`
  reuses an existing batch. S0+S1 still re-execute (re-read CSV, re-index
  RVABREP) but the orchestrator filters the fresh S1 output through
  `tracking_store.list_txn_nums_for_batch(batch_id)` — docs not in the
  prior batch's scope are logged at INFO with `reason="resume_out_of_scope"`
  and dropped. Within each stage, `is_stage_done` (new semantic — see
  Changed) per-doc short-circuits the work for already-done docs.
  Re-running with `from_stage=1` on a completed batch issues ZERO uploads.
- **20 pipeline integration tests** in
  `tests/integration/pipeline/test_csv_trigger_pipeline.py` across 9
  groups: parameter validation, fresh full run, S1 error handling,
  cross-batch skip, per-stage failures (S2/S3/S4/S5), resume (3 modes),
  heterogeneous batch, S0 failure, healed-CIF propagation.
  Branch coverage on `orchestrators/csv_trigger.py`: **96%**.
- **Pipeline test harness** at `tests/integration/pipeline/conftest.py`:
  wires every adapter / service from the existing fixture set, plus a
  `register_cmis_for_docs(txn_nums)` helper that pre-stubs warmup /
  folder creation / upload responses via the `responses` library. Each
  test composes its scenario by writing a trigger CSV under `tmp_path`,
  building a pipeline via the factory, and asserting on the `RunReport`
  plus side effects in the tracking store.
- **Pipeline RVABREP fixture** at `tests/fixtures/pipeline/rvabrep.csv` —
  6 synthetic rows tailored to the orchestrator test scenarios
  (happy path, unmapped id_rvi, missing files, metadata-source-fail,
  CIF self-healing) and pointing at the assembly fixtures from change 009.
- **`ITrackingStore.list_txn_nums_for_batch(batch_id) -> set[str]`** —
  new abstract method. Returns the set of `rvabrep_txn_num` values in
  `migration_log` for the given batch. Unknown batches return `set()`.
  Implemented in `SQLiteTrackingStore` via
  `SELECT DISTINCT rvabrep_txn_num FROM migration_log WHERE batch_id = ?`.
- **`ITrackingStore.flush()` abstract method** — promoted from
  `SQLiteTrackingStore` to the port. Orchestrators call this before any
  read that depends on writes from the same run (the "read my own writes"
  anchor). Synchronous implementations may make this a no-op.

### Cambiado

- **`SQLiteTrackingStore.is_stage_done(txn, batch_id, stage)`** semantic
  changed from "row's `status` field equals exactly `stage.value`" to
  "row has reached at least `stage` in this batch". Implementation now
  uses an `IN (...)` clause against the set of statuses ≥ the requested
  stage (e.g., `is_stage_done(S2_DONE)` returns True for rows currently
  at S2_DONE, S3_PENDING, S3_DONE, S3_FAILED, …, S5_FAILED). The old
  semantic was unusable for resume logic — after S5_DONE, every prior
  `is_stage_done(S(N)_DONE)` would return False because the row's status
  had moved on. Existing 007 tests still pass (they only check
  immediately after `mark_stage_done`); two new tests in
  `TestListTxnNumsForBatch` lock in the new semantic. **This is a
  behavioral change but no public callers existed before this change.**
- `src/cmcourier/orchestrators/__init__.py` re-exports
  `CsvTriggerPipeline` and `RunReport`.
- `src/cmcourier/domain/ports.py` gains two abstract methods on
  `ITrackingStore` (above). `tests/unit/domain/test_ports.py` updated.

### Verificación

- `pytest -v`: **337 / 337 pass** in ~58 s (314 from earlier changes + 20
  pipeline tests + 2 SQLite port-amendment tests + 1 ports test).
- `pytest --cov=src/cmcourier`: total branch coverage **96.07%**;
  `orchestrators/csv_trigger.py` at **96%** (target ≥ 85%);
  `adapters/tracking/sqlite.py` holds at **92%**.
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.*`: clean across 30 source files.
- `pre-commit run --all-files`: ruff (legacy alias), ruff format, mypy
  all pass.

### Justificación

- **First MVP pipeline**. Every adapter and service from changes 003-010
  is now reachable through `CsvTriggerPipeline.run`. The only remaining
  blocker before operators can run real migrations is the CLI + config
  layer (Click command, Pydantic v2 config schema, YAML loader). That is
  the next change, NOT this one.
- **`is_stage_done` semantic redesign justified by real consumer**. The
  exception's first real consumer (the orchestrator) needed "has the doc
  reached at least stage N", not "is the doc currently at stage N
  exactly". The old semantic was speculation — useful for 007's spec but
  unusable for 011. Existing tests survived because they only checked
  the state immediately after a transition. Changing the semantic in
  one place (the adapter) avoids two competing methods on the port.
- **Cross-batch is_uploaded skip checks the SAME batch first**. Without
  this branch, idempotent re-runs (`run(batch_id=existing)`) would treat
  every doc as cross-batch-skipped because `is_uploaded(txn)` queries
  for `S5_DONE` in ANY batch, including the current one. The orchestrator
  preempts this by first asking `is_stage_done(txn, batch_id, S1_DONE)`
  for the current batch; if True, the doc flows through stages with
  per-stage skip-checks. If False, `is_uploaded` is consulted for the
  cross-batch case.
- **Trigger-level errors stay out of `migration_log`**. `RVABREPNotFoundError`
  and `RVABREPDeletedError` fire before any doc identity exists for the
  trigger. Creating a row would force a fake `rvabrep_txn_num`. Logging
  at WARNING with `shortname` + `system_id` is the right granularity;
  trigger-level metrics (how many triggers, how many empty) come from
  the `RunReport.total_triggers` vs `total_docs` ratio.
- **`flush` is part of the port**. The orchestrator needs the
  "read-your-writes" guarantee before reading state it just wrote
  (`is_stage_done` after `mark_stage_done`). Making `flush` abstract
  forces every implementation to declare its consistency model —
  asynchronous stores block, synchronous stores no-op.
- **Resume re-runs S0 + S1 wastefully**. The orchestrator re-reads the
  trigger CSV and re-indexes RVABREP on every resume invocation. A
  more efficient design (rehydrate (trigger, doc) state from
  `migration_log` rows) would require storing more fields per row and
  is a clear post-MVP optimization. The current cost is bounded by
  batch size, and resume is an operator-driven action — not a hot path.
- **Per-stage methods follow the same shape but are not abstracted**.
  Constitution III rule of three: 5 similar `_stage_sN` bodies (~25
  LOC each) is under the abstraction budget. Other pipelines (rvabrep,
  as400, local-scan, single-doc) will reuse most of this shape — when
  the 2nd pipeline lands, the orchestrator's stage skeleton becomes a
  candidate for extraction.

---

## [0.12.0] — 2026-05-10

### Agregado

- **`cmcourier.adapters.upload.cmis_uploader.CmisUploader`** — concrete `IUploader` for IBM Content Manager via the CMIS Browser Binding REST/JSON protocol. Single-threaded MVP: one `requests.Session` shared across calls; thread-local sessions deferred to a follow-up change when the orchestrator's worker pool lands. Holds an in-memory `set[str]` folder cache so a verified or created folder path is never re-POSTed within a process lifetime.
- **Lazy JSESSIONID warmup**: no HTTP at construction time; the first call to `test_connection`, `ensure_folder`, or `upload` issues `GET {base_url}/{repo_id}?cmisselector=repositoryInfo`. Re-warmup fires on any 401 from a subsequent POST.
- **Recursive idempotent folder creation**: `ensure_folder(path)` walks segments left-to-right, skips any segment starting with `$` (system folders like `$type`), and POSTs `createFolder` to the parent for the rest. HTTP 409 (Conflict) is treated as success; the resulting path is still added to the cache. Re-invocation after a successful walk issues zero HTTP calls.
- **Streaming multipart upload** via `requests-toolbelt.MultipartEncoder`. The file is read from disk on demand by the encoder; the adapter never calls `.read()` on the whole stream. Property bag is laid out as `propertyId[N] / propertyValue[N]` pairs in insertion order, with three fixed slots for `cmis:objectTypeId`, `cmis:name`, `cmis:contentStreamMimeType` (the first three triples) and then the caller's `properties` mapping appended starting at index 3.
- **`cmcourier.adapters.upload.cmis_uploader.BandwidthLimiter`** — token-bucket file-stream wrapper with `read`, `seek`, `tell`, `close`, `name`, `__enter__`, `__exit__`. `mbps <= 0` disables throttling (read passthrough). Positive `mbps` throttles to `mbps * 1_000_000` bytes per second via a `time.monotonic()` refill loop. Passthrough methods are required so `MultipartEncoder` introspection works.
- **Complete retry policy**: HTTP 201/2xx → success; HTTP 401 → re-warmup + retry exactly once (a second 401 raises `CMISClientError(status_code=401)`); HTTP 4xx (other) → fail-fast `CMISClientError`; HTTP 5xx → exponential backoff (`retry_base_delay_s * 2**(attempt-1)`, capped at 60 s), up to `retry_max_attempts`; `requests.exceptions.ConnectionError` whose message contains `"10053"` (Windows abort) → `ERROR` log + doubled sleep; retry budget exhausted → `RetriesExhaustedError(txn_num, attempts)` with the last `CMISServerError` as `__cause__`. 409 is handled as success ONLY in `_create_folder_segment`, never in the generic post path.
- **Three-path `cmis:objectId` parser**: `succinctProperties["cmis:objectId"]` → `properties["cmis:objectId"]["value"]` → `str(data.get("id", "unknown"))`. Each fallback is reachable from a real IBM response shape variant. Unparseable JSON returns `"unknown"`.
- **`cmcourier.adapters.upload.cmis_uploader.CmisConfig`** — frozen+slots dataclass with `base_url`, `repo_id`, `username`, `password`, `timeout_seconds=300.0`, `verify_ssl=False`, `max_bandwidth_mbps=0.0`, `retry_max_attempts=3`, `retry_base_delay_s=2.0`.
- **26 integration tests** in `tests/integration/adapters/test_cmis_uploader.py` across 9 groups: config, warmup, `test_connection`, `ensure_folder` (skip `$`, recursive, cache, 409, cached-after-409), upload happy path (3 objectId fallbacks + Content-Type assertion), retry (5xx-then-201, 4xx fail-fast, 401 re-warmup, retries exhausted), Windows-10053 (delay doubling + ERROR log), BandwidthLimiter (throttle + passthrough + passthrough methods), logging discipline. Branch coverage on `cmis_uploader.py`: **94%** (target ≥ 85%).

### Cambiado

- `src/cmcourier/adapters/upload/__init__.py` re-exports `BandwidthLimiter`, `CmisConfig`, `CmisUploader`.
- **`pyproject.toml`** dev deps add `responses>=0.25,<1.0` for HTTP mocking. `responses` is the dev-only library that lets the integration tests exercise the real `requests` stack with the network stubbed — Constitution Principle VI's "no mocking the SUT" applies; `responses` mocks the network, not `requests`.

### Verificación

- `pytest -v`: **314 / 314 pass** in ~36 s (288 from earlier changes + 26 new).
- `pytest --cov=src/cmcourier`: total branch coverage **96.21%**; `adapters/upload/cmis_uploader.py` at **94%**.
- `ruff check`, `ruff format --check`: clean (one `PTH123` lint nudged `open(...)` to `path.open(...)` during verification).
- `mypy --strict on cmcourier.*`: clean across 29 source files.
- `pre-commit run --all-files`: ruff (legacy alias), ruff format, mypy all pass.

### Justificación

- **Stage S5 closes the adapter set** for the MVP `rvabrep-pipeline`. With S0 (triggers), S1 (indexing), S2 (mapping), S3 (metadata), S4 (assembly), S5 (upload), and S6 (tracking) all real, the next change is the orchestrator — every adapter it cables will be production code, not a stub.
- **MVP includes BandwidthLimiter and complete retry policy** (per user direction). Skipping these to ship the adapter faster would mean either a noticeable production retry hole or a flaky first-week dry-run on shared corporate networks. The retry policy is the most heavily-tested area of the adapter precisely because its failure modes are silent and expensive.
- **Single-threaded MVP** (also per user direction): the adapter holds ONE `requests.Session`. The "per thread" requirement becomes load-bearing only when the orchestrator wants worker pools; refactoring to `threading.local()` is a focused, ~10-line change in a follow-up. Shipping it now would mean test fixtures and async patterns we'd be designing around a hypothetical orchestrator instead of a real one.
- **`responses` chosen over `requests-mock`**: same author surface, but `responses` integrates as a pytest fixture / context manager rather than monkey-patching `requests.adapters`. The result is a flat top-down test reading: register stubs → run code → inspect calls. The `responses.add_callback` API also lets us inspect the multipart `Content-Type` boundary without parsing the body.
- **`requests-toolbelt.MultipartEncoder` is non-negotiable**. Loading a 540-page TIFF into memory before POSTing is the production failure mode the domain spec explicitly warns against. The encoder reads the file stream on demand and computes content-length without buffering. Test 4.13 asserts the request header rather than the body bytes because `responses` does not faithfully reproduce multipart wire bytes anyway.
- **409 lives in `_create_folder_segment`, not in `_post_with_retries`**: making the generic retry path treat 409 as success would mask conflicts on document creation (where 409 means a real cmis:name collision, not idempotency). Locality of decision-making beats DRY here.
- **`assert last_exc is not None` before `RetriesExhaustedError(...) from last_exc`** is intentional. `mypy --strict` cannot prove the loop entered, so the assertion satisfies both the type checker and a future reader. The assertion is reachable only if `retry_max_attempts >= 1` (configured default 3); a misconfiguration `retry_max_attempts=0` falls through to the assert as a `AssertionError` — that is acceptable behavior, distinct from a runtime upload failure.
- **Logging discipline (Constitution VIII)**: retry / warn / error logs carry `txn_num`, `attempt`, `status_code`, and `folder_path` via the `extra` dict; no property values, no response bodies beyond a 1024-char truncation. `TestLoggingDiscipline` verifies that a `clbNonGroup.BAC_CIF` value containing the sentinel `BAC_VALUE_THAT_MUST_NOT_LEAK_999999` never appears in any log record across an entire retry cycle.

---

## [0.11.0] — 2026-05-10

### Agregado

- **`cmcourier.adapters.assembly.pdf_assembler.PdfAssembler`** — concrete `IAssembler` for Stage S4. Dispatches on `RVABREPDocument.is_pdf`: native PDFs pass through via `shutil.copy2` to `{temp_dir}/{txn_num}.pdf` with `page_count` read from `doc.total_pages` (we trust RVABREP, do not parse the PDF); paged documents are glob-discovered, sorted by `int(extension)` to handle variable padding, and merged via `img2pdf.convert` (fast path) with a `PIL.Image` + `PyPDF2.PdfMerger` fallback for mixed-content edge cases.
- **`cmcourier.adapters.assembly.pdf_assembler.AssemblerConfig`** — frozen+slots dataclass exposing `source_root`, `temp_dir`, and `image_type_map` (defaults: `B → image/tiff`, `O → application/pdf`, `C → image/jpeg`).
- **OneDrive temp-dir trap**: if `temp_dir` resolves to a `./tmp` variant (`tmp`, `./tmp`, `tmp/`, `.\\tmp`), the assembler diverts to `Path(tempfile.gettempdir()) / "cmcourier_tmp"` and creates the dir at construction time. Constants `_ONEDRIVE_TRAP_VARIANTS` and `_DIVERTED_DIR_NAME` live as module-level frozensets.
- **Page discovery semantics**: glob `FILECODE.*` in the source directory, filter to entries whose extension is purely numeric (`str.isdigit`), sort by `int(extension)`. The native PDF extension `.PDF` is excluded by the digit filter. Missing source dir or zero numeric pages raises `SourceFileMissingError(file_path=...)`. A discovered/expected mismatch emits a `WARNING` log naming `txn_num` + counts but does NOT raise — the filesystem is the source of truth.
- **Dual-path assembly**: img2pdf primary, Pillow + PyPDF2 fallback. The fallback opens each page via `PIL.Image`, converts to RGB if necessary (mode `1` TIFFs cannot save as PDF directly), writes each page as a single-page PDF into a `BytesIO`, and merges via `PdfMerger`. If both paths fail, the assembler raises `PDFAssemblyFailedError(txn_num=..., reason=...)` with the secondary exception as `__cause__`.
- **18 integration tests** in `tests/integration/adapters/test_pdf_assembler.py` across 9 groups: construction, native passthrough, paged happy path (TIFF + JPEG + variable padding + unrelated-PDF exclusion), page-count mismatch WARNING, source-files missing, fallback path (monkey-patched img2pdf), both-paths-fail, output validation (PyPDF2 reader inspection), logging discipline. Branch coverage on `pdf_assembler.py`: **98%** (target ≥ 90%).
- **`tests/integration/adapters/conftest.py`** — session-scoped autouse fixture generator using Pillow to materialize the binary fixtures (TIFF / JPEG / PDF) under `tests/fixtures/assembly/`. Idempotent (skips existing files). Generated binaries are gitignored.
- **`.gitignore`** updated with patterns for the generated assembly fixtures (`tests/fixtures/assembly/**/*.{pdf,PDF,tif,tiff,jpg,jpeg}` plus numeric-extension page files like `.001`, `.10`, `.540`).

### Cambiado

- `src/cmcourier/adapters/assembly/__init__.py` re-exports `PdfAssembler` and `AssemblerConfig`.

### Verificación

- `pytest -v`: **288 / 288 pass** in ~33 s (270 from earlier changes + 18 new).
- `pytest --cov=src/cmcourier`: total branch coverage **96.55%**; `adapters/assembly/pdf_assembler.py` at **98%**.
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.*`: clean across 28 source files (the existing `img2pdf` / `PyPDF2` `ignore_missing_imports` blocks in `pyproject.toml` cover the new module's third-party imports).
- `pre-commit run --all-files`: ruff (legacy alias), ruff format, mypy all pass.

### Justificación

- **Stage S4 is self-contained** — filesystem only, no network, no AS400. With S4 shipping, the only remaining adapter for the MVP `rvabrep-pipeline` is S5 (CMIS upload). Tracking + service triangle + S0 strategies are all already in place.
- **Both assembly paths included in MVP** (per user direction): the Pillow/PyPDF2 fallback adds ~30 LOC and ~2 tests but exercises real `PIL` + `PyPDF2` code under a monkey-patched img2pdf, so the adapter is "fit for purpose" from v1 without leaving a half-shipped fallback to wire up later.
- **`page_count` comes from `doc.total_pages` for native PDFs, from the glob result for paged docs**. Parsing the native PDF would be extra IO with no business value — RVABREP is the authority for the document's intended page count, and the staged PDF is what we ship to CM regardless.
- **Page-count mismatch is a WARNING, not an error**. The filesystem is the source of truth. If a paged document has 540 pages claimed in RVABREP but only 539 on disk, the migration still ships 539 — refusing would block real production data. Operators see the WARNING in tier-2 logs and investigate offline.
- **OneDrive trap baked into the constructor** (not a callable utility) because misconfiguration here destroys throughput silently (locked files, retry storms). Catching it at construction surfaces the diversion immediately in startup logs; tier-3 ops can grep for `temp_dir` divergence.
- **Synthetic-fixture pattern** mirrors change 005 (xlsx generation in `tests/conftest.py`) — binary blobs stay out of git history; regeneration is sub-second and deterministic. This keeps repo size flat and avoids merge conflicts on opaque binaries.
- **PyPDF2 v3 deprecation warning** (`PyPDF2 is deprecated. Please move to the pypdf library instead.`) is acknowledged but accepted for now. A follow-up change can migrate to `pypdf` without touching the assembler's public API; the migration is a constitutional amendment of the `Constraints` section, not a domain change.

---

## [0.10.0] — 2026-05-10

### Agregado

- **`cmcourier.services.indexing.IndexingService`** — concrete Stage S1. Given a `TriggerRecord`, returns every non-deleted `RVABREPDocument` matching `(shortname, system_id)`. CIF is intentionally NOT a filter — CIF self-healing is the responsibility of Stage S3.
- **Two public APIs**: `find_documents(trigger) -> list[RVABREPDocument]` raises `RVABREPNotFoundError` / `RVABREPDeletedError` / `IndexingError`; `find_documents_batch(triggers) -> Iterator[(trigger, list)]` yields one pair per input trigger with empty lists on miss (silent — orchestrators decide semantics). Batched API chunks input into IN-list batches of 50 issuing one `get_by_fields_in` call per chunk.
- **`cmcourier.services.indexing.IndexingColumnsConfig`** — frozen+slots dataclass mapping adapter row keys onto `RVABREPDocument` fields. Defaults match RVABREP physical column names verbatim (`ABABCD`, `ABAACD`, `ABAANB`, `ABACST`, `ABAHCD` = id_rvi, …); tests override every column to the CSV fixture's friendly names.
- **Duplicate `txn_num` handling**: WARNING log + first-wins (mirrors MappingService's first-wins precedent). No exception is raised. Production data quality issues surface in logs, not in the pipeline's error path.
- **Row coercion**: `creation_date` parses via `parse_cymmdd`; `last_view_date` of `'0'` or `''` becomes `None`; `total_pages` coerces to `int` with empty/`None` → `0`; every other field is `str()`-coerced defensively against pandas / pyodbc returning native ints.
- **22 unit tests** in `tests/unit/services/test_indexing.py` across 7 groups (construction, single-trigger, duplicates, batched, coercion, error wrap, logging). Branch coverage on `services/indexing.py`: **96%** (target ≥ 95%).
- **1 fixture CSV** under `tests/fixtures/services/rvabrep_index_sample.csv`: 15 synthetic rows covering vanilla multi-match, fully-deleted, mixed-deleted, duplicate txn_num, same-shortname-across-systems, `last_view_date='0'` / `''`, PDF and paged variants.

### Cambiado

- `src/cmcourier/services/__init__.py` re-exports `IndexingService` and `IndexingColumnsConfig` (alongside the prior 15 public symbols).
- **`cmcourier.domain.exceptions.RVABREPDeletedError`** amended from `(txn_num, delete_code)` to `(shortname, system_id, deleted_count)`. The exception's first real consumer (IndexingService) describes the SET case "every matching row is deleted", not "this specific record is deleted". `tests/unit/domain/test_exceptions.py` updated to assert the new shape. No production code uses the old signature.

### Verificación

- `pytest -v`: **270 / 270 pass** in ~24 s (248 from earlier changes + 22 new).
- `pytest --cov=src/cmcourier`: total branch coverage **96.40%**; `services/indexing.py` at **96%**.
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.*`: clean across 27 source files.
- `pre-commit run --all-files`: ruff (legacy alias), ruff format, mypy all pass.

### Justificación

- **Closes the service triangle**. Mapping (S2, change 004), Metadata (S3, change 005), and now Indexing (S1) are the three services every CMCourier pipeline relies on. With this change, the next milestone is the first orchestrator that wires S0..S6 end-to-end.
- **CIF is NOT a filter here**. CIF self-healing is a Stage S3 responsibility — adding CIF to the WHERE clause would either reject legitimate documents (when the trigger's CIF is missing) or duplicate CIF resolution logic across two stages. Single source of truth wins.
- **Batched API yields empty on miss, not raises**. Single-trigger callers (single-doc pipeline, doctor command) want typed errors. Orchestrator callers want to keep processing the batch — a missing trigger becomes a tracking event, not an exception that aborts the iterator. The two APIs express the two semantics cleanly.
- **One `get_by_fields_in` per chunk, Python-side grouping by `(shortname, system_id)`**: triggers in the same chunk may have different `system_id`s, so passing `system_id` as a fixed filter would over-restrict. The over-fetch is bounded (cardinality of shortnames across systems is small in practice).
- **`RVABREPDeletedError` amendment is justified**: the exception's original `(txn_num, delete_code)` shape modeled a single-doc workflow that hadn't shipped. The set-semantic shape `(shortname, system_id, deleted_count)` matches the actual S1 use case where "every matching row is deleted" is the failure surface. The single-doc pipeline, when it lands, can introduce a separate exception (or extend this one additively) without churn.
- **Logging discipline (Constitution VIII)**: the WARNING for duplicate txn_num carries `shortname` and `duplicate_count` in `extra`, never the values of `cif` / `index2..6`. The test in `TestLoggingDiscipline` asserts that the CIF value `'456789'` from the duplicate fixture row never appears in any log record.

---

## [0.9.0] — 2026-05-10

### Agregado

- **`cmcourier.adapters.tracking.sqlite.SQLiteTrackingStore`** — concrete `ITrackingStore` over stdlib `sqlite3`. Two-connection model (sync reader + async writer daemon thread fed by a `queue.Queue`); WAL journal + `synchronous=OFF` + 64 MiB page cache + temp_store=MEMORY; batched commits up to 500 writes or every 1 s; cross-batch idempotency via the partial index `idx_migration_log_uploaded` on `rvabrep_txn_num WHERE status='S5_DONE'`; within-batch idempotency via the unique index `idx_migration_log_txn_batch` on `(rvabrep_txn_num, batch_id)` plus `INSERT OR IGNORE` on `mark_stage_pending`. `start_batch` is the only synchronous write (returns a UUID4 the caller needs immediately). `flush()` blocks on `queue.join()` for test determinism and orchestrators that need to read state they just wrote. `close()` is idempotent and drains pending writes.
- **`MigrationRecord.batch_id: str`** — new required field on the domain dataclass (`src/cmcourier/domain/models.py`) between `rvabrep_file_name` and `status`. Resolves a port inconsistency where `mark_stage_pending(record, stage)` had no way to know the record's batch — putting it on the record itself is cleaner than amending the port signature.
- **`tests/integration/adapters/test_sqlite_tracking_store.py`** — 25 integration tests against a real per-test SQLite file (no mocks; Constitution Principle VI) across 7 groups: schema, batch lifecycle, per-stage state machine, queries, lifecycle, error wrapping, and the writer's 500-row batch cap. `_make_record(batch_id, txn_num, **overrides)` helper at module level.
- **2 new unit tests** in `tests/unit/domain/test_models.py` covering the new `batch_id` field on `MigrationRecord` (default-value rejection + presence on construction). Existing `MigrationRecord` constructions in the file updated to pass `batch_id="batch-test-001"`.

### Cambiado

- `src/cmcourier/adapters/tracking/__init__.py` re-exports `SQLiteTrackingStore`.

### Verificación

- `pytest -v`: **248 / 248 pass** in ~22 s (222 from earlier changes + 25 new integration tests + 1 new unit test on the new field; net +26).
- `pytest --cov=src/cmcourier`: total branch coverage **96.41 %**; `adapters/tracking/sqlite.py` at **92 %** (target ≥ 90 %).
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.*`: clean across 26 source files.
- `pre-commit run --all-files`: ruff (legacy alias), ruff format, mypy all pass.

### Justificación

- Stage S6 (Tracking) is transversal — every pipeline depends on it. Without it, no orchestrator can resume after a crash, no `is_uploaded` skip-check is possible, and no per-stage retry can be scoped. This change ships the only tracking backend the MVP needs.
- **Two SQLite connections, one writer thread** is the lightest design that simultaneously meets the throughput target (a 200 000-document target on a single process) and respects SQLite's threading rules. WAL coordinates the two connections so a writer never blocks a reader. `synchronous=OFF` is acceptable because every operation is idempotent (Constitution Principle II) — a crashed batch is replayed, not corrupted.
- **`start_batch` is the only synchronous write** because the caller needs the UUID4 immediately to attach to records that flow into subsequent stages. Every other write is `enqueue + return` so orchestrators are not bottlenecked on disk.
- **Idempotency is encoded in the schema**, not in Python: the unique index on `(rvabrep_txn_num, batch_id)` lets `INSERT OR IGNORE` be the entire body of `mark_stage_pending`'s SQL; the partial index on `WHERE status='S5_DONE'` makes `is_uploaded` an O(1) read regardless of how many batches have run. Constitution Principle II is structural in this adapter.
- **`preprocess_staging` and `document_cache` tables are explicitly OUT OF SCOPE** for this change — the 3-phase pipeline and the cross-mode metadata cache that use them are deferred to post-MVP (`docs/roadmap/POST-MVP.md`). Shipping only the two tables the MVP actually needs avoids ALM debt later.
- **Logging discipline (Constitution Principle VIII)**: logs identify operational keys (`txn_num`, `batch_id`) but never field values; `error_message` bodies live in the DB but are never echoed back to logs.

---

## [0.8.0] — 2026-05-10

### Agregado

- **`cmcourier.services.triggers.csv.CsvTriggerStrategy`** — concrete `S0Strategy` over any tabular `IDataSource`. Validates required columns at first row; treats blank `CIF` as `None` (CIF self-healing in stage S3 covers it); skips rows with blank `shortname`/`system_id` with an INFO log of the count. Lazy iteration.
- **`cmcourier.services.triggers.direct_rvabrep.DirectRvabrepTriggerStrategy`** — concrete `S0Strategy` that scans RVABREP itself, with optional `RvabrepFilters(systems, document_types)`. Picks the smaller filter for the IN-list query and rejects the other in Python during iteration. Deduplicates `(shortname, system_id)` pairs (first occurrence wins, matching the MappingService precedent).
- **`cmcourier.services.triggers.stubs.{As400TriggerStrategy, LocalScanTriggerStrategy}`** — concrete `S0Strategy` placeholders. Constructor succeeds; `acquire()` raises `NotImplementedError` with messages naming the missing dependency. Same late-fail pattern used for `as400:<alias>` in 005.
- **3 frozen+slots config dataclasses**: `CsvTriggerColumnsConfig` (defaults match the canonical trigger CSV — `ShortName`, `CIF`, `SystemID`), `RvabrepColumnsConfig` (defaults match RVABREP physical columns — `ABABCD`, `ABACCD`, `ABAACD`, `ABAHCD`), `RvabrepFilters`.
- **21 unit tests** in `tests/unit/services/test_trigger_strategies.py` (3 test classes covering CSV, RVABREP, stubs). All using real `TabularDataSource` over CSV fixtures. Branch coverage on `services/triggers/*`: **100%**.
- **4 fixture CSVs** under `tests/fixtures/services/triggers/`: `trigger_list.csv` (5 rows incl. blanks), `trigger_list_alt_columns.csv` (custom column names), `trigger_list_missing_col.csv` (validates required-column error), `rvabrep_export.csv` (8 rows, 4 unique pairs after dedup).

### Cambiado

- `src/cmcourier/services/__init__.py` re-exports the 7 new public symbols from `triggers/` (in addition to the 8 from `mapping`/`metadata`).

### Verificación

- `pytest -v`: **222 / 222 pass** in ~3 s (201 from earlier changes + 21 new).
- `pytest --cov`: total project branch coverage holds at ≥94%; `services/triggers/*` at **100%**.
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.services.*`: clean across 25 source files.
- `pre-commit run --all-files`: clean.

### Justificación

- Stage S0 (Trigger Acquisition) is the entry point of every pipeline. With S0 unimplemented, no orchestrator could run end-to-end. This change ships the two real strategies needed for the MVP pipelines (`rvabrep-pipeline`, `csv-trigger-pipeline`) and gates the other two with explicit stubs that document the missing dependency.
- **No `TriggerService` wrapper class.** The `S0Strategy` port already represents the trigger-acquisition abstraction; orchestrators in future changes instantiate the appropriate strategy directly per pipeline. The strategies ARE the service.
- The `source_descriptor` parameter on `S0Strategy.acquire()` is silently ignored by every strategy. It's a vestigial port parameter from 002; refining the port to remove it is out of scope (would require an amendment to 002's spec).
- Stubs raise at `acquire()`, not at construction. That lets orchestrators dispatch to them with valid wiring and surface the "missing dependency" error to operators only when the strategy is actually used.

---

## [0.7.0] — 2026-05-10

### Agregado

- **`cmcourier.services.metadata.MetadataService`** — most complex service in CMCourier so far; engine of stage S3 (Metadata Resolution). Per-field fallback chain with validation regexes (`re.fullmatch`), default-value fallback (validated against the first source's regex), CIF self-healing (returns a new `TriggerRecord` since the input is frozen), and field-alias normalization (case-insensitive forward map).
- **Five frozen+slots dataclasses**: `MetadataConfig`, `FieldSourceConfig`, `SourceConfig`, `ValidationConfig`, `MetadataResolution`. Carry the configuration shape and the resolution result.
- **Source types supported**: `trigger` (read TriggerRecord attribute), `rvabrep` (read RVABREPDocument attribute), `csv:<alias>` (lookup via IDataSource). `as400:<alias>` raises `NotImplementedError` with an explicit message naming the missing AS400 adapter — that source type lights up when the AS400 adapter ships.
- **Eager pre-fetching of CSV sources** at construction. Cache keyed by `(alias, key_column, key_value, value_column)` so a single CSV source serves multiple fields without re-iterating. `setdefault` preserves first-occurrence on duplicate keys (matches MappingService's first-wins precedent).
- **CIF self-healing**: if `trigger.cif is None` and `BAC_CIF` is among the canonical fields to resolve, the service resolves `BAC_CIF` first and returns a new `TriggerRecord` with the resolved CIF. Subsequent CSV lookups (which use `trigger.cif` as the lookup key) see the resolved value.
- **`MetadataResolution`** as the typed return shape: `metadata: ResolvedMetadata` + `healed_trigger: TriggerRecord`. Callers (orchestrators, in later changes) MUST use `result.healed_trigger` for subsequent stages.
- **32 unit tests** in `tests/unit/services/test_metadata.py` covering construction + pre-fetch (3), vanilla per source type (3), fallback chain (5), CIF self-healing (4), aliases (3), source dispatch (3), type immutability (2), and edge cases (9). Branch coverage on `metadata.py`: **99%** (target ≥95%).
- **3 CSV fixtures** under `tests/fixtures/services/metadata/`: `clients.csv`, `accounts.csv`, `cards.csv`. Synthetic CIFs (`123456`, `234567`, `345678`) and synthetic names (`JUAN PEREZ TEST`, etc.).

### Cambiado

- **Pre-commit hook bumped**: `.pre-commit-config.yaml` `ruff-pre-commit` rev from `v0.4.10` to `v0.15.12` to align with the local venv's resolved version. Five changes in a row had hit the version drift; this resolves it. Ruff's hook IDs changed slightly (`ruff` → `ruff (legacy alias)`, `ruff-format` → `ruff format`) but behavior is identical.
- `src/cmcourier/services/__init__.py` re-exports the six new public symbols from `metadata` (in addition to the two from `mapping`).

### Verificación

- `pytest -v`: **201 / 201 pass** in ~3 s (169 from earlier changes + 32 new).
- `pytest --cov=src/cmcourier`: total branch coverage **94%+**. Coverage on `services/metadata.py`: **99%**.
- `ruff check`, `ruff format --check`: clean.
- `mypy --strict on cmcourier.services.*`: clean across 21 source files.
- `pre-commit run --all-files`: ruff (legacy alias), ruff format, mypy all pass.

### Justificación

- The metadata layer is the heart of CMCourier's "configurability" promise: every CMIS property comes from the fallback chain, with validation per source and a safety-net default. Without this service, no document can be uploaded with correct metadata.
- **Pre-fetching included in this change (not deferred)**: without it, a 200,000-document migration would fire tens of thousands of point queries against AS400. The pre-fetch is central to the architecture, not an optimization to bolt on later.
- **CIF self-healing returns a new `TriggerRecord` instead of mutating**: domain models are `frozen=True`. The contract is documented and tested; orchestrators threading `healed_trigger` forward is the next change's responsibility.
- **`as400:<alias>` raises `NotImplementedError` with explicit message**: cleaner than partially-implementing it. The handler will be added in one line when the AS400 adapter ships; tests pin the contract today.
- **Logging discipline (Constitution Principle VIII)**: the service logs field NAMES (`BAC_CIF`, `BAC_Nombre_Cliente`) but NEVER field VALUES. Customer name, account number, and CIF VALUES are PII; field names are not.

---

## [0.6.0] — 2026-05-09

### Agregado

- **`cmcourier.services.mapping.MappingService`** — the first service-layer class. Caches the Modelo Documental at construction from any `IDataSource` and exposes `get_mapping(id_rvi)`, `get_all()`, `count()`, and `__contains__`. Stage S2 of every pipeline depends on this lookup, as does the future `doctor` command's mapping-completeness check.
- **`cmcourier.services.mapping.MappingColumnsConfig`** — frozen dataclass for column-name overrides. Defaults match the canonical Modelo Documental columns (`"ID CLASE DOCUMENTAL"`, `"ID RVI"`, `"ID Corto"`, `"CLASE DOCUMENTAL"`, `"METADATOS"`).
- **Duplicate handling**: first occurrence of a repeated `ID RVI` wins; subsequent occurrences are dropped with a `WARNING` log entry naming the duplicate value.
- **Empty-ID-RVI handling**: rows with blank or whitespace-only `ID RVI` cells are silently skipped; the constructor logs an `INFO` line with the skipped count.
- **METADATOS parsing**: comma-separated, whitespace-tolerant, empty-fragment-filtering. `(""," CIF, NUM "," CIF , ", "CIF,", "CIF,,NUM_CUENTA")` all yield clean tuples without surprises.
- **`tests/unit/services/test_mapping.py`** — 21 unit tests using a real `TabularDataSource` over `tests/fixtures/services/modelo_documental.csv` (no IDataSource mocks; the SUT does no I/O so the adapter is wiring, not the system under test). Coverage on `services/mapping.py`: **100 %**.
- **`tests/fixtures/services/modelo_documental.csv`** — 8-row fixture with vanilla rows, METADATOS edge cases (empty, whitespace, trailing comma, doubled comma), one duplicate `ID RVI`, and one empty-ID row.

### Cambiado

- `src/cmcourier/services/__init__.py` re-exports `MappingService` and `MappingColumnsConfig` so callers write `from cmcourier.services import MappingService`.
- README "Status checklist" ticks the fourth-change milestone.

### Verificación

- `pytest -v`: **169 / 169 pass** in 1.32 s (148 from earlier changes + 21 new).
- `pytest --cov=src/cmcourier`: **total branch coverage 95.34 %** (threshold 80 %); `services/mapping.py` 100 %; `domain/*` 95-100 %; `adapters/sources/tabular.py` 96 %.
- `ruff check`, `ruff format --check`, `mypy --strict`: all clean.
- `pre-commit run --all-files`: ruff, ruff-format, mypy all pass.

### Justificación

- **First service layer in CMCourier**. Validates that the hexagonal architecture established by 001-003 holds together end-to-end: `services/mapping.py` imports only `cmcourier.domain.*` (Constitution Principle I); the test wires a real `TabularDataSource` adapter; the service raises the domain-defined `IDRViNotMappedError` on cache miss. Future services (metadata, trigger, document) follow the same shape.
- **Eager-load + dict cache** chosen over lazy-with-cache-miss-query because the Modelo Documental is small (< 1000 rows in practice) and stage S2 needs O(1) lookup at pipeline scale.
- **Field aliases (CIF → BAC_CIF) NOT handled here**. They are the responsibility of the metadata service (next change). Mapping exposes raw names from the source.
- **Logging via stdlib `logging.getLogger(__name__)`** is PII-safe in this layer because `id_rvi` is a document-class code, not customer data. The PII masking helper (`cli/ui/logging.py`, forthcoming) routes the loggers properly when it lands.

---

## [0.5.0] — 2026-05-09

### Agregado

- **`cmcourier.adapters.sources.tabular.TabularDataSource`** — first concrete `IDataSource` implementation. Reads CSV and XLSX files via pandas (with `openpyxl` as the engine for `.xlsx`/`.xls`), exposes the full IDataSource contract minus the SQL methods, and normalizes pandas `NaN` to Python `None` at the port boundary so callers never see pandas-specific sentinels.
- **`tests/integration/adapters/test_tabular_data_source.py`** — 34 integration tests parametrized over CSV / XLSX. Covers the contract methods, lifecycle (`close`, idempotency, post-close access), file-extension dispatch (case-insensitive, unknown rejected), encoding override (latin-1 fixture), and multi-sheet XLSX selection. Branch coverage on the new module: 96 % (target ≥ 90 %).
- **`tests/fixtures/sources/`** — synthetic test fixtures: `sample.csv`, `bad_extension.txt`, `latin1.csv` (committed), and `sample.xlsx` / `multi_sheet.xlsx` (generated at session start by a new `tests/conftest.py` autouse fixture; `*.xlsx` is gitignored to keep binaries out of the repo).
- **`openpyxl>=3.1,<4.0`** added to runtime dependencies — required by `pandas.read_excel` for `.xlsx` files.

### Cambiado

- `tests/conftest.py` now hosts a session-scoped autouse fixture (`_generate_xlsx_fixtures`) that materializes `sample.xlsx` and `multi_sheet.xlsx` at session start if they do not exist. Previously the file held only a docstring.
- `src/cmcourier/adapters/sources/__init__.py` re-exports `TabularDataSource` so callers write `from cmcourier.adapters.sources import TabularDataSource`.
- `.gitignore` excludes `tests/fixtures/sources/*.xlsx` (deterministic regeneration; binary diffs in git are noise).

### Verificación

- `pytest`: **148 / 148 pass** in 2.81 s (112 unit + 34 integration + 2 smoke tests).
- `pytest --cov=src/cmcourier`: **total branch coverage 94.33 %** (threshold 80 %; tabular.py 96 %, domain layer 95-100 %).
- `ruff check`, `ruff format --check`: clean.
- `mypy src/cmcourier/`: clean across 19 source files.
- `pre-commit run --all-files`: ruff, ruff-format, mypy all pass.

### Justificación

- Provides the first concrete adapter so subsequent service-layer changes (004+) have a real `IDataSource` to test against without depending on AS400 — Constitution Principle VI's canonical dev/test substitute. The AS400 adapter, when it lands, implements the same port; both are interchangeable behind the abstraction.
- `query()` and `query_stream()` raise `NotImplementedError` with explicit messages rather than fake SQL via `pandasql` or `duckdb`. The IDataSource port is broad enough to cover both AS400 (SQL) and tabular (field-based) use cases; service code that calls `query()` knows it is talking to a SQL-capable adapter. A future ISP refactor of the port can split the SQL methods off if the asymmetry becomes painful.
- `dtype=str` always — preserves leading zeros (`"000456"` does not become integer 456) and unifies type semantics across CSV/XLSX. Type interpretation is a service-layer responsibility via factories, not an adapter concern.
- One class for both formats — they share the IDataSource methods identically; only loading differs. Two classes would duplicate ~80 % of the code without benefit.
- `openpyxl` is a transitive technical consequence of the explicit XLSX scope decision for this change. Not a constitutional amendment.

---

## [0.4.0] — 2026-05-09

### Agregado

- **`cmcourier.domain.models`** — frozen dataclasses (`@dataclass(frozen=True, slots=True)`) for `TriggerRecord`, `RVABREPDocument`, `CMMapping`, `ResolvedMetadata`, `StagedFile`, and `MigrationRecord`. The `StageStatus` enum (subclassing `enum.StrEnum` from Python 3.11) encodes the per-stage state machine with values matching member names so persistence layers can store them directly. Module-level helpers `parse_cymmdd`, `is_pdf_filename`, `compute_cm_folder`, and `compute_cm_object_type` live alongside the models because they are intrinsic to model semantics.
- **`cmcourier.domain.ports`** — abstract interfaces `IDataSource`, `ITrackingStore` (with stage-aware methods `is_stage_done`, `mark_stage_pending`, `mark_stage_done`, `mark_stage_failed`, plus the cross-batch `is_uploaded` idempotency anchor), `IAssembler`, `IUploader`, and `S0Strategy` (the new abstraction for the four trigger source modes). All declared as `abc.ABC` with `@abstractmethod` decorators. Concrete implementations land in 003+.
- **`cmcourier.domain.exceptions`** — typed hierarchy rooted at `CMCourierError`, organized by stage (`TriggerError` S0, `IndexingError` S1 with `RVABREPNotFoundError` / `RVABREPDeletedError` / `RVABREPDuplicateError`, `MappingError` S2 with `IDRViNotMappedError`, `MetadataError` S3 with `SourceFailedError` / `DefaultValidationFailedError`, `AssemblyError` S4 with `SourceFileMissingError` / `PDFAssemblyFailedError`, `UploadError` S5 with `CMISClientError` / `CMISServerError` / `RetriesExhaustedError`, `TrackingError` S6) plus `ConfigurationError`. Every concrete subclass carries explicit named context parameters (`txn_num`, `id_rvi`, `batch_id`, etc.) for structured logging per Constitution Principle VIII.
- **`cmcourier.domain.__init__`** re-exports every public name (35 symbols) so callers write `from cmcourier.domain import IDataSource` regardless of which submodule the symbol lives in. `__all__` is alphabetized.
- **`tests/unit/domain/test_models.py`**, **`test_ports.py`**, **`test_exceptions.py`**, **`test_imports.py`** — 112 unit tests covering construction, validation rejection, frozen-ness, computed properties, helper edge cases (CYYMMDD round-trip, the canonical clase_id → folder/object-type example, etc.), abstract-class semantics, exception hierarchy filtering, structured-context surfacing in `str(exc)`, and complete `__all__` re-export coverage.

### Verificación

- `pytest -m unit -v tests/unit/domain/`: **112 / 112 pass** in 0.17 s.
- `pytest --cov=src/cmcourier/domain`: **98.56 % branch coverage** (target ≥ 95 %).
- `mypy src/cmcourier/`: clean across 18 source files with strict mode applied to `domain/`, `services/`, `orchestrators/`.
- `ruff check src/ tests/`, `ruff format --check`: clean.
- `pre-commit run --all-files`: ruff, ruff-format, and mypy hooks all pass.

### Justificación

- Provides the stable contract that every adapter (003+) and service (004+) will build against. Without this layer, no concrete code can be written without inventing types ad-hoc.
- All dataclasses are `frozen=True, slots=True` to make accidental mutation impossible and to keep per-instance memory footprint small at scale (200 000+ records in flight is plausible).
- Exceptions carry structured context for downstream PII-safe logging in the observability layer without relying on message parsing.
- Constitution Principle I held throughout: zero third-party imports inside `src/cmcourier/domain/`. The only non-stdlib dependencies in test files are `pytest` itself.

---

## [0.3.0] — 2026-05-09

### Agregado

- **`pyproject.toml`** (PEP 621) declaring all runtime and dev dependencies per Constitution §Constraints, with major-version bounds on every package: `pydantic`, `click`, `pyodbc`, `requests`, `requests-toolbelt`, `pandas`, `img2pdf`, `Pillow`, `PyPDF2` (runtime); `pytest`, `pytest-cov`, `ruff`, `mypy`, `pre-commit`, `types-requests`, `pandas-stubs` (dev).
- **`src/cmcourier/`** in src layout (PEP 420) with hexagonal layering visible from day one: `domain/`, `adapters/{sources,tracking,assembly,upload}/`, `services/`, `orchestrators/`, `cli/{commands,ui}/`, `config/`. Every directory has an explicit `__init__.py` with a layer-purpose docstring.
- **`src/cmcourier/__init__.py`** exposes `__version__ = "0.0.0"`.
- **`src/cmcourier/cli/app.py`** Click group placeholder reserving the `cmcourier` binary entry point.
- **`tests/`** with `unit/{domain,services,orchestrators}/` and `integration/{adapters,pipeline}/` mirrors plus `conftest.py` (empty fixtures placeholder) and `tests/test_smoke.py` (asserts package imports and exposes a SemVer `__version__`).
- **`.pre-commit-config.yaml`** with ruff (lint + format), mypy on staged `src/cmcourier/` files, conventional-pre-commit on `commit-msg`, and a custom local hook (`scripts/hooks/no-co-authored-by.sh`) that blocks any commit message containing `Co-Authored-By` (Constitution Principle IX).
- **`scripts/hooks/no-co-authored-by.sh`** — executable Bash hook backing the rule above.
- **`.gitignore`** covering Python build/runtime artifacts, tooling caches, virtualenvs, IDE junk, and operational artifacts (`logs/`, `tmp/`, `staging/`, SQLite tracking files).
- **`.editorconfig`** with 4-space indent, LF endings, UTF-8, trim trailing whitespace, final newline; `*.md` exempt from trailing-space trim; `*.{yml,yaml,json,toml}` use 2-space indent.
- **`docs/INDEX.md`** — canonical map of every documentation artifact in the repository, organized by purpose per the Diátaxis framework. Updated by every change that adds or moves a doc.
- **`docs/how-to/README.md`** — index of how-to guides (problem-oriented "How to use"), with naming convention (`how-to/<task-slug>.md`) and an empty list at MVP start.
- **`docs/explanation/README.md`** — index of explanation documents (understanding-oriented "How it works"), with naming convention (`explanation/<concept-slug>.md`) and a pointer to the canonical domain explanation.
- **README "Getting started"** section populated with prerequisites (including unixODBC-dev / IBM iSeries Access driver requirement for `pyodbc`), install / test / lint / type-check commands, env-var conventions, and a pointer to `docs/INDEX.md`.
- **README "Documentation map"** prominently links `docs/INDEX.md` as the canonical entry point.

### Cambiado

- README "Documentation map" expanded with rows for `docs/INDEX.md`, `docs/how-to/README.md`, `docs/explanation/README.md`.
- README "Status checklist" ticks the `/sdd-init` and Python-skeleton-bootstrap milestones.

### Justificación

- This change executes Phase 0 of the implementation order from the project's domain spec, now under SDD discipline (spec / plan / tasks landed in commits `c908927` and `56a091c`; this commit ships the implementation).
- The skeleton holds **no business logic** — its only purpose is to give every subsequent change a working sandbox. The smoke test (`tests/test_smoke.py`) is the single proof that the scaffolding works: it asserts that `import cmcourier` succeeds and that `__version__` is a SemVer string.
- Pre-commit hooks enforce the constitutional rules from the first commit onward — Conventional Commits, no `Co-Authored-By` trailer, ruff lint + format, mypy on staged files. This is the moment the constitution stops being a document and starts being executable.
- Coverage threshold (80%) is configured but trivially passes on the empty skeleton. It becomes binding the moment the first real code lands.
- Documentation architecture follows the [Diátaxis framework](https://diataxis.fr): docs split by purpose (learn / solve / look up / understand) rather than by topic. We materialize only the two quadrants the user explicitly requested (`how-to`, `explanation`); `tutorials` and `reference` are deferred to natural-content moments per `specs/001-bootstrap-python-skeleton/plan.md §13`.

---

## [0.2.0] — 2026-05-08

### Agregado
- **Domain spec §10 rewritten**: replaced the old "Execution Modes A/B/C" model with a stage-based pipeline architecture. Eight atomic stages (`S0`–`S7`) compose into named pipelines exposed as CLI commands.
- **Domain spec §10.5**: Pre-Flight Validation specification. Automatic before any pipeline run; available as standalone `cmcourier doctor` command.
- **Domain spec §10.6**: TUI by default with PREP / UPLOAD tabs (Rich); `cmcourier background` is the explicit headless exception.
- **Domain spec §10.7**: Adaptive heavy / light upload lanes — design intent recorded, marked as post-MVP feature.
- **Domain spec §11**: CLI surface restructured to match stage-based pipelines. `doctor`, pipelines as commands, `batch` and `inspect` subcommand groups.
- **Domain spec §17.4**: Observability section expanded into five logging tiers (application, pipeline, network, system, slow-ops) with per-tier configuration toggles, bottleneck identification framework, PII discipline.
- **`docs/roadmap/POST-MVP.md`**: New exhaustive roadmap of nine deferred features (adaptive lanes, system metrics, log analysis tooling, AS400 tracking backend, AIMD auto-tuning, additional pipelines, multi-batch parallelism, per-batch bandwidth, cross-batch metadata cache) plus a watchlist. Each entry: intent, design, MVP placeholder, why deferred, acceptance criteria.
- **`README.md`**: project overview, status, documentation map, tech stack, project workflow, status checklist.
- **`CONTRIBUTING.md`**: SDD workflow, branching, conventional commits, PR standards, constitutional amendment procedure pointer.
- **`CHANGELOG.md`**: this file.

### Cambiado
- **Configuration schema**: removed the global `datasource_mode` field. Trigger source is selected by which pipeline command is invoked, not by a config flag.

### Justificación
- The user surfaced a list of design changes that the rewrite should adopt: pipelines as composable stages, modes as commands rather than config, an explicit `doctor` command, TUI everywhere except background, batch-as-first-class with two-batch producer-consumer flow, stage-by-stage execution per batch, exhaustive observability, validatable mapping/metadata configurations.
- Document Class Mapping (`S2`) was promoted to a separate stage from Metadata Resolution (`S3`) so missing mappings and missing metadata produce distinct error classes — better diagnosis, better doctor output.
- The adaptive heavy/light lane design was explicitly deferred to post-MVP after a viability vs complexity trade-off review. Single-lane MVP delivers correct results; adaptive lanes deliver faster results.

---

## [0.1.0] — 2026-05-08

### Agregado
- **`.specify/memory/constitution.md`** ratified at v1.0.0 with nine core principles:
  - I. Hexagonal Architecture is Non-Negotiable
  - II. Idempotency is Sacred
  - III. No God Objects — Decompose by Responsibility
  - IV. Streaming Over Buffering
  - V. Config is the Single Source of Truth
  - VI. Real Test Pyramid (AS400 is not mocked)
  - VII. Spec Before Code
  - VIII. Data Sensitivity is Non-Negotiable
  - IX. Concepts Over Code, Verify Over Assume
- Constraints section: Python 3.11+, Pydantic v2, Click, pyodbc, requests + requests-toolbelt, pandas, img2pdf + Pillow + PyPDF2, SQLite (WAL), pytest, ruff, mypy.
- File and directory conventions per GitHub Spec Kit (`.specify/memory/`, `specs/<NNN-feature-slug>/`).
- Governance section: amendment procedure with SemVer (MAJOR/MINOR/PATCH), enforcement, document precedence chain.
- Project structure under `docs/domain/` (ground truth) and `docs/samples/{csv,excel,responses}/` (reference fixtures from RVIMigration).

### Movido
- The domain spec was moved into `docs/domain/` (preserved as git rename).
- `*.csv`, `*.xlsx`, `EjemploRespuestaCMIS.txt` → `docs/samples/{csv,excel,responses}/` (preserved as git renames).

### Justificación
- The old project (`RVIMigration`) drifted into a 1341-line God Object without immutable principles guiding the work. The constitution exists so the rewrite does not repeat that history.
- Spec Kit was chosen over OpenSpec for file-based, git-versioned SDD artifacts.

---

## Cómo leer este changelog

- **Agregado**: nueva funcionalidad o documentación
- **Cambiado**: comportamiento o documentación existente modificada
- **Deprecado**: comportamiento o funcionalidad en vías de salir
- **Eliminado**: comportamiento o funcionalidad borrada
- **Corregido**: fixes de bugs
- **Seguridad**: cambios relevantes para seguridad
- **Movido**: relocalizaciones de archivos (preservadas como git renames donde sea posible)
- **Justificación**: el *por qué* detrás de un release, cuando no es obvio de las entradas arriba

Las versiones pre-1.0.0 son hitos de documentación. La 1.0.0 va a marcar la primera migración MVP lista para producción.
