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
"""

from __future__ import annotations

__all__ = ["As400Recovery", "RecoveryItem", "RecoveryResult"]

import logging
from dataclasses import dataclass, field

from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore
from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore, UploadedRecord
from cmcourier.domain.exceptions import IDRViNotMappedError
from cmcourier.domain.ports import IDataSource
from cmcourier.services.indexing import IndexingService
from cmcourier.services.mapping import MappingService

_log = logging.getLogger(__name__)


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
    ) -> None:
        self._sqlite = sqlite_store
        self._as400 = as400_store
        self._indexing = indexing_service
        self._mapping = mapping_service
        self._rvabrep_source = rvabrep_source

    def close(self) -> None:
        """128: cierra lo que el wiring construyó para esta recovery (el
        store AS400 y la fuente RVABREP). El SQLite es del caller."""
        self._as400.close()
        if self._rvabrep_source is not None:
            self._rvabrep_source.close()

    def recover(self, *, batch_id: str | None = None, apply: bool = False) -> RecoveryResult:
        """Reconcilia SQLite → AS400 por txn.

        Para cada doc ``S5_DONE`` que NIARVILOG no tiene, re-deriva los
        campos faltantes e inserta la fila terminal. ``apply=False``
        (default) es dry-run: arma el plan sin escribir AS400."""
        recovered: list[str] = []
        already_present: list[str] = []
        unrecoverable: list[RecoveryItem] = []
        records = self._sqlite.uploaded_records(batch_id)
        # 118: el chequeo de existencia es batcheado (IN chunkeado, 113)
        # — pre-118 era un SELECT por doc, y en el caso común (casi todo
        # ya presente) ese chequeo era el ÚNICO trabajo por doc. Si AS400
        # está caído, esto falla de entrada con el error real — mejor
        # que 100k `unrecoverable` idénticos.
        present = self._as400.read_states_by_txns([r.txn_num for r in records])
        for rec in records:
            if rec.txn_num in present:
                already_present.append(rec.txn_num)
                continue
            try:
                outcome = self._recover_missing(rec, apply=apply)
            except Exception as exc:  # noqa: BLE001 — un txn malo no aborta el resto
                _log.exception("recover: txn=%s falló inesperadamente", rec.txn_num)
                unrecoverable.append(RecoveryItem(rec.txn_num, f"error: {exc}"))
                continue
            if isinstance(outcome, RecoveryItem):
                unrecoverable.append(outcome)
            else:  # "recovered"
                recovered.append(rec.txn_num)
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

    def _recover_missing(self, rec: UploadedRecord, *, apply: bool) -> RecoveryItem | str:
        """Recupera un doc que NIARVILOG no tiene (la ausencia ya fue
        determinada por la lectura batcheada de :meth:`recover`).
        Devuelve ``"recovered"`` o un :class:`RecoveryItem`."""
        # Re-derivar DOCFRM / IMGTIP desde la fila RVABREP.
        document = self._indexing.find_document_by_txn(rec.txn_num)
        if document is None:
            return RecoveryItem(rec.txn_num, "rvabrep_row_not_found")
        # Re-derivar IDNBAC / TIPIDN desde el mapping.
        try:
            mapping = self._mapping.get_mapping(document.index7)
        except IDRViNotMappedError:
            return RecoveryItem(rec.txn_num, f"id_rvi_not_mapped:{document.index7}")
        if apply:
            self._as400.insert_recovered_row(
                siscod=rec.system_id,
                trnnum=rec.txn_num,
                docfrm=document.index7,
                imgarc=rec.file_name,
                imgtip=document.image_type,
                ctecif=rec.shortname,
                ctenum=int(rec.cif) if rec.cif.isdigit() else 0,
                idnbac=mapping.id_corto,
                tipidn=mapping.cmis_type,
                objidn=rec.cm_object_id,
                numrei=rec.retry_count,
            )
        return "recovered"
