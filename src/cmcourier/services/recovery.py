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

from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore, NiarvilogRow
from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore, UploadedRecord
from cmcourier.domain.exceptions import IdentityResolutionError, IDRViNotMappedError
from cmcourier.domain.models import RVABREPDocument
from cmcourier.domain.ports import IDataSource
from cmcourier.services.identity import IdentitySlotConfig, ctenum_for
from cmcourier.services.indexing import IndexingService
from cmcourier.services.mapping import MappingService

# 144: ``SyncProgress`` vive en ``sync_progress`` (lo comparte el
# reconciliador); se re-exporta acá para los imports pre-existentes.
from cmcourier.services.sync_progress import ProgressEmitter, SyncProgress

_log = logging.getLogger(__name__)

# 144: cada cuántos INSERT completados se emite progreso en `insertando`.
_INSERT_PROGRESS_EVERY = 50

# 147 REQ-004: la "cadena" que el error de identidad reporta acá. No es una
# cadena de fuentes porque no la hay: el valor lo escribió la corrida original.
_TRACKING_CHAIN: tuple[str, ...] = (
    "cif <- migration_log (SQLite): recorded by the original run; the chain was not re-run",
)


@dataclass(frozen=True, slots=True)
class RecoveryItem:
    """Un txn que no se pudo recuperar, con el motivo."""

    txn_num: str
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Resultado de una corrida de recuperación.

    En dry-run (``apply=False``), ``recovered`` y ``updated`` listan los
    txn que se insertarían / actualizarían — no se escribe nada.

    151 REQ-003: donde antes había dos grupos (``recovered`` /
    ``already_present``) ahora hay cuatro. El viejo ``already_present``
    mezclaba las filas consistentes con las DESACTUALIZADAS detrás de un
    conteo que sonaba a éxito: un doc ``S5_DONE`` local que en el AS400
    había quedado en ``'F'`` se reportaba como "ya presente" y se dejaba
    divergente para siempre.

    * ``recovered`` — ausentes en NIARVILOG: INSERT ``'O'``.
    * ``updated`` — presentes con ``STSCOD != 'O'``: UPDATE a ``'O'``.
    * ``consistent`` — presentes con ``'O'`` y el MISMO ``OBJIDN``: nada.
    * ``divergent`` — presentes con ``'O'`` y OTRO ``OBJIDN``: dos
      objetos en CM para el mismo documento. No se toca (REQ-001:
      ninguna dirección decide sola), lo resuelve el operador.
    """

    recovered: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    consistent: list[str] = field(default_factory=list)
    divergent: list[RecoveryItem] = field(default_factory=list)
    unrecoverable: list[RecoveryItem] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Classified:
    """151 REQ-003: el reparto de los docs ``S5_DONE`` locales contra el
    estado real de NIARVILOG."""

    missing: list[UploadedRecord] = field(default_factory=list)
    stale: list[UploadedRecord] = field(default_factory=list)
    consistent: list[str] = field(default_factory=list)
    divergent: list[RecoveryItem] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _InsertPlan:
    """144: un doc faltante con TODOS sus campos ya re-derivados — lo
    único que le queda es el INSERT (en dry-run, ni eso)."""

    rec: UploadedRecord
    document: RVABREPDocument
    idnbac: str
    tipidn: str
    # 147 REQ-004: ``None`` ⇒ NULL. Nunca un ``0`` inventado.
    ctenum: int | None = None


def _classify(records: list[UploadedRecord], present: dict[str, NiarvilogRow]) -> _Classified:
    """151 REQ-003: reparte los ``S5_DONE`` locales según el ESTADO real de
    NIARVILOG, no según la mera presencia de la clave.

    ``read_states_by_txns`` siempre devolvió el ``STSCOD``; hasta 151 el
    recover lo descartaba y sólo miraba ``txn in present``.

    REQ-001 aplicado: el lado local manda sobre los documentos que
    CMCourier procesó — y estos son todos ``S5_DONE`` locales —, así que
    una fila ausente o desactualizada se corrige. Pero un ``'O'`` con OTRO
    ``OBJIDN`` no es "desactualizado": es el AS400 afirmando que existe un
    objeto en CM que no es el nuestro. Ahí ninguna dirección decide sola,
    se reporta.
    """
    groups = _Classified()
    for rec in records:
        row = present.get(rec.txn_num)
        if row is None:
            groups.missing.append(rec)
        elif row.stscod != "O":
            groups.stale.append(rec)
        elif row.objidn == rec.cm_object_id:
            groups.consistent.append(rec.txn_num)
        else:
            groups.divergent.append(
                RecoveryItem(
                    rec.txn_num,
                    f"objidn_mismatch: AS400 STSCOD='O' OBJIDN={row.objidn!r} "
                    f"vs local cm_object_id={rec.cm_object_id!r}",
                )
            )
    return groups


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
        cif_slot: IdentitySlotConfig | None = None,
    ) -> None:
        self._sqlite = sqlite_store
        self._as400 = as400_store
        self._indexing = indexing_service
        self._mapping = mapping_service
        self._rvabrep_source = rvabrep_source
        self._write_workers = max(1, write_workers)
        # 147 REQ-004: la política de ``identity.cif`` del YAML (``max_digits``
        # + ``on_missing``). Acá no hay trigger vivo — el CIF sale de
        # ``migration_log`` — así que la cadena no se re-corre, pero la
        # validación sí: es offline y evita repetir el ``22003``.
        self._cif_slot = cif_slot

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
        """Reconcilia SQLite → AS400 por txn: INSERTa las filas ausentes
        (re-derivando los campos que SQLite no guarda) y ACTUALIZA las
        desactualizadas (151 REQ-003). ``apply=False`` (default) es
        dry-run. ``on_progress`` (144) recibe un :class:`SyncProgress` al
        empezar cada fase y, al escribir, cada 50 docs y al final."""
        emit = ProgressEmitter(on_progress)
        emit("leyendo tracking", 0, 0)
        records = self._sqlite.uploaded_records(batch_id)
        # 118: el chequeo de existencia es batcheado (IN chunkeado, 113) —
        # pre-118 era un SELECT por doc. Si AS400 está caído, esto falla de
        # entrada con el error real, no con 100k `unrecoverable` idénticos.
        emit("consultando NIARVILOG", 0, len(records))
        present = self._as400.read_states_by_txns([r.txn_num for r in records])
        # 151 REQ-003: se compara el ESTADO, no la presencia de la clave.
        groups = _classify(records, present)
        # 144: las filas RVABREP de TODOS los faltantes en una llamada. Los
        # `stale` no pasan por acá: el UPDATE va por TRNNUM y no toca
        # DOCFRM / IMGTIP, así que no hay nada que re-derivar.
        emit("consultando RVABREP", 0, len(groups.missing))
        documents: dict[str, RVABREPDocument] = {}
        if groups.missing:
            documents = self._indexing.find_documents_by_txns([r.txn_num for r in groups.missing])
        plans, unrecoverable = self._plan(groups.missing, documents)
        recovered, updated = self._write_or_plan(plans, groups.stale, unrecoverable, emit, apply)
        _log.info(
            "recover: %d a insertar, %d a actualizar, %d consistentes, "
            "%d divergentes, %d no recuperables (apply=%s)",
            len(recovered),
            len(updated),
            len(groups.consistent),
            len(groups.divergent),
            len(unrecoverable),
            apply,
        )
        return RecoveryResult(
            recovered=recovered,
            updated=updated,
            consistent=groups.consistent,
            divergent=groups.divergent,
            unrecoverable=unrecoverable,
        )

    def _write_or_plan(
        self,
        plans: list[_InsertPlan],
        stale: list[UploadedRecord],
        unrecoverable: list[RecoveryItem],
        emit: ProgressEmitter,
        apply: bool,
    ) -> tuple[list[str], list[str]]:
        """Ejecuta (``apply=True``) o sólo enumera (dry-run) las dos
        escrituras del recover. ``unrecoverable`` se extiende in-place con
        los txn que fallaron — un txn malo no aborta al resto."""
        if not apply:
            return [p.rec.txn_num for p in plans], [r.txn_num for r in stale]
        recovered, failed = self._insert_all(plans, emit)
        unrecoverable.extend(failed)
        updated, update_failed = self._update_all(stale, emit)
        unrecoverable.extend(update_failed)
        return recovered, updated

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
        # 145 REQ-001: el sistema viaja en la proyección de audit que el
        # tracking guardó cuando corrió el batch original — no hay trigger
        # vivo del cual sacarlo acá.
        try:
            mapping = self._mapping.get_mapping(document.index7, rec.system_id or None)
        except IDRViNotMappedError:
            return RecoveryItem(rec.txn_num, f"id_rvi_not_mapped:{document.index7}")
        # 147 REQ-004: el CIF se re-valida contra ``identity.cif`` ANTES de
        # planificar el INSERT. Un ``on_missing: fail`` deja el txn como no
        # recuperable con el motivo, en vez de mandar al wire el mismo valor
        # que ya rompió con ``22003``.
        try:
            ctenum = ctenum_for(rec.cif, self._cif_slot, chain=_TRACKING_CHAIN)
        except IdentityResolutionError as exc:
            return RecoveryItem(rec.txn_num, str(exc))
        return _InsertPlan(rec, document, mapping.id_corto, mapping.cmis_type, ctenum)

    def _insert_all(
        self, plans: list[_InsertPlan], emit: ProgressEmitter
    ) -> tuple[list[str], list[RecoveryItem]]:
        """144: ejecuta los INSERT en un pool acotado. Cada hilo del pool
        tiene su conexión (``ThreadLocalConnectionPool``); la semántica
        sigue siendo por fila. ``recovered`` conserva el orden del
        tracking aunque los INSERT terminen desordenados."""
        return self._run_writes(
            "insertando", [p.rec.txn_num for p in plans], lambda i: self._insert_one(plans[i]), emit
        )

    def _update_all(
        self, stale: list[UploadedRecord], emit: ProgressEmitter
    ) -> tuple[list[str], list[RecoveryItem]]:
        """151 REQ-003: lleva a ``'O'`` las filas presentes pero
        desactualizadas. Mismo pool acotado y misma semántica por fila que
        los INSERT — un txn que falla no aborta al resto.

        Sin filas `stale` no emite fase: el operador no tiene por qué ver
        un ``actualizando 0/0`` en cada corrida."""
        if not stale:
            return [], []
        return self._run_writes(
            "actualizando", [r.txn_num for r in stale], lambda i: self._update_one(stale[i]), emit
        )

    def _run_writes(
        self,
        phase: str,
        txns: list[str],
        work: Callable[[int], RecoveryItem | None],
        emit: ProgressEmitter,
    ) -> tuple[list[str], list[RecoveryItem]]:
        """144/151: el pool acotado que comparten INSERT y UPDATE.

        ``work`` recibe el índice del ítem y devuelve ``None`` si salió
        bien. La lista de OK conserva el orden del tracking aunque los
        writes terminen desordenados — el reporte es determinista."""
        total = len(txns)
        emit(phase, 0, total)
        outcomes: list[RecoveryItem | None] = [None] * total
        done = 0
        with ThreadPoolExecutor(
            max_workers=min(self._write_workers, max(1, total)), thread_name_prefix="recover-w"
        ) as pool:
            futures: dict[Future[RecoveryItem | None], int] = {
                pool.submit(work, i): i for i in range(total)
            }
            for future in as_completed(futures):
                outcomes[futures[future]] = future.result()
                done += 1
                if done % _INSERT_PROGRESS_EVERY == 0:
                    emit(phase, done, total)
        if done % _INSERT_PROGRESS_EVERY != 0:
            emit(phase, done, total)
        ok = [t for t, o in zip(txns, outcomes, strict=True) if o is None]
        failed = [o for o in outcomes if o is not None]
        return ok, failed

    def _update_one(self, rec: UploadedRecord) -> RecoveryItem | None:
        """151 REQ-003: UN UPDATE guardado; ``None`` si salió bien.

        ``rowcount == 0`` significa que la guarda ``STSCOD <> 'O'`` no
        matcheó: otro proceso llevó la fila a ``'O'`` entre nuestra lectura
        batcheada y este write. No es un éxito y no se reporta como tal."""
        try:
            rowcount = self._as400.mark_uploaded_if_stale_by_txn(
                trnnum=rec.txn_num, cm_object_id=rec.cm_object_id
            )
        except Exception as exc:  # noqa: BLE001 — un txn malo no aborta el resto
            _log.exception("recover: UPDATE de txn=%s falló inesperadamente", rec.txn_num)
            return RecoveryItem(rec.txn_num, f"error: {exc}")
        if rowcount < 1:
            return RecoveryItem(
                rec.txn_num,
                "stale_update_no_rows: la fila dejó de estar desactualizada entre "
                "la lectura y el UPDATE (otro proceso la tocó)",
            )
        return None

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
                ctenum=plan.ctenum,
                idnbac=plan.idnbac,
                tipidn=plan.tipidn,
                objidn=rec.cm_object_id,
                numrei=rec.retry_count,
            )
        except Exception as exc:  # noqa: BLE001 — un txn malo no aborta el resto
            _log.exception("recover: txn=%s falló inesperadamente", rec.txn_num)
            return RecoveryItem(rec.txn_num, f"error: {exc}")
        return None
