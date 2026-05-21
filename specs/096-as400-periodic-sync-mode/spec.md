# 096 — Modo de sincronización periódico para AS400

## Por qué

El sync AS400 del cambio 034 es **por-documento y sincrónico**: cada
doc en S5 hace `try_claim` antes y `mark_uploaded`/`mark_failed`
después — 2-3 round-trips a DB2-for-i en el critical path del upload
(ver diagnóstico en spec 095). El cambio 095 hace que esos round-trips
**escalen** con los workers, pero siguen estando en el camino caliente:
cada doc paga latencia AS400 antes de poder subir.

Para despliegues **sin migrador competidor** — el banco corriendo solo
CMCourier, sin la implementación Java paralela — ese claim atómico
distribuido es overhead que no compra nada. Esos operadores quieren
otra cosa: que el pipeline suba a CMIS a máxima velocidad usando solo
el tracking local SQLite, y que las dos bases se **reconcilien en
segundo plano cada X minutos**.

Esto introduce un **modo de sync alternativo**, opt-in, que no
reemplaza al actual.

> ⚠️ **Tradeoff explícito y aceptado.** El modo periódico **sacrifica
> la prevención de doble-upload**. El claim atómico por-documento del
> modo `claim` existe para que dos sistemas no suban el mismo doc.
> En modo `periodic`, durante la ventana de X minutos los dos sistemas
> pueden subir el mismo doc — y el conflicto se detecta **después**,
> vía el log de reconciliación, no se previene. Este modo es para
> entornos donde no hay competidor o donde el banco acepta
> reconciliación post-hoc. El modo `claim` sigue siendo el default.

## Qué

### Config

```yaml
tracking:
  as400_sync:
    enabled: true
    mode: claim                  # 096 — "claim" (default, 034) | "periodic"
    periodic:
      interval_minutes: 5        # cada cuánto corre la reconciliación
```

* `mode: claim` → comportamiento pre-096 byte-idéntico (claim atómico
  por-doc).
* `mode: periodic` → S5 NO toca AS400 en el hot path; un reconciliador
  de fondo sincroniza cada `interval_minutes`.
* El bloque `periodic` se valida solo cuando `mode == "periodic"`;
  `interval_minutes` ≥ 1.

### Comportamiento en modo `periodic`

**1. S5 desacoplado de AS400.** El `IdempotencyCoordinator` recibe el
`mode`. En `periodic`:

* `try_claim()` → devuelve `True` sin tocar AS400 (sin claim distribuido).
* `mark_uploaded()` / `mark_failed()` → escriben **solo SQLite**.

S5 corre a la velocidad de SQLite + CMIS, sin round-trips AS400.

**2. Reconciliador de fondo.** Un servicio nuevo `As400Reconciler`.
Una pasada hace:

1. `cleanup_stale_in_progress()` (recicla filas `I` viejas).
2. Lee los registros terminales de SQLite y las filas de AS400 del
   scope.
3. Clasifica cada txn en tres categorías:

   | Categoría | Condición | Acción |
   |---|---|---|
   | `synced_to_as400` | SQLite tiene estado terminal, AS400 no lo refleja (`N` / fila ausente) | UPDATE/INSERT en AS400 |
   | `synced_to_local` | AS400 tiene `O`, SQLite no lo sabe | Escribir el estado en SQLite |
   | `conflict` | Ambas bases tienen estado terminal y **divergen** (`OBJIDN` distinto, o una dice `O` y la otra `F`) y no lo escribió esta corrida | **Solo loguear** — resolución manual |

4. Aplica los syncs de las dos primeras categorías. Los `conflict`
   **nunca** se tocan automáticamente.

**3. Daemon thread.** Cuando `mode == "periodic"`, el orquestador
levanta un thread daemon que dispara una pasada cada
`interval_minutes`, más una **pasada final** al terminar la corrida
(garantiza que nada quede sin sincronizar).

**4. CLI one-shot.** Nuevo subcomando `cmcourier sync reconcile
--config X` que corre **una** pasada manual (reusa `As400Reconciler`).
Para operadores que quieran forzar la reconciliación fuera del
intervalo o sin una corrida activa.

### Log nuevo de reconciliación

Stream nuevo `reconcile-{date}.jsonl`, logger
`cmcourier.metrics.reconcile`, registrado en `observability/setup.py`
junto a `network-` y `metrics-`. Cada pasada emite:

* **Un evento resumen** por pasada:

  ```json
  {"event": "reconcile_pass", "synced_to_as400": 12,
   "synced_to_local": 3, "conflicts": 1, "stale_cleaned": 0,
   "duration_ms": 842.1}
  ```

