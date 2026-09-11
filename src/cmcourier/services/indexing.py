"""Stage S1: :class:`IndexingService`.

Dado un :class:`TriggerRecord`, encuentra cada
:class:`RVABREPDocument` no borrado que matchee por
``(shortname, system_id)``. El CIF NO se usa como filtro aquí
intencionalmente: el self-healing de CIF es responsabilidad del
Stage S3 (Metadata).

API pública: :meth:`enrich_census` (dispatch polimórfico de S1) y
:meth:`enrich` / :meth:`find_documents`, que son su vista "sólo los
vivos" con semántica de errores tipados
(:class:`RVABREPNotFoundError` / :class:`RVABREPDeletedError`).
148: el `pipeline` usa ``enrich_census`` porque las filas borradas
DEJARON de ser un descarte — cada una tiene que llegar al censo con su
``txn_num`` real. 121: el lookup batcheado
``find_documents_batch`` se eliminó — era código muerto cuya semántica
(sin distinción not-found vs all-deleted) no matcheaba el contrato de
`S1_FILTERED` del orchestrator. 144: :meth:`find_documents_by_txns` es
un batch DISTINTO — keyed por txn, para el `sync recover`; sin esa
ambigüedad.

Principio I de la Constitución: este módulo importa solo la
biblioteca estándar y :mod:`cmcourier.domain`. Principio VIII:
los logs identifican nombres de columnas y shortnames de trigger,
pero nunca los valores de CIF ni de campos indexados de texto
libre.
"""

from __future__ import annotations

__all__ = ["EnrichOutcome", "IndexingColumnsConfig", "IndexingService"]

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cmcourier.domain.exceptions import (
    IndexingError,
    RVABREPDeletedError,
    RVABREPNotFoundError,
)
from cmcourier.domain.models import (
    ClientTrigger,
    ExcludedTrigger,
    LocalScanTrigger,
    ReasonCode,
    RVABREPDocument,
    RvabrepRowTrigger,
    Trigger,
    TriggerRecord,
    parse_cymmdd,
)
from cmcourier.domain.ports import IDataSource

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EnrichOutcome:
    """148 REQ-004: el resultado COMPLETO de S1 para un trigger.

    ``documents`` son los que siguen viaje; ``excluded`` son las filas
    que el origen dice que no —hoy, las que traen código de borrado—,
    cada una con su ``txn_num`` REAL y su ``reason_code``.

    Existe porque pre-148 esas filas se caían en dos lugares distintos y
    los dos eran mudos: ``_classify`` las tiraba con un ``if not
    delete_code`` (sin contador, sin log, sin fila) y, cuando venían
    TODAS borradas, el orquestador escribía UNA fila con clave sintética
    por identidad de trigger — N documentos del mismo cliente colapsaban
    en uno.

    Capas: el servicio CLASIFICA y no persiste; el orquestador —el único
    dueño del ``batch_id`` y del tracking store— escribe la fila.
    """

    documents: tuple[RVABREPDocument, ...]
    excluded: tuple[ExcludedTrigger, ...]


# ---------------------------------------------------------------------------
# Configuración de columnas (nombres físicos de RVABREP por defecto)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndexingColumnsConfig:
    """Mapa de nombres de columna entre las filas del adapter y
    :class:`RVABREPDocument`.

    Los defaults coinciden con los nombres físicos de columnas de
    RVABREP en AS400. Tests y deploys no-AS400 overridean columnas
    individuales.
    """

    shortname_column: str = "ABABCD"  # index1 en RVABREPDocument, ShortName del trigger
    system_id_column: str = "ABAACD"  # system_code en RVABREPDocument, SystemID del trigger
    delete_code_column: str = "ABACST"
    txn_num_column: str = "ABAANB"

    index2_column: str = "ABACCD"
    index3_column: str = "ABADCD"
    index4_column: str = "ABAECD"
    index5_column: str = "ABAFCD"
    index6_column: str = "ABAGCD"
    index7_column: str = "ABAHCD"  # = id_rvi

    image_type_column: str = "ABABST"
    image_path_column: str = "ABAICD"
    file_name_column: str = "ABAJCD"

    creation_date_column: str = "ABAADT"
    last_view_date_column: str = "ABABDT"
    total_pages_column: str = "ABABUN"


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------


