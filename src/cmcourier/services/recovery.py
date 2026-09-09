"""Recuperación de filas faltantes en AS400 NIARVILOG (099).

Repara el daño del bug 096 (ver cambio 098): documentos subidos a CM y
marcados ``S5_DONE`` en SQLite, pero sin su fila en NIARVILOG.

El INSERT a NIARVILOG necesita ``DOCFRM`` / ``IMGTIP`` / ``IDNBAC`` /
``TIPIDN`` — campos que ``migration_log`` de SQLite no almacena. La
recuperación los **re-deriva**: ``DOCFRM`` / ``IMGTIP`` desde la fila
RVABREP (vía :class:`IndexingService`), e ``IDNBAC`` / ``TIPIDN`` desde
el mapping (vía :class:`MappingService`). Así los campos quedan
idénticos a los que la corrida original habría escrito.

El recorrido es **resiliente por-txn** — la lección del 098: un txn que
falla no aborta la recuperación de los demás.

144: el recover es batcheado de punta a punta y reporta progreso.
Existencia en NIARVILOG (118) y filas RVABREP se leen en UNA llamada
cada una (IN chunkeado, 113); los INSERT de apply corren en un pool
acotado (``write_workers``, cada hilo con su conexión vía
``ThreadLocalConnectionPool``). El caller recibe :class:`SyncProgress`
por fase vía ``on_progress`` — un callback que levanta se loguea y no
aborta nada.
"""

from __future__ import annotations

__all__ = ["As400Recovery", "RecoveryItem", "RecoveryResult", "SyncProgress"]

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore
from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore, UploadedRecord
from cmcourier.domain.exceptions import IDRViNotMappedError
from cmcourier.domain.models import RVABREPDocument
from cmcourier.domain.ports import IDataSource
from cmcourier.services.indexing import IndexingService
from cmcourier.services.mapping import MappingService

_log = logging.getLogger(__name__)

# 144: cada cuántos INSERT completados se emite progreso en `insertando`.
_INSERT_PROGRESS_EVERY = 50


@dataclass(frozen=True, slots=True)
class SyncProgress:
    """144: un evento de progreso del sync con AS400.

    ``phase`` es texto para el operador (``"insertando"``); ``done`` /
    ``total`` son ``0/0`` en fases sin conteo previo."""

    phase: str
    done: int
    total: int