* **Un evento detalle por cada conflicto** — para que el operador
  sepa exactamente qué resolver a mano:

  ```json
  {"event": "reconcile_conflict", "txn_num": "0001234",
   "sqlite_state": "S5_DONE", "sqlite_object_id": "cm-aaa",
   "as400_stscod": "O", "as400_objidn": "cm-bbb",
   "hint": "resolve with: cmcourier sync resolve 0001234 --prefer-..."}
  ```

El resumen también va a stdout/TUI al cerrar la corrida: "Reconciliación:
X→AS400, Y→local, Z en conflicto (ver reconcile-*.jsonl)".

### Cambios

1. **`schema.py`** — `As400SyncConfig.mode: Literal["claim","periodic"]
   = "claim"` + `As400SyncConfig.periodic: PeriodicSyncConfig | None`
   con `interval_minutes: int = Field(ge=1, default=5)`. Validación
   cruzada: `periodic` requerido cuando `mode == "periodic"`.

2. **`IdempotencyCoordinator`** — nuevo parámetro `mode`. En `periodic`,
   `try_claim` es no-op `True` y `mark_*` saltean la escritura AS400.

3. **`As400Reconciler`** (servicio nuevo, `services/reconciler.py`) —
   encapsula la pasada de tres categorías. Toma `SQLiteTrackingStore`
   + `As400NiarvilogStore`. Reusa `read_state_by_txn` y los `mark_*`
   del store AS400.

4. **`As400NiarvilogStore`** — método nuevo `import_to_local`-side no
   aplica (el reconciliador escribe SQLite directo); pero sí se
   necesita un `mark_uploaded_by_txn` ya existente (034) para el
   push. Verificar que cubre el caso INSERT-si-falta.

5. **Orquestadores** (`staged.py` / `streaming.py`) — cuando
   `mode == "periodic"`, arrancar/parar el daemon thread del
   reconciliador; correr la pasada final en el teardown.

6. **`cli/commands/sync.py`** — subcomando `reconcile`.

7. **`observability/setup.py`** — registrar el logger
   `cmcourier.metrics.reconcile` → `reconcile-{date}.jsonl`.

8. **`wiring.py`** — pasar `mode` al coordinador; construir el
   `As400Reconciler` cuando corresponde.

### Tests

* `test_reconciler.py`: las tres categorías con `pyodbc` fakeado +
  SQLite real. Caso clave: conflicto `OBJIDN` divergente → queda en
  `conflict`, no se pisa.
* `test_idempotency.py`: en `mode=periodic`, `try_claim` no llama a
  AS400; `mark_uploaded` solo escribe SQLite.
* `test_sync.py`: `sync reconcile` corre una pasada y devuelve exit 0;
  exit 2 si `mode != periodic`.
* Schema: default `claim`, opt-in `periodic`, validación de
  `interval_minutes`.
* Daemon: la pasada final corre en el teardown del orquestador.

## Criterios de aceptación

1. `mode: claim` (default) → comportamiento 034/095 byte-idéntico.
2. `mode: periodic` → S5 no emite NINGÚN round-trip a AS400; medible
   por ausencia de `niarvilog_claim_*` en `network-*.jsonl` durante S5.
3. La reconciliación corre cada `interval_minutes` y una vez al final.
4. `reconcile-*.jsonl` tiene un `reconcile_pass` por pasada y un
   `reconcile_conflict` por cada conflicto.
5. Los conflictos NUNCA se resuelven automáticamente.
6. `cmcourier sync reconcile` corre una pasada manual.
7. `pytest -m unit` y `pytest -m integration` pasan.

## Riesgos

* **Pérdida de prevención de doble-upload** — ver el recuadro de
  arriba. Es la propiedad central que este modo sacrifica. El
  proposal y el `docs/how-to/as400-sync.md` deben advertirlo en
  negrita. El default `claim` protege a quien no lea.
* **Ventana de inconsistencia**: entre dos pasadas, AS400 está
  desactualizado hasta `interval_minutes`. Aceptable por diseño —
  es el trade que el operador elige al activar el modo.
* **Scope de la reconciliación**: si la pasada recorre toda la tabla
  NIARVILOG en bancos grandes, puede ser cara. v1 limita el scope a
  los txn que SQLite tocó desde la última pasada (delta), no la tabla
  entera. La reconciliación full-table queda como opción futura.
* **Daemon thread + teardown**: el thread debe pararse limpio ante
  Ctrl-C / excepción. Usar un `threading.Event` para el shutdown y
  un `join()` con timeout en el teardown.
