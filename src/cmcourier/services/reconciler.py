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

from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore
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
    ) -> ReconcileResult:
        """Una pasada: propaga ``items`` a AS400 e importa filas ``O``
        ajenas del ``import_scope`` a SQLite. Emite el log de la pasada."""
        t0 = time.monotonic()
        stale = self._as400.cleanup_stale_in_progress()
        synced_as400: list[str] = []
        conflicts: list[ReconcileConflict] = []
        for item in items:
            outcome = self._reconcile_item(item)
            if isinstance(outcome, ReconcileConflict):
                conflicts.append(outcome)
            elif outcome == "synced":
                synced_as400.append(item.document.txn_num)
            # "consistent" → AS400 ya estaba al día: ni sync ni conflicto.
        synced_local = self._import_foreign_uploads(import_scope or set())
        result = ReconcileResult(
            synced_to_as400=synced_as400,
            synced_to_local=synced_local,
            conflicts=conflicts,
            stale_cleaned=stale,
        )
        self._log_pass(result, duration_ms=round((time.monotonic() - t0) * 1000.0, 3))
        return result

    # ------------------------------------------------------------- internos

    def _reconcile_item(
        self, item: PendingSyncItem
    ) -> ReconcileConflict | Literal["synced", "consistent"]:
        """Propaga un doc local a AS400.

        Devuelve ``"synced"`` si se propagó, ``"consistent"`` si AS400 ya
        estaba al día, o un :class:`ReconcileConflict` si AS400 tiene un
        estado terminal divergente que no se debe pisar."""
        siscod = item.trigger.audit_row().get("system_id") or ""
        local_oid = item.cm_object_id or ""
        row = self._as400.read_state(
            siscod=siscod,
            trnnum=item.document.txn_num,
            docfrm=item.document.index7,
            imgarc=item.document.file_name,
        )
        # Estado terminal ya consistente con lo nuestro → nada que hacer.
        if (
            row is not None
            and row.stscod == "O"
            and item.outcome == "uploaded"
            and row.objidn == local_oid
        ):
            return "consistent"
        # AS400 con estado terminal/in-progress que NO es el nuestro →
        # conflicto. No lo pisamos: resolución manual.
        if row is not None and row.stscod in ("O", "I", "F"):
            return ReconcileConflict(
                txn_num=item.document.txn_num,
                local_outcome=item.outcome,
                local_object_id=local_oid,
                as400_stscod=row.stscod,
                as400_objidn=row.objidn,
            )
        # Fila ausente o en 'N' → reclamamos y propagamos el estado.
        claimed = self._as400.try_claim(
            record=item.record,
            document=item.document,
            mapping=item.mapping,
            trigger=item.trigger,
        )
        if not claimed:
            # Perdimos la race contra otro proceso entre el read y el claim.
            return ReconcileConflict(
                txn_num=item.document.txn_num,
                local_outcome=item.outcome,
                local_object_id=local_oid,
                as400_stscod="I",
                as400_objidn="",
            )
        if item.outcome == "uploaded":
            self._as400.mark_uploaded(
                record=item.record,
                document=item.document,
                mapping=item.mapping,
                trigger=item.trigger,
                cm_object_id=local_oid,
            )
        else:
            self._as400.mark_failed(
                record=item.record,
                document=item.document,
                mapping=item.mapping,
                trigger=item.trigger,
                error=item.error or "",
            )
        return "synced"

    def _import_foreign_uploads(self, import_scope: set[str]) -> list[str]:
        """Importa a SQLite las filas ``O`` de AS400 que otro sistema subió.

        Recorre el ``import_scope`` (txns que la corrida local conoce);
        para cada txn que SQLite no marca como subido pero AS400 sí, copia
        el estado a SQLite."""
        imported: list[str] = []
        for txn in sorted(import_scope):
            if self._sqlite.is_uploaded(txn):
                continue
            row = self._as400.read_state_by_txn(trnnum=txn)
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

    def _log_pass(self, result: ReconcileResult, *, duration_ms: float) -> None:
        _reconcile_log.info(
            "reconcile_pass",
            extra={
                "event": "reconcile_pass",
                "synced_to_as400": len(result.synced_to_as400),
                "synced_to_local": len(result.synced_to_local),
                "conflicts": len(result.conflicts),
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
        while not self._stop.wait(self._interval_s):
            self.run_one()

    def run_one(self) -> ReconcileResult:
        """Una pasada: drena el buffer y reconcilia. No propaga excepciones
        — un AS400 caído no debe tumbar el pipeline."""
        items = self._buffer.drain()
        scope: set[str] = set()
        if self._import_scope_provider is not None:
            scope = self._import_scope_provider()
        try:
            return self._reconciler.run_pass(items, import_scope=scope)
        except Exception:  # noqa: BLE001
            _log.exception("periodic reconcile pass failed")
            return ReconcileResult()

    def stop(self, *, join_timeout_s: float = 30.0) -> ReconcileResult:
        """Para el daemon y corre una pasada FINAL — garantiza que nada
        quede sin sincronizar al terminar la corrida."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_s)
            self._thread = None
        return self.run_one()