@dataclass(frozen=True, slots=True)
class RecoveryItem:
    """Un txn que no se pudo recuperar, con el motivo."""

    txn_num: str
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Resultado de una corrida de recuperación.

    En dry-run (``apply=False``), ``recovered`` lista los txn que se
    insertarían — el INSERT no se ejecuta."""

    recovered: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    unrecoverable: list[RecoveryItem] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _InsertPlan:
    """144: un doc faltante con TODOS sus campos ya re-derivados — lo
    único que le queda es el INSERT (en dry-run, ni eso)."""

    rec: UploadedRecord
    document: RVABREPDocument
    idnbac: str
    tipidn: str


class _ProgressEmitter:
    """144: envuelve ``on_progress`` para que un callback roto (UI
    muerta, widget que ya no existe) no aborte el recover."""

    def __init__(self, on_progress: Callable[[SyncProgress], None] | None) -> None:
        self._cb = on_progress

    def __call__(self, phase: str, done: int, total: int) -> None:
        if self._cb is None:
            return
        try:
            self._cb(SyncProgress(phase, done, total))
        except Exception:  # noqa: BLE001 — el progreso es cosmético, el recover no
            _log.exception("recover: on_progress falló en fase %r (%d/%d)", phase, done, total)


class As400Recovery:
    """Recupera en NIARVILOG las filas de docs ``S5_DONE`` ausentes."""

    def __init__(
        self,
        *,
        sqlite_store: SQLiteTrackingStore,
        as400_store: As400NiarvilogStore,
        indexing_service: IndexingService,
        mapping_service: MappingService,
        rvabrep_source: IDataSource | None = None,
        write_workers: int = 8,
    ) -> None:
        self._sqlite = sqlite_store
        self._as400 = as400_store
        self._indexing = indexing_service
        self._mapping = mapping_service
        self._rvabrep_source = rvabrep_source
        self._write_workers = max(1, write_workers)

    def close(self) -> None:
        """128: cierra lo que el wiring construyó para esta recovery (el
        store AS400 y la fuente RVABREP). El SQLite es del caller."""
        self._as400.close()
        if self._rvabrep_source is not None:
            self._rvabrep_source.close()

    def recover(
        self,
        *,
        batch_id: str | None = None,
        apply: bool = False,
        on_progress: Callable[[SyncProgress], None] | None = None,
    ) -> RecoveryResult:
        """Reconcilia SQLite → AS400 por txn.

        Para cada doc ``S5_DONE`` que NIARVILOG no tiene, re-deriva los
        campos faltantes e inserta la fila terminal. ``apply=False``
        (default) es dry-run: arma el plan sin escribir AS400.
        ``on_progress`` (144) recibe un :class:`SyncProgress` al empezar
        cada fase y, en ``insertando``, cada 50 docs y al final."""
        emit = _ProgressEmitter(on_progress)
        emit("leyendo tracking", 0, 0)
        records = self._sqlite.uploaded_records(batch_id)
        # 118: el chequeo de existencia es batcheado (IN chunkeado, 113)
        # — pre-118 era un SELECT por doc, y en el caso común (casi todo
        # ya presente) ese chequeo era el ÚNICO trabajo por doc. Si AS400
        # está caído, esto falla de entrada con el error real — mejor
        # que 100k `unrecoverable` idénticos.
        emit("consultando NIARVILOG", 0, len(records))
        present = self._as400.read_states_by_txns([r.txn_num for r in records])
        already_present = [r.txn_num for r in records if r.txn_num in present]
        missing = [r for r in records if r.txn_num not in present]
        # 144: las filas RVABREP de TODOS los faltantes en una llamada.
        emit("consultando RVABREP", 0, len(missing))
        documents: dict[str, RVABREPDocument] = {}
        if missing:
            documents = self._indexing.find_documents_by_txns([r.txn_num for r in missing])
        plans, unrecoverable = self._plan(missing, documents)
        if apply:
            recovered, failed = self._insert_all(plans, emit)
            unrecoverable.extend(failed)
        else:
            recovered = [p.rec.txn_num for p in plans]
        _log.info(
            "recover: %d a recuperar, %d ya presentes, %d no recuperables (apply=%s)",
            len(recovered),
            len(already_present),
            len(unrecoverable),
            apply,
        )
        return RecoveryResult(
            recovered=recovered,
            already_present=already_present,
            unrecoverable=unrecoverable,
        )

    def _plan(
        self, missing: list[UploadedRecord], documents: dict[str, RVABREPDocument]
    ) -> tuple[list[_InsertPlan], list[RecoveryItem]]:
        """Re-deriva los campos de cada faltante. Un txn que falla queda
        como :class:`RecoveryItem`; el resto sigue."""
        plans: list[_InsertPlan] = []
        unrecoverable: list[RecoveryItem] = []
        for rec in missing:
            try:
                outcome = self._derive(rec, documents.get(rec.txn_num))
            except Exception as exc:  # noqa: BLE001 — un txn malo no aborta el resto
                _log.exception("recover: txn=%s falló inesperadamente", rec.txn_num)
                unrecoverable.append(RecoveryItem(rec.txn_num, f"error: {exc}"))
                continue
            if isinstance(outcome, RecoveryItem):
                unrecoverable.append(outcome)
            else:
                plans.append(outcome)
        return plans, unrecoverable

    def _derive(
        self, rec: UploadedRecord, document: RVABREPDocument | None
    ) -> RecoveryItem | _InsertPlan:
        """Arma el plan de un doc que NIARVILOG no tiene (la ausencia ya
        fue determinada por la lectura batcheada de :meth:`recover`)."""
        # DOCFRM / IMGTIP vienen de la fila RVABREP (leída en batch, 144).
        if document is None:
            return RecoveryItem(rec.txn_num, "rvabrep_row_not_found")
        # Re-derivar IDNBAC / TIPIDN desde el mapping.
        try:
            mapping = self._mapping.get_mapping(document.index7)
        except IDRViNotMappedError:
            return RecoveryItem(rec.txn_num, f"id_rvi_not_mapped:{document.index7}")
        return _InsertPlan(rec, document, mapping.id_corto, mapping.cmis_type)

    def _insert_all(
        self, plans: list[_InsertPlan], emit: _ProgressEmitter
    ) -> tuple[list[str], list[RecoveryItem]]:
        """144: ejecuta los INSERT en un pool acotado. Cada hilo del pool
        tiene su conexión (``ThreadLocalConnectionPool``); la semántica
        sigue siendo por fila. ``recovered`` conserva el orden del
        tracking aunque los INSERT terminen desordenados."""
        total = len(plans)
        emit("insertando", 0, total)
        outcomes: list[RecoveryItem | None] = [None] * total
        done = 0
        with ThreadPoolExecutor(
            max_workers=min(self._write_workers, max(1, total)), thread_name_prefix="recover-w"
        ) as pool:
            futures: dict[Future[RecoveryItem | None], int] = {
                pool.submit(self._insert_one, plan): i for i, plan in enumerate(plans)
            }
            for future in as_completed(futures):
                outcomes[futures[future]] = future.result()
                done += 1
                if done % _INSERT_PROGRESS_EVERY == 0:
                    emit("insertando", done, total)
        if done % _INSERT_PROGRESS_EVERY != 0:
            emit("insertando", done, total)
        recovered = [p.rec.txn_num for p, o in zip(plans, outcomes, strict=True) if o is None]
        failed = [o for o in outcomes if o is not None]
        return recovered, failed

    def _insert_one(self, plan: _InsertPlan) -> RecoveryItem | None:
        """Un INSERT; ``None`` si salió bien. Nunca levanta: el fallo
        vuelve como :class:`RecoveryItem` (un txn malo no aborta el resto)."""
        rec, document = plan.rec, plan.document
        try:
            self._as400.insert_recovered_row(
                siscod=rec.system_id,
                trnnum=rec.txn_num,
                docfrm=document.index7,
                imgarc=rec.file_name,
                imgtip=document.image_type,
                ctecif=rec.shortname,
                ctenum=int(rec.cif) if rec.cif.isdigit() else 0,
                idnbac=plan.idnbac,
                tipidn=plan.tipidn,
                objidn=rec.cm_object_id,
                numrei=rec.retry_count,
            )
        except Exception as exc:  # noqa: BLE001 — un txn malo no aborta el resto
            _log.exception("recover: txn=%s falló inesperadamente", rec.txn_num)
            return RecoveryItem(rec.txn_num, f"error: {exc}")
        return None