class IndexingService:
    """Motor del stage S1: TriggerRecord → list[RVABREPDocument]."""

    def __init__(
        self,
        source: IDataSource,
        config: IndexingColumnsConfig,
    ) -> None:
        self._source = source
        self._cfg = config

    # ----------------------------------------------------------- API pública

    def enrich(self, trigger: Trigger) -> list[RVABREPDocument]:
        """046: enriquecimiento polimórfico de S1.

        `Dispatch` según el subtipo de trigger:

        * ``ClientTrigger`` → camino existente de ``find_documents``
          (lookup en RVABREP por (shortname, system_id), expandiendo
          a N docs).
        * ``RvabrepRowTrigger`` → envuelve la fila ya cargada en un
          único :class:`RVABREPDocument`. **Cero queries**.
        * ``LocalScanTrigger`` → mismo caso que el anterior: la fila
          de RVABREP matcheada viene adjunta desde el momento del
          acquire en S0.

        Lanza ``RVABREPNotFoundError`` cuando no hay filas que matcheen
        y ``RVABREPDeletedError`` cuando toda fila matcheada está
        marcada como borrada.

        148: es un envoltorio delgado sobre :meth:`enrich_census` que
        conserva el contrato pre-148 para los callers que sólo quieren
        los documentos vivos (el ``doctor``, el dry-run). El `pipeline`
        usa ``enrich_census``: necesita también los que NO siguen.
        """
        outcome = self.enrich_census(trigger)
        if not outcome.documents and outcome.excluded:
            first = outcome.excluded[0]
            raise RVABREPDeletedError(
                shortname=first.shortname or "",
                system_id=first.system_id or "",
                deleted_count=len(outcome.excluded),
            )
        return list(outcome.documents)

    def enrich_census(self, trigger: Trigger) -> EnrichOutcome:
        """148 REQ-004: igual que :meth:`enrich`, pero sin tirar nada.

        Mismo `dispatch` polimórfico; lo que cambia es que las filas con
        código de borrado salen en :attr:`EnrichOutcome.excluded` en vez
        de desaparecer (camino ``ClientTrigger``) o de colapsar en una
        sola ``RVABREPDeletedError`` sin ``txn_num`` (camino fila
        conocida). ``RVABREPNotFoundError`` e ``IndexingError`` se siguen
        levantando: ahí no hay ninguna fila que clasificar.
        """
        if isinstance(trigger, ClientTrigger):
            rows = self._query_for_trigger(trigger)
            if not rows:
                raise RVABREPNotFoundError(
                    shortname=trigger.shortname,
                    system_id=trigger.system_id,
                )
            return self._classify_census(rows, trigger)
        if isinstance(trigger, (RvabrepRowTrigger, LocalScanTrigger)):
            return self._classify_census([dict(trigger.row)], None)
        raise TypeError(
            f"unknown Trigger subtype: {type(trigger).__name__!r} — "
            f"add a dispatch branch in IndexingService.enrich"
        )

    def txn_num_of(self, trigger: Trigger) -> str:
        """148 REQ-004: el ``txn_num`` que un trigger ya conoce, o ``""``.

        Lo necesita el registro de un CRASH en streaming: la excepción
        puede llegar antes de que S1 haya derivado ningún documento, y
        una fila del censo sin clave no se puede escribir. Un
        ``ClientTrigger`` no conoce ninguno (se expande a N documentos
        recién en S1), así que devuelve ``""`` y el caller cae a una
        clave sintética.
        """
        if isinstance(trigger, ExcludedTrigger):
            return trigger.txn_num
        if isinstance(trigger, (RvabrepRowTrigger, LocalScanTrigger)):
            return _str(trigger.row.get(self._cfg.txn_num_column))
        return ""

    def find_document_by_txn(self, txn_num: str) -> RVABREPDocument | None:
        """099: busca la fila RVABREP de un ``txn_num`` y la convierte a
        :class:`RVABREPDocument`. Devuelve ``None`` si no existe.

        Lo usa la recuperación AS400 (`cmcourier sync recover`) para
        re-derivar ``index7`` / ``image_type`` de un doc ya subido cuya
        fila NIARVILOG se perdió."""
        rows = self._source.get_by_fields({self._cfg.txn_num_column: txn_num})
        if not rows:
            return None
        return self._row_to_document(dict(rows[0]))

    def find_documents_by_txns(self, txns: Iterable[str]) -> dict[str, RVABREPDocument]:
        """144: versión batcheada de :meth:`find_document_by_txn` — UNA
        query ``IN`` chunkeada (113) por ⌈N/1000⌉ viajes, en lugar de un
        SELECT sobre ``(query) AS T`` por doc.

        Devuelve ``{txn_num: documento}``; un txn sin fila simplemente
        no está en el dict (el caller lo trata como
        ``rvabrep_row_not_found``). Mismo contrato que el lookup
        unitario: la primera fila gana ante duplicados y la marca de
        borrado NO excluye — el recover re-deriva campos de docs YA
        subidos. A diferencia del ``find_documents_batch`` eliminado en
        121, acá la clave es el txn: no hay ambigüedad not-found vs
        all-deleted que resolver."""
        values = list(txns)
        if not values:
            return {}
        rows = self._source.get_by_fields_in(self._cfg.txn_num_column, values, {})
        docs: dict[str, RVABREPDocument] = {}
        for row in rows:
            txn = _str(row.get(self._cfg.txn_num_column))
            if txn not in docs:
                docs[txn] = self._row_to_document(dict(row))
        return docs

    def find_documents(self, trigger: TriggerRecord) -> list[RVABREPDocument]:
        """Busca cada fila de RVABREP no borrada que matchee el trigger."""
        outcome = self.enrich_census(trigger)
        if not outcome.documents:
            raise RVABREPDeletedError(
                shortname=trigger.shortname,
                system_id=trigger.system_id,
                deleted_count=len(outcome.excluded),
            )
        return list(outcome.documents)

    # ----------------------------------------------------------- internos

    def _query_for_trigger(self, trigger: TriggerRecord) -> list[dict[str, Any]]:
        try:
            return self._source.get_by_fields(
                {
                    self._cfg.shortname_column: trigger.shortname,
                    self._cfg.system_id_column: trigger.system_id,
                }
            )
        except Exception as exc:
            raise IndexingError(
                "indexing query failed",
                shortname=trigger.shortname,
                system_id=trigger.system_id,
            ) from exc

    def _classify_census(
        self,
        rows: list[dict[str, Any]],
        trigger: TriggerRecord | None,
    ) -> EnrichOutcome:
        """148 REQ-004: parte las filas en "siguen viaje" y "el origen dice que no".

        Pre-148 la primera línea era ``active = [r for r in rows if not
        delete_code]``: las borradas se caían sin contador, sin log y sin
        fila. Ahora cada una sale con su ``txn_num`` real, que es lo que
        le permite al orquestador escribir N filas y no una sola con
        clave sintética.
        """
        documents: list[RVABREPDocument] = []
        excluded: list[ExcludedTrigger] = []
        seen: set[str] = set()
        duplicates = 0
        for row in rows:
            if _str(row.get(self._cfg.delete_code_column)):
                excluded.append(self._excluded_from_row(row, ReasonCode.DELETED_AT_SOURCE))
                continue
            txn = _str(row.get(self._cfg.txn_num_column))
            if txn in seen:
                duplicates += 1
                continue
            seen.add(txn)
            documents.append(self._row_to_document(dict(row)))
        if duplicates:
            _log.warning(
                "indexing: dropped duplicate txn_num rows",
                extra={
                    "shortname": trigger.shortname if trigger is not None else "",
                    "duplicate_count": duplicates,
                },
            )
        return EnrichOutcome(documents=tuple(documents), excluded=tuple(excluded))

    def _excluded_from_row(self, row: Mapping[str, Any], reason: ReasonCode) -> ExcludedTrigger:
        """Proyecta una fila rechazada a lo que ``migration_log`` necesita."""
        cfg = self._cfg
        return ExcludedTrigger(
            reason_code=reason,
            txn_num=_str(row.get(cfg.txn_num_column)),
            id_rvi=_str(row.get(cfg.index7_column)),
            file_name=_str(row.get(cfg.file_name_column)),
            shortname=_str(row.get(cfg.shortname_column)) or None,
            cif=_str(row.get(cfg.index2_column)) or None,
            system_id=_str(row.get(cfg.system_id_column)) or None,
        )

    def _row_to_document(self, row: dict[str, Any]) -> RVABREPDocument:
        cfg = self._cfg
        return RVABREPDocument(
            system_code=_str(row.get(cfg.system_id_column)),
            txn_num=_str(row.get(cfg.txn_num_column)),
            index1=_str(row.get(cfg.shortname_column)),
            index2=_str(row.get(cfg.index2_column)),
            index3=_str(row.get(cfg.index3_column)),
            index4=_str(row.get(cfg.index4_column)),
            index5=_str(row.get(cfg.index5_column)),
            index6=_str(row.get(cfg.index6_column)),
            index7=_str(row.get(cfg.index7_column)),
            image_type=_str(row.get(cfg.image_type_column)),
            image_path=_normalize_image_path(_str(row.get(cfg.image_path_column))),
            file_name=_str(row.get(cfg.file_name_column)),
            creation_date=parse_cymmdd(_str(row.get(cfg.creation_date_column))),
            last_view_date=_parse_last_view_date(row.get(cfg.last_view_date_column)),
            total_pages=_to_int(row.get(cfg.total_pages_column)),
            delete_code=_str(row.get(cfg.delete_code_column)),
        )


