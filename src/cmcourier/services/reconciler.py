"""Reconciliación periódica SQLite ↔ AS400 NIARVILOG (096).

En ``tracking.as400_sync.mode == "periodic"`` la stage S5 no toca AS400:
escribe solo SQLite y **encola** el contexto completo del doc en un
:class:`PendingSyncBuffer`. Un reconciliador de fondo drena ese buffer
cada ``periodic.interval_minutes`` y propaga el estado a AS400.

Diseño elegido (096, opción A — "buffer de contexto"): el INSERT a
NIARVILOG necesita campos (``DOCFRM``, ``IMGTIP``, ``IDNBAC``,
``TIPIDN``) que la tabla ``migration_log`` de SQLite no almacena, así
que el reconciliador NO puede reconstruir el write leyendo SQLite — S5
le pasa el ``record``/``document``/``mapping``/``trigger`` enteros.

El reconciliador clasifica cada doc en tres categorías y emite un log
nuevo (``reconcile-*.jsonl``):

* ``synced_to_as400``  — doc local propagado a AS400.
* ``synced_to_local``  — fila ``O`` de AS400 que otro sistema subió,
  importada a SQLite.
* ``conflict``         — el doc está en las dos bases con estado
  divergente y no lo escribió esta corrida → resolución manual.

144: la propagación a AS400 corre en un pool acotado (``write_workers``,
cada hilo con su conexión vía ``ThreadLocalConnectionPool``) y reporta
progreso por ``on_progress`` — la pasada FINAL de la corrida era un
write + commit por doc, en serie, y muda; el monitor se quedaba en
"corriendo" minutos después del último upload.
"""

from __future__ import annotations

__all__ = [
    "As400Reconciler",
    "PendingSyncBuffer",
    "PendingSyncItem",
    "PeriodicReconciler",
    "ReconcileConflict",
    "ReconcileResult",
    "stop_reconciler_visibly",
]

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Literal

from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore, NiarvilogRow
from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore
from cmcourier.domain.models import CMMapping, MigrationRecord, RVABREPDocument, Trigger
from cmcourier.services.sync_progress import ProgressEmitter, SyncProgress
from cmcourier.services.worker_pool_stats import WorkerPoolStats

_log = logging.getLogger(__name__)
_reconcile_log = logging.getLogger("cmcourier.metrics.reconcile")

# 144: fase que ve el operador durante la pasada final, y cada cuántos
# items completados se re-emite.
SYNC_PHASE_LABEL = "sincronizando AS400"
_PROGRESS_EVERY = 50


@dataclass(frozen=True, slots=True)
class PendingSyncItem:
    """Un doc que S5 completó en mode=periodic, a la espera de propagarse."""

    record: MigrationRecord
    document: RVABREPDocument
    mapping: CMMapping
    trigger: Trigger
    outcome: Literal["uploaded", "failed"]
    cm_object_id: str | None = None
    error: str | None = None


class PendingSyncBuffer:
    """Cola thread-safe de :class:`PendingSyncItem` producidos por S5.

    Los worker threads de S5 hacen ``append``; el reconciliador hace
    ``drain`` (lee + vacía atómicamente).
    """

    def __init__(self) -> None:
        self._items: list[PendingSyncItem] = []
        self._lock = threading.Lock()

    def append(self, item: PendingSyncItem) -> None:
        with self._lock:
            self._items.append(item)

    def drain(self) -> list[PendingSyncItem]:
        with self._lock:
            items = self._items
            self._items = []
        return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass(frozen=True, slots=True)
class ReconcileConflict:
    """Un doc que está en las dos bases con estado divergente."""

    txn_num: str
    local_outcome: str
    local_object_id: str
    as400_stscod: str
    as400_objidn: str


_ItemOutcome = ReconcileConflict | Literal["synced", "consistent"]


