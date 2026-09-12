"""Sincronización AS400 → local: traer lo que hicieron los otros (151).

``RVILIB.NIARVILOG`` es el punto de coordinación entre varios programas
que hacen la MISMA migración: CMCourier y el proceso Java del banco. Para
que eso funcione, los dos lados tienen que poder enterarse de lo que hizo
el otro. Hasta 151 esta dirección no existía: todas las lecturas del AS400
partían de una lista de TXN que el lado local ya conocía, y las tres vías
que decían importar (``reconciler`` con su ``import_scope_provider`` que
el wiring nunca pasaba, ``preflight_sync``, ``sync resolve
--prefer-as400``) no escribían una sola fila en producción.

**La regla de autoridad (REQ-001)** gobierna todo lo de acá:

    El lado local manda sobre los documentos que CMCourier procesó.
    El AS400 manda sobre los documentos que CMCourier nunca vio.

De ahí salen las tres decisiones del pull, y sólo esas tres:

* TXN **sin fila terminal local** ⇒ se importa (el AS400 es la autoridad).
* TXN con fila terminal local que **coincide** ⇒ nada.
* TXN con fila terminal local que **NO coincide** ⇒ divergencia: se
  reporta, no se pisa. Ninguna dirección decide sola; lo resuelve el
  operador con ``sync resolve``. Dos direcciones que se pisan entre sí no
  son una sincronización, son una pelea.

Y una restricción de forma: el operador eligió traer TODO, sin rango,
pero *"todo" no puede significar "todo en RAM"*. La lectura del AS400 va
en streaming (``stream_rows_by_status``, ``fetchmany``) y las escrituras
a SQLite van por lotes — el precedente es 148, donde ``get_by_fields_in``
terminaba en ``fetchall()`` y por eso el censo no podía barrer un sistema
entero.
"""

from __future__ import annotations

__all__ = ["As400Pull", "PullItem", "PullResult"]

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore, NiarvilogRow
from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore
from cmcourier.services.sync_progress import ProgressEmitter, SyncProgress

_log = logging.getLogger(__name__)

# Cuántas filas del AS400 se procesan (y se escriben) por vuelta.
_DEFAULT_CHUNK_SIZE = 500

_PHASE = "listando NIARVILOG"

# Qué estado local afirma lo MISMO que cada ``STSCOD`` terminal del AS400.
# Sólo ``'O'`` y ``'F'`` entran al pull: ``'I'`` y ``'N'`` son estados en
# vuelo que cambian solos, e importarlos sería fotografiar algo que ya no
# es cierto.
_AGREEMENT: dict[str, str] = {"O": "S5_DONE", "F": "S5_FAILED"}


@dataclass(frozen=True, slots=True)
class PullItem:
    """Un TXN sobre el que los dos lados afirman cosas distintas."""

    txn_num: str
    reason: str


@dataclass(frozen=True, slots=True)
class PullResult:
    """Resultado de una pasada del pull.

    Los tres primeros son CONTEOS, no listas, a propósito: el pull barre
    la tabla entera y acumular cientos de miles de txn para imprimir un
    número sería tirar por la ventana el streaming que la spec pide. Las
    divergencias sí van completas — son pocas por definición y el
    operador necesita nombrarlas para resolverlas.

    En dry-run (``apply=False``) los conteos dicen qué se importaría.
    """

    scanned: int = 0
    imported_uploaded: int = 0
    imported_failed: int = 0
    consistent: int = 0
    divergent: list[PullItem] = field(default_factory=list)


@dataclass(slots=True)
class _Tally:
    """Acumulador mutable de una pasada (el resultado es inmutable)."""

    scanned: int = 0
    imported_uploaded: int = 0
    imported_failed: int = 0
    consistent: int = 0
    divergent: list[PullItem] = field(default_factory=list)

    def to_result(self) -> PullResult:
        return PullResult(
            scanned=self.scanned,
            imported_uploaded=self.imported_uploaded,
            imported_failed=self.imported_failed,
            consistent=self.consistent,
            divergent=self.divergent,
        )