# ---------------------------------------------------------------------------
# Helpers de coerción
# ---------------------------------------------------------------------------


def _str(value: Any) -> str:
    """Coerciona *value* a cadena tratando ``None`` como cadena vacía."""
    if value is None:
        return ""
    return str(value)


def _normalize_image_path(value: str) -> str:
    """075: strippea leading separators del ``ABAICD`` antes de que
    pase al dominio.

    El RVI escribe el ``image_path`` con un leading ``/`` (paths
    "absolutos" desde la raíz del file share, ej.
    ``/RVI9/020526/0004``). Pre-075 ese path llegaba al assembler
    tal cual, y al concatenarlo con ``assembly.source_root`` vía
    ``Path / Path``, pathlib descartaba silenciosamente
    ``source_root`` (``Path("a") / "/b"`` devuelve ``Path("/b")``).

    Esta función aplana backslashes a forward slashes, strippea
    whitespace, y después strippea separadores al inicio (en ese
    orden, así inputs como ``"  /RVI9  "`` quedan ``"RVI9"`` y no
    ``"/RVI9"``). Devuelve ``str`` para mantener el tipo del campo
    ``RVABREPDocument.image_path``.
    """
    return value.replace("\\", "/").strip().lstrip("/")


def _to_int(value: Any) -> int:
    """Coerciona *value* a ``int``; ``None`` o cadena vacía resultan en ``0``."""
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    return int(text)


def _parse_last_view_date(value: Any) -> Any:
    """Parsea una celda ``last_view_date`` en formato CYYMMDD,
    mapeando ``'0'`` o ``''`` a ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "0":
        return None
    return parse_cymmdd(text)
