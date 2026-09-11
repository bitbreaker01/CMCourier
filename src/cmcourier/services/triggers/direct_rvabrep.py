"""Estrategia de trigger por RVABREP directo. Modo ``direct_rvabrep``."""

from __future__ import annotations

__all__ = [
    "DirectRvabrepTriggerStrategy",
    "RvabrepColumnsConfig",
    "RvabrepFilters",
]

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from cmcourier.domain.models import ExcludedTrigger, ReasonCode, RvabrepRowTrigger, Trigger
from cmcourier.domain.ports import IDataSource, S0Strategy

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RvabrepColumnsConfig:
    """Overrides de nombres físicos de columnas de RVABREP."""

    col_shortname: str = "ABABCD"  # index1
    col_cif: str = "ABACCD"  # index2
    col_system_id: str = "ABAACD"  # system_code
    col_id_rvi: str = "ABAHCD"  # index7 (tipo de documento)
    file_name_column: str = "ABAJCD"  # ABAJCD (file_name)
    # 148 REQ-004: la clave real de la fila. Una exclusión detectada en S0
    # tiene que llevarla — la clave sintética por identidad de trigger
    # colisiona contra el índice único ``(rvabrep_txn_num, batch_id)`` y
    # colapsa N documentos en 1 fila.
    col_txn_num: str = "ABAANB"


@dataclass(frozen=True, slots=True)
class RvabrepFilters:
    """Filtros para el escaneo de RVABREP. Tupla vacía = sin filtro.

    148 REQ-001: los dos filtros NO son simétricos.

    * ``systems`` es un filtro de VERDAD: va al ``WHERE ... IN`` del SQL,
      así que una fila de otro sistema nunca vuelve del origen.
    * ``document_types`` **ya no toca el SQL**. Cambió de significado: de
      *"traeme sólo estos"* pasó a *"de todo lo que traigas, migrá estos
      y contame el resto"*. Una fila cuyo código no está en la lista
      vuelve igual del AS400 y se emite como
      :class:`~cmcourier.domain.models.ExcludedTrigger` con
      ``EXCLUDED_BY_FILTER`` — si el código fuera al ``IN``, el documento
      excluido nunca volvería y el censo no lo podría contar, que es
      exactamente el defecto que la spec 148 arregla.
    """

    systems: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()


def _is_blank(v: object) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _text(v: object) -> str:
    return "" if v is None else str(v).strip()


class DirectRvabrepTriggerStrategy(S0Strategy):
    """Descubre triggers escaneando RVABREP en sí, opcionalmente
    filtrado.

    046: yieldea un :class:`RvabrepRowTrigger` por cada fila
    matcheada y no borrada. Antes de 046 la estrategia deduplicaba
    por ``(shortname, system_id)`` y yieldeaba un ``TriggerRecord``;
    eso forzaba a S1 a re-consultar RVABREP y re-expandir a N docs
    por cliente, trabajo desperdiciado y semántica equivocada para
    "procesar ESTA fila, no todo el cliente". Ahora el enriquecimiento
    en S1 queda trivial porque la fila ya se conoce.

    148 REQ-001: la query lleva **únicamente** ``filters.systems``, y con
    ``systems`` seteado el escaneo va por ``stream_by_fields_in`` — con el
    censo siempre activo un sistema entero es la carga NORMAL y no puede
    materializarse en una lista de Python. ``filters.document_types`` se
    aplica en memoria sobre lo que volvió, y lo que no matchea NO se
    descarta: se emite como
    :class:`~cmcourier.domain.models.ExcludedTrigger`.

    148 REQ-004 — capas: esta estrategia **clasifica** y no persiste
    nada. No conoce el ``batch_id`` ni el tracking store, y no tiene por
    qué: emite el ítem clasificado y S1 —el único lugar que es dueño de
    ambos— escribe la fila de ``migration_log``.
    """

    def __init__(
        self,
        rvabrep_source: IDataSource,
        filters: RvabrepFilters | None = None,
        columns: RvabrepColumnsConfig | None = None,
    ) -> None:
        self._source = rvabrep_source
        self._filters = filters or RvabrepFilters()
        self._columns = columns or RvabrepColumnsConfig()

    def acquire(self, source_descriptor: str = "") -> Iterator[Trigger]:
        """Yieldea UN trigger por cada fila que el escaneo trajo.

        148 REQ-004: ninguna fila se cae en silencio. Una fila sin
        shortname o sin system_id sale como ``SOURCE_ROW_INCOMPLETE`` y
        una cuyo código no está en ``filters.document_types`` sale como
        ``EXCLUDED_BY_FILTER`` — ambas como
        :class:`~cmcourier.domain.models.ExcludedTrigger`, que S1
        registra y que nunca llega a S2. Pre-148 la primera era un
        contador y un INFO agregado al final, y la segunda un ``continue``
        pelado.
        """
        del source_descriptor
        allowed = {c.strip() for c in self._filters.document_types if c.strip()}
        for row in self._iter_filtered_rows():
            if _is_blank(row.get(self._columns.col_shortname)) or _is_blank(
                row.get(self._columns.col_system_id)
            ):
                yield self._excluded(row, ReasonCode.SOURCE_ROW_INCOMPLETE)
                continue
            if allowed and _text(row.get(self._columns.col_id_rvi)) not in allowed:
                yield self._excluded(row, ReasonCode.EXCLUDED_BY_FILTER)
                continue
            yield RvabrepRowTrigger(
                row=row,
                col_shortname=self._columns.col_shortname,
                col_cif=self._columns.col_cif,
                col_system_id=self._columns.col_system_id,
            )

    def _excluded(self, row: Mapping[str, Any], reason: ReasonCode) -> ExcludedTrigger:
        """Proyecta una fila rechazada al ítem clasificado que S1 registra."""
        cols = self._columns
        return ExcludedTrigger(
            reason_code=reason,
            txn_num=_text(row.get(cols.col_txn_num)),
            id_rvi=_text(row.get(cols.col_id_rvi)),
            file_name=_text(row.get(cols.file_name_column)),
            shortname=_text(row.get(cols.col_shortname)) or None,
            cif=_text(row.get(cols.col_cif)) or None,
            system_id=_text(row.get(cols.col_system_id)) or None,
        )

    def _iter_filtered_rows(self) -> Iterator[dict[str, Any]]:
        """148 REQ-001: el ``WHERE`` lleva sólo los sistemas, y siempre en stream.

        ``filters.document_types`` NO entra al SQL: un código excluido
        tiene que volver del origen para que el censo lo pueda contar.
        """
        if not self._filters.systems:
            yield from self._source.get_all()
            return
        yield from self._source.stream_by_fields_in(
            field=self._columns.col_system_id,
            values=list(self._filters.systems),
            fixed_filters={},
        )