class As400Pull:
    """Importa a SQLite lo que otros programas dejaron en NIARVILOG."""

    def __init__(
        self,
        *,
        sqlite_store: SQLiteTrackingStore,
        as400_store: As400NiarvilogStore,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> None:
        self._sqlite = sqlite_store
        self._as400 = as400_store
        self._chunk_size = max(1, chunk_size)

    def close(self) -> None:
        """Cierra el store AS400. El SQLite es del caller (mismo criterio
        que :meth:`As400Recovery.close`)."""
        self._as400.close()

    def pull(
        self,
        *,
        apply: bool = False,
        on_progress: Callable[[SyncProgress], None] | None = None,
    ) -> PullResult:
        """Barre NIARVILOG y rellena los huecos del tracking local.

        ``apply=False`` (default) es dry-run: clasifica sin escribir. La
        pasada es idempotente — los INSERT son ``INSERT OR IGNORE`` sobre
        el batch sintético — así que re-correrla tras un corte es seguro.
        """
        emit = ProgressEmitter(on_progress)
        emit(_PHASE, 0, 0)
        tally = _Tally()
        for chunk in self._chunks():
            self._process(chunk, tally, apply=apply)
            tally.scanned += len(chunk)
            if apply:
                # Las escrituras van por lotes: la cola del writer de
                # SQLite no crece con el tamaño de la tabla del AS400.
                self._sqlite.flush()
            emit(_PHASE, tally.scanned, 0)
        _log.info(
            "pull: %d filas leídas, %d importadas 'O', %d importadas 'F', "
            "%d consistentes, %d divergentes (apply=%s)",
            tally.scanned,
            tally.imported_uploaded,
            tally.imported_failed,
            tally.consistent,
            len(tally.divergent),
            apply,
        )
        return tally.to_result()

    def _chunks(self) -> Iterator[list[NiarvilogRow]]:
        """Agrupa el stream del AS400 en lotes sin materializar la tabla."""
        chunk: list[NiarvilogRow] = []
        for row in self._as400.stream_rows_by_status():
            chunk.append(row)
            if len(chunk) >= self._chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk

    def _process(self, chunk: list[NiarvilogRow], tally: _Tally, *, apply: bool) -> None:
        """Clasifica un lote contra el tracking local y, si corresponde,
        lo escribe. UNA consulta a SQLite por lote, no una por fila."""
        local = self._sqlite.terminal_states_by_txns([r.trnnum for r in chunk])
        for row in chunk:
            status = local.get(row.trnnum)
            if status is None:
                self._import(row, tally, apply=apply)
            elif _AGREEMENT.get(row.stscod) == status:
                tally.consistent += 1
            else:
                tally.divergent.append(
                    PullItem(
                        row.trnnum,
                        f"AS400 STSCOD={row.stscod!r} vs local {status}: "
                        "el pull no pisa un estado terminal local",
                    )
                )

    def _import(self, row: NiarvilogRow, tally: _Tally, *, apply: bool) -> None:
        """REQ-001: CMCourier nunca vio este documento, así que manda el
        AS400. Va al batch sintético ``__as400_import__`` — lo que subió
        otro programa no es una exclusión nuestra y no puede ensuciar el
        censo de un batch real."""
        if row.stscod == "O":
            tally.imported_uploaded += 1
            if apply:
                self._sqlite.record_external_upload(
                    txn_num=row.trnnum,
                    file_name=row.imgarc,
                    shortname=row.ctecif,
                    cif=str(row.ctenum),
                    system_id=row.siscod,
                    cm_object_id=row.objidn,
                    id_rvi=row.docfrm,
                )
            return
        tally.imported_failed += 1
        if apply:
            self._sqlite.record_external_failure(
                txn_num=row.trnnum,
                file_name=row.imgarc,
                shortname=row.ctecif,
                cif=str(row.ctenum),
                system_id=row.siscod,
                error_message=row.eerrmsg,
                id_rvi=row.docfrm,
            )