* **Conflicto detectado tarde**: el operador se entera del doble-upload
  recién en el log. Mitigación: el resumen sube a stdout/TUI, no se
  esconde solo en el jsonl.

## ⚠️ Blocker descubierto en implementación (2026-05-20)

Al implementar el `As400Reconciler` apareció un blocker que la fase de
diseño no había resuelto:

**El INSERT a NIARVILOG necesita campos que SQLite no guarda.** Un
INSERT en `RVILIB.NIARVILOG` requiere `DOCFRM` (`document.index7`),
`IMGTIP` (`document.image_type`), `IDNBAC` (`mapping.id_corto`) y
`TIPIDN` (`mapping.cmis_type`). La tabla `migration_log` de SQLite
(`adapters/tracking/sqlite.py:57`) **no almacena ninguno de esos
cuatro** — solo lleva `trigger_*`, `rvabrep_txn_num`,
`rvabrep_file_name`, `cm_object_id`.

Consecuencia: el reconciliador **no puede reconstruir un write a
AS400 leyendo solo SQLite**. Y como en `mode: periodic` S5 nunca hace
`try_claim`, en AS400 **no existe ninguna fila** para esos docs — así
que `mark_uploaded_by_txn` (que solo hace UPDATE...WHERE txn) tampoco
sirve: rowcount siempre 0.

**Las dos salidas (decisión pendiente del operador):**

| Opción | Qué implica |
|---|---|
| **A — Buffer de contexto** (recomendada) | S5 en periodic encola el `(record, document, mapping, trigger, cm_object_id)` completo en un `PendingSyncBuffer` thread-safe. El reconciliador drena ese buffer y tiene todo el contexto para el INSERT. Cambia el modelo mental: no es "comparo dos tablas", es "S5 produce work items, el reconciliador los flushea cada X min". Más simple y correcto. |
| **B — Extender `migration_log`** | Agregar columnas `doc_format`, `image_type`, `id_corto`, `cmis_type` a la tabla SQLite (migración de schema). El reconciliador reconstruye desde SQLite como decía el spec original. Toca el schema de tracking — más invasivo, más riesgo. |

La opción A no afecta a `mode: claim`. La dirección `synced_to_local`
(importar filas que otro sistema subió) sigue necesitando una decisión
sobre cómo materializar un `MigrationRecord` huérfano en SQLite — el
mismo problema que el 034 ya difirió en `sync resolve --prefer-as400`.

## Estado de implementación (2026-05-20)

Blocker resuelto con la **opción A (buffer de contexto)**.

* ✅ Schema (`mode`, `PeriodicSyncConfig`) — implementado y testeado.
* ✅ `IdempotencyCoordinator` modo periodic + `PendingSyncBuffer` —
  S5 escribe SQLite y encola el contexto del doc.
* ✅ `As400Reconciler` + `PeriodicSyncBuffer` + `PeriodicReconciler`
  (daemon thread) — `services/reconciler.py`, 15 tests.
* ✅ `SQLiteTrackingStore.record_external_upload` — para la dirección
  `synced_to_local`.
* ✅ Log `reconcile-*.jsonl` (`cmcourier.metrics.reconcile`) —
  `observability/setup.py`.
* ✅ Integración: `wiring.py` arma el reconciliador; `StagedPipeline`,
  `StreamingOrchestrator` y `MultiBatchOrchestrator` lo arrancan/paran.
* ⏸️ CLI `sync reconcile` — **diferido**. Un proceso CLI standalone
  no tiene el `PendingSyncBuffer` in-process (vive durante una corrida
  del pipeline), así que solo podría correr la dirección de import.
  Es un sub-feature aparte; se planifica por separado.

## Notas

- Depende conceptualmente del cambio **095** pero NO lo bloquea: 095
  optimiza el modo `claim`, 096 agrega el modo `periodic`. Un
  operador puede querer 095 sin 096 (necesita el claim atómico pero
  más rápido) o 096 sin 095 (no le importa el claim). Son ortogonales.
- El `As400Reconciler` reusa la lógica conceptual de
  `IdempotencyCoordinator.preflight_sync` (las tres categorías
  `imported` / `conflict` / `consistent` del 034) — evaluar en la
  fase de diseño si conviene extraer un clasificador común en vez de
  duplicar.
- Relacionado: cambio 034 (sync original + `sync resolve`), spec 095
  (connection pool), `docs/how-to/as400-sync.md` y
  `docs/how-to/testing-as400-sync.md` (ambos necesitan una sección
  nueva para el modo periódico).