@dataclass(slots=True)
class _PassTally:
    """144: acumulador mutable de una pasada — lo llena el hilo llamador
    a medida que los futures del pool terminan (nunca los workers)."""

    synced: list[str] = field(default_factory=list)
    conflicts: list[ReconcileConflict] = field(default_factory=list)
    requeued: list[PendingSyncItem] = field(default_factory=list)
    failed: int = 0


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Resultado de una pasada de reconciliación."""

    synced_to_as400: list[str] = field(default_factory=list)
    synced_to_local: list[str] = field(default_factory=list)
    conflicts: list[ReconcileConflict] = field(default_factory=list)
    stale_cleaned: int = 0
    # 098: ítems cuya reconciliación falló o quedó sin procesar (corte
    # cooperativo). ``run_one`` los re-encola en el buffer — nunca se
    # pierden en silencio.
    failed: int = 0
    requeued: list[PendingSyncItem] = field(default_factory=list)


class As400Reconciler:
    """Corre pasadas de reconciliación SQLite ↔ AS400 (096)."""

    def __init__(
        self,
        *,
        sqlite_store: SQLiteTrackingStore,
        as400_store: As400NiarvilogStore,
        write_workers: int = 8,
    ) -> None:
        self._sqlite = sqlite_store
        self._as400 = as400_store
        # 144: hilos del pool de writes. Cada uno usa su propia conexión
        # ODBC (``ThreadLocalConnectionPool``), igual que los workers S5.
        self._write_workers = max(1, int(write_workers))

    def run_pass(
        self,
        items: list[PendingSyncItem],
        *,
        import_scope: set[str] | None = None,
        stop_event: threading.Event | None = None,
        on_progress: Callable[[SyncProgress], None] | None = None,
    ) -> ReconcileResult:
        """Una pasada: propaga ``items`` a AS400 e importa filas ``O``
        ajenas del ``import_scope`` a SQLite. Emite el log de la pasada.

        098: resiliente — **nunca levanta excepción** y **nunca pierde un
        ítem**. Una falla per-ítem se aísla (el ítem va a ``requeued``,
        los demás siguen). Si ``stop_event`` se setea, corta entre ítems
        y manda el resto a ``requeued`` para que ``run_one`` los
        re-encole.

        144: los writes corren en el pool acotado; ``on_progress``
        recibe ``SyncProgress("sincronizando AS400", k, N)`` al empezar,
        cada 50 items completados y al final — siempre desde ESTE hilo."""
        t0 = time.monotonic()
        try:
            stale = self._as400.cleanup_stale_in_progress()
        except Exception:  # noqa: BLE001 — un cleanup fallido no aborta la pasada
            _log.exception("reconcile: cleanup_stale_in_progress falló")
            stale = 0
        tally = _PassTally()
        states = self._read_states(items, tally)
        if states is None:
            items = []  # la lectura falló: ya está todo en requeued
            states = {}
        self._propagate_all(items, states, stop_event, tally, ProgressEmitter(on_progress))
        try:
            synced_local = self._import_foreign_uploads(import_scope or set())
        except Exception:  # noqa: BLE001
            _log.exception("reconcile: import_foreign_uploads falló")
            synced_local = []
        result = ReconcileResult(
            synced_to_as400=tally.synced,
            synced_to_local=synced_local,
            conflicts=tally.conflicts,
            stale_cleaned=stale,
            failed=tally.failed,
            requeued=tally.requeued,
        )
        self._log_pass(result, duration_ms=round((time.monotonic() - t0) * 1000.0, 3))
        return result

    # ------------------------------------------------------------- internos

    def _read_states(
        self, items: list[PendingSyncItem], tally: _PassTally
    ) -> dict[str, object] | None:
        """117: UNA lectura batcheada (IN chunkeado, 113) para todo el
        batch — pre-117 era un SELECT por item. Si la lectura falla
        (AS400 caído), TODO el batch va a requeued (098: nunca se pierde
        un item) y devuelve ``None``."""
        if not items:
            return {}
        try:
            return dict(self._as400.read_states_by_txns([it.document.txn_num for it in items]))
        except Exception as exc:  # noqa: BLE001
            _log.exception("reconcile: lectura batcheada falló — se re-encola el batch")
            for item in items:
                tally.failed += 1
                tally.requeued.append(item)
                self._log_failure(item, exc)
            return None

    def _propagate_all(
        self,
        items: list[PendingSyncItem],
        states: dict[str, object],
        stop_event: threading.Event | None,
        tally: _PassTally,
        emit: ProgressEmitter,
    ) -> None:
        """144: despacha los writes al pool con una ventana de a lo sumo
        ``write_workers`` en vuelo. La ventana (y no un ``submit`` de
        todo el batch) es lo que mantiene al ``stop_event`` cooperativo:
        se chequea antes de CADA despacho, y lo no despachado vuelve
        al buffer. Los ya despachados terminan — un write a medias no
        se corta."""
        total = len(items)
        emit(SYNC_PHASE_LABEL, 0, total)
        done = next_idx = 0
        pending: dict[Future[_ItemOutcome], PendingSyncItem] = {}
        with ThreadPoolExecutor(
            max_workers=min(self._write_workers, max(1, total)), thread_name_prefix="reconcile-w"
        ) as pool:
            while next_idx < total or pending:
                while next_idx < total and len(pending) < self._write_workers:
                    if stop_event is not None and stop_event.is_set():
                        # Corte cooperativo — el resto del batch vuelve al buffer.
                        tally.requeued.extend(items[next_idx:])
                        next_idx = total
                        break
                    item = items[next_idx]
                    row = states.get(item.document.txn_num)
                    pending[pool.submit(self._propagate_item, item, row)] = item
                    next_idx += 1
                if not pending:
                    break
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    self._collect(pending.pop(future), future, tally)
                    done += 1
                    if done % _PROGRESS_EVERY == 0:
                        emit(SYNC_PHASE_LABEL, done, total)
        if done % _PROGRESS_EVERY != 0:
            emit(SYNC_PHASE_LABEL, done, total)

    def _collect(
        self, item: PendingSyncItem, future: Future[_ItemOutcome], tally: _PassTally
    ) -> None:
        """Clasifica el resultado de UN write. Una falla per-ítem NO
        arrastra al resto del batch (098)."""
        try:
            outcome = future.result()
        except Exception as exc:  # noqa: BLE001
            tally.failed += 1
            tally.requeued.append(item)
            self._log_failure(item, exc)
            return
        if isinstance(outcome, ReconcileConflict):
            tally.conflicts.append(outcome)
        elif outcome == "synced":
            tally.synced.append(item.document.txn_num)
        # "consistent" → AS400 ya estaba al día: ni sync ni conflicto.

    def _propagate_item(self, item: PendingSyncItem, row: object) -> _ItemOutcome:
        """117: propaga un doc local a AS400 con UN solo write. 144:
        corre en un hilo del pool — no toca estado compartido.

        ``row`` viene de la lectura batcheada de ``run_pass`` (por
        TRNNUM — convención del banco: máx. una fila por txn, 034 fase
        4). Pre-117 esto era read + try_claim + mark_* — 3 round-trips
        por item; el paso por `'I'` no aportaba nada (el doc ya
        terminó). Devuelve ``"synced"``, ``"consistent"``, o un
        :class:`ReconcileConflict` (estado divergente o race perdida —
        nunca se pisa)."""
        local_oid = item.cm_object_id or ""
        state = row if isinstance(row, NiarvilogRow) else None
        # Estado terminal ya consistente con lo nuestro → nada que hacer.
        if (
            state is not None
            and state.stscod == "O"
            and item.outcome == "uploaded"
            and state.objidn == local_oid
        ):
            return "consistent"
        # AS400 con estado terminal/in-progress que NO es el nuestro →
        # conflicto. No lo pisamos: resolución manual.
        if state is not None and state.stscod in ("O", "I", "F"):
            return ReconcileConflict(
                txn_num=item.document.txn_num,
                local_outcome=item.outcome,
                local_object_id=local_oid,
                as400_stscod=state.stscod,
                as400_objidn=state.objidn,
            )
        stscod = "O" if item.outcome == "uploaded" else "F"
        if state is not None:
            # Fila en 'N' → UPDATE guardado directo al estado terminal.
            synced = self._as400.update_terminal_if_new(
                document=item.document,
                mapping=item.mapping,
                trigger=item.trigger,
                stscod=stscod,
                cm_object_id=local_oid,
                error=item.error or "",
            )
        else:
            # Fila ausente → INSERT directo con el estado terminal.
            synced = self._as400.insert_terminal(
                # 147 REQ-004: la identidad sale del record, no del trigger.
                record=item.record,
                document=item.document,
                mapping=item.mapping,
                trigger=item.trigger,
                stscod=stscod,
                cm_object_id=local_oid,
                error=item.error or "",
            )
        if not synced:
            # Race perdida entre el read batcheado y el write.
            return ReconcileConflict(
                txn_num=item.document.txn_num,
                local_outcome=item.outcome,
                local_object_id=local_oid,
                as400_stscod="I",
                as400_objidn="",
            )
        return "synced"

    def _import_foreign_uploads(self, import_scope: set[str]) -> list[str]:
        """Importa a SQLite las filas ``O`` de AS400 que otro sistema subió.

        117: la lectura del scope es batcheada (un ``IN`` chunkeado en
        lugar de un SELECT por txn)."""
        candidates = [txn for txn in sorted(import_scope) if not self._sqlite.is_uploaded(txn)]
        if not candidates:
            return []
        rows = self._as400.read_states_by_txns(candidates)
        imported: list[str] = []
        for txn in candidates:
            row = rows.get(txn)
            if row is None or row.stscod != "O":
                continue
            self._sqlite.record_external_upload(
                txn_num=row.trnnum,
                file_name=row.imgarc,
                shortname=row.ctecif,
                cif=str(row.ctenum),
                system_id=row.siscod,
                cm_object_id=row.objidn,
            )
            imported.append(txn)
        return imported

    def _log_failure(self, item: PendingSyncItem, exc: BaseException) -> None:
        """098: registra un ítem que falló su reconciliación. Va al app
        log Y al ``reconcile-*.jsonl`` — el ítem se re-encola, no se
        pierde, pero el operador necesita verlo."""
        _log.warning(
            "reconcile: ítem falló txn=%s — se re-encola para reintento (%s)",
            item.document.txn_num,
            exc,
        )
        _reconcile_log.info(
            "reconcile_failure",
            extra={
                "event": "reconcile_failure",
                "txn_num": item.document.txn_num,
                "error": str(exc),
            },
        )

    def _log_pass(self, result: ReconcileResult, *, duration_ms: float) -> None:
        _reconcile_log.info(
            "reconcile_pass",
            extra={
                "event": "reconcile_pass",
                "synced_to_as400": len(result.synced_to_as400),
                "synced_to_local": len(result.synced_to_local),
                "conflicts": len(result.conflicts),
                "failed": result.failed,
                "requeued": len(result.requeued),
                "stale_cleaned": result.stale_cleaned,
                "duration_ms": duration_ms,
            },
        )
        for c in result.conflicts:
            _reconcile_log.info(
                "reconcile_conflict",
                extra={
                    "event": "reconcile_conflict",
                    "txn_num": c.txn_num,
                    "local_outcome": c.local_outcome,
                    "local_object_id": c.local_object_id,
                    "as400_stscod": c.as400_stscod,
                    "as400_objidn": c.as400_objidn,
                    "hint": (
                        f"resolve with: cmcourier sync resolve {c.txn_num} "
                        "--prefer-as400|--prefer-local"
                    ),
                },
            )


class PeriodicReconciler:
    """Daemon thread que dispara una pasada de reconciliación cada
    ``interval_s`` segundos, más una pasada final al pararse (096)."""

    def __init__(
        self,
        *,
        reconciler: As400Reconciler,
        buffer: PendingSyncBuffer,
        interval_s: float,
        import_scope_provider: Callable[[], set[str]] | None = None,
    ) -> None:
        self._reconciler = reconciler
        self._buffer = buffer
        self._interval_s = max(1.0, float(interval_s))
        self._import_scope_provider = import_scope_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="periodic-reconciler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # Event.wait devuelve True cuando se setea el stop → corta el loop.
        # Las pasadas del daemon le pasan ``_stop`` a run_pass para poder
        # cortar rápido entre ítems cuando se pide parar (098).
        while not self._stop.wait(self._interval_s):
            self.run_one(stop_event=self._stop)

    def run_one(
        self,
        *,
        stop_event: threading.Event | None = None,
        on_progress: Callable[[SyncProgress], None] | None = None,
    ) -> ReconcileResult:
        """Una pasada: drena el buffer, reconcilia, y **re-encola** los
        ítems que fallaron o quedaron sin procesar (098 — nunca se pierde
        un ítem drenado). No propaga excepciones. ``on_progress`` (144)
        va tal cual a ``run_pass``."""
        items = self._buffer.drain()
        scope: set[str] = set()
        if self._import_scope_provider is not None:
            scope = self._import_scope_provider()
        try:
            result = self._reconciler.run_pass(
                items, import_scope=scope, stop_event=stop_event, on_progress=on_progress
            )
        except Exception:  # noqa: BLE001 — defensa: run_pass ya no debería levantar
            _log.exception("periodic reconcile pass failed — se re-encola el batch entero")
            # Nada de lo drenado se pierde: vuelve completo al buffer.
            for it in items:
                self._buffer.append(it)
            return ReconcileResult()
        # 098: los fallidos / no procesados vuelven al buffer para reintento.
        for it in result.requeued:
            self._buffer.append(it)
        return result

    def stop(
        self,
        *,
        join_timeout_s: float = 120.0,
        on_progress: Callable[[SyncProgress], None] | None = None,
    ) -> ReconcileResult:
        """Para el daemon y corre la pasada FINAL.

        098: el daemon corta rápido (chequea ``_stop`` entre ítems) y
        re-encola lo que no procesó. La pasada final corre **sin**
        ``stop_event`` — procesa el buffer completo — y en el thread
        no-daemon que la llama, así que el cierre del proceso no la
        puede matar a mitad. 144: sólo ESTA pasada reporta progreso
        (``on_progress``) — es la que el operador ve como cierre."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_s)
            self._thread = None
        # sin stop_event → drena y procesa TODO
        return self.run_one(on_progress=on_progress)


def stop_reconciler_visibly(
    reconciler: PeriodicReconciler, pool_stats: WorkerPoolStats | None
) -> ReconcileResult:
    """144: para el reconciliador publicando la pasada final como fase
    de cierre en ``pool_stats`` — el monitor muestra ``cerrando ·
    sincronizando AS400 k/N`` en lugar de "corriendo".

    Lo comparten los tres orquestadores (streaming, batched, multi-batch).
    ``pool_stats=None`` (dobles de test sin stats) degrada a un ``stop()``
    pelado. La fase se limpia SIEMPRE, incluso si ``stop`` levanta."""
    if pool_stats is None:
        return reconciler.stop()
    pool_stats.set_closing(SYNC_PHASE_LABEL, 0, 0)
    try:
        return reconciler.stop(
            on_progress=lambda ev: pool_stats.set_closing(ev.phase, ev.done, ev.total)
        )
    finally:
        pool_stats.clear_closing()
