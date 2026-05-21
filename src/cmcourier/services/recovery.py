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
    ) -> None:
        self._sqlite = sqlite_store
        self._as400 = as400_store
        self._indexing = indexing_service
        self._mapping = mapping_service

    def recover(self, *, batch_id: str | None = None, apply: bool = False) -> RecoveryResult:
        """Reconcilia SQLite → AS400 por txn.

        Para cada doc ``S5_DONE`` que NIARVILOG no tiene, re-deriva los
        campos faltantes e inserta la fila terminal. ``apply=False``
        (default) es dry-run: arma el plan sin escribir AS400."""
        recovered: list[str] = []
        already_present: list[str] = []
        unrecoverable: list[RecoveryItem] = []
        for rec in self._sqlite.uploaded_records(batch_id):
            try:
                outcome = self._recover_one(rec, apply=apply)
            except Exception as exc:  # noqa: BLE001 — un txn malo no aborta el resto
                _log.exception("recover: txn=%s falló inesperadamente", rec.txn_num)
                unrecoverable.append(RecoveryItem(rec.txn_num, f"error: {exc}"))
                continue
            if isinstance(outcome, RecoveryItem):
                unrecoverable.append(outcome)
            elif outcome == "already_present":
                already_present.append(rec.txn_num)
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

    def _recover_one(
        self, rec: UploadedRecord, *, apply: bool
    ) -> RecoveryItem | str:
        """Recupera un doc. Devuelve ``"recovered"``, ``"already_present"``
        o un :class:`RecoveryItem` (no recuperable)."""
        if self._as400.read_state_by_txn(trnnum=rec.txn_num) is not None:
            return "already_present"
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
