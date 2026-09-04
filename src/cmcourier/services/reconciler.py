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
"""

from __future__ import annotations

__all__ = [
    "As400Reconciler",
    "PendingSyncBuffer",
    "PendingSyncItem",
    "PeriodicReconciler",
    "ReconcileConflict",
    "ReconcileResult",
]

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore, NiarvilogRow
from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore
from cmcourier.domain.models import CMMapping, MigrationRecord, RVABREPDocument, Trigger

_log = logging.getLogger(__name__)
_reconcile_log = logging.getLogger("cmcourier.metrics.reconcile")


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
    ) -> None:
        self._sqlite = sqlite_store
        self._as400 = as400_store

    def run_pass(
        self,
        items: list[PendingSyncItem],
        *,
        import_scope: set[str] | None = None,
        stop_event: threading.Event | None = None,
    ) -> ReconcileResult:
        """Una pasada: propaga ``items`` a AS400 e importa filas ``O``
        ajenas del ``import_scope`` a SQLite. Emite el log de la pasada.

        098: resiliente — **nunca levanta excepción** y **nunca pierde un
        ítem**. Una falla per-ítem se aísla (el ítem va a ``requeued``,
        los demás siguen). Si ``stop_event`` se setea, corta entre ítems
        y manda el resto a ``requeued`` para que ``run_one`` los
        re-encole."""
        t0 = time.monotonic()
        try:
            stale = self._as400.cleanup_stale_in_progress()
        except Exception:  # noqa: BLE001 — un cleanup fallido no aborta la pasada
            _log.exception("reconcile: cleanup_stale_in_progress falló")
            stale = 0
        synced_as400: list[str] = []
        conflicts: list[ReconcileConflict] = []
        requeued: list[PendingSyncItem] = []
        failed = 0
        # 117: UNA lectura batcheada (IN chunkeado, 113) para todo el
        # batch — pre-117 era un SELECT por item. Si la lectura falla
        # (AS400 caído), TODO el batch va a requeued (098: nunca se
        # pierde un item).
        states: dict[str, object] = {}
        if items:
            try:
                states = dict(
                    self._as400.read_states_by_txns([it.document.txn_num for it in items])
                )
            except Exception as exc:  # noqa: BLE001
                _log.exception("reconcile: lectura batcheada falló — se re-encola el batch")
                for item in items:
                    failed += 1
                    requeued.append(item)
                    self._log_failure(item, exc)
                items = []
        for idx, item in enumerate(items):
            if stop_event is not None and stop_event.is_set():
                # Corte cooperativo — el resto del batch vuelve al buffer.
                requeued.extend(items[idx:])
                break
            try:
                outcome = self._propagate_item(item, states.get(item.document.txn_num))
            except Exception as exc:  # noqa: BLE001
                # Una falla per-ítem NO arrastra al resto del batch.
                failed += 1
                requeued.append(item)
                self._log_failure(item, exc)
                continue
            if isinstance(outcome, ReconcileConflict):
                conflicts.append(outcome)
            elif outcome == "synced":
                synced_as400.append(item.document.txn_num)
            # "consistent" → AS400 ya estaba al día: ni sync ni conflicto.
        try:
            synced_local = self._import_foreign_uploads(import_scope or set())
        except Exception:  # noqa: BLE001
            _log.exception("reconcile: import_foreign_uploads falló")
            synced_local = []
        result = ReconcileResult(
            synced_to_as400=synced_as400,
            synced_to_local=synced_local,
            conflicts=conflicts,
            stale_cleaned=stale,
            failed=failed,
            requeued=requeued,
        )
        self._log_pass(result, duration_ms=round((time.monotonic() - t0) * 1000.0, 3))
        return result

    # ------------------------------------------------------------- internos

    def _propagate_item(
        self, item: PendingSyncItem, row: object
    ) -> ReconcileConflict | Literal["synced", "consistent"]:
        """117: propaga un doc local a AS400 con UN solo write.

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

    def run_one(self, *, stop_event: threading.Event | None = None) -> ReconcileResult:
        """Una pasada: drena el buffer, reconcilia, y **re-encola** los
        ítems que fallaron o quedaron sin procesar (098 — nunca se pierde
        un ítem drenado). No propaga excepciones."""
        items = self._buffer.drain()
        scope: set[str] = set()
        if self._import_scope_provider is not None:
            scope = self._import_scope_provider()
        try:
            result = self._reconciler.run_pass(items, import_scope=scope, stop_event=stop_event)
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

    def stop(self, *, join_timeout_s: float = 120.0) -> ReconcileResult:
        """Para el daemon y corre la pasada FINAL.

        098: el daemon corta rápido (chequea ``_stop`` entre ítems) y
        re-encola lo que no procesó. La pasada final corre **sin**
        ``stop_event`` — procesa el buffer completo — y en el thread
        no-daemon que la llama, así que el cierre del proceso no la
        puede matar a mitad."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_s)
            self._thread = None
        return self.run_one()  # sin stop_event → drena y procesa TODO
