"""Tests unitarios para :class:`cmcourier.services.indexing.IndexingService`.

Ejercita el servicio de punta a punta contra un
:class:`TabularDataSource` real sobre un `fixture` CSV (Principio VI
de la constitución: sin `mock`s de `IDataSource`). Un
:class:`_CallCountingSource` chico envuelve el adaptador para
aseverar conteos de llamadas en los requerimientos de performance
del lookup `batched` (NFR-002).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cmcourier.adapters.sources import TabularDataSource
from cmcourier.domain.exceptions import (
    IndexingError,
    RVABREPDeletedError,
    RVABREPNotFoundError,
)
from cmcourier.domain.models import ReasonCode, RvabrepRowTrigger, TriggerRecord
from cmcourier.domain.ports import IDataSource
from cmcourier.services.indexing import IndexingColumnsConfig, IndexingService

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "services"
_SAMPLE_CSV = _FIXTURES / "rvabrep_index_sample.csv"


# ---------------------------------------------------------------------------
# Helpers de test
# ---------------------------------------------------------------------------


class _CallCountingSource(IDataSource):
    """Envuelve un `IDataSource` real y cuenta invocaciones al adaptador."""

    def __init__(self, inner: IDataSource) -> None:
        self.inner = inner
        self.get_by_fields_calls = 0
        self.get_by_fields_in_calls = 0
        self.stream_by_fields_in_calls = 0  # 148 REQ-001

    def get_all(self) -> Iterator[dict[str, Any]]:
        yield from self.inner.get_all()

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.get_by_fields_calls += 1
        return self.inner.get_by_fields(filters)

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return self.inner.query(sql, params)

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        return self.inner.query_stream(sql, params)

    def get_by_fields_in(
        self,
        field: str,
        values: list[Any],
        fixed_filters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        self.get_by_fields_in_calls += 1
        return self.inner.get_by_fields_in(field, values, fixed_filters)

    def stream_by_fields_in(  # 148 REQ-001
        self,
        field: str,
        values: list[Any],
        fixed_filters: Mapping[str, Any],
    ) -> Iterator[dict[str, Any]]:
        self.stream_by_fields_in_calls += 1
        yield from self.inner.stream_by_fields_in(field, values, fixed_filters)

    def count(self) -> int:
        return self.inner.count()

    def close(self) -> None:
        self.inner.close()


def _friendly_config() -> IndexingColumnsConfig:
    """Mapa de columnas que coincide con los nombres amigables en `rvabrep_index_sample.csv`."""
    return IndexingColumnsConfig(
        shortname_column="shortname",
        system_id_column="system_id",
        delete_code_column="delete_code",
        txn_num_column="txn_num",
        index2_column="index2",
        index3_column="index3",
        index4_column="index4",
        index5_column="index5",
        index6_column="index6",
        index7_column="index7",
        image_type_column="image_type",
        image_path_column="image_path",
        file_name_column="file_name",
        creation_date_column="creation_date",
        last_view_date_column="last_view_date",
        total_pages_column="total_pages",
    )


def _trigger(shortname: str, system_id: str = "1", cif: str | None = None) -> TriggerRecord:
    return TriggerRecord(shortname=shortname, cif=cif, system_id=system_id)


@pytest.fixture
def source() -> Iterator[TabularDataSource]:
    src = TabularDataSource(_SAMPLE_CSV)
    yield src
    src.close()


@pytest.fixture
def service(source: TabularDataSource) -> IndexingService:
    return IndexingService(source, _friendly_config())


# ---------------------------------------------------------------------------
# Grupo 1 — Construcción y defaults
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_construction_does_not_query(self, source: TabularDataSource) -> None:
        counting = _CallCountingSource(source)
        IndexingService(counting, _friendly_config())
        assert counting.get_by_fields_calls == 0
        assert counting.get_by_fields_in_calls == 0

    def test_default_column_config_matches_rvabrep_aba_codes(self) -> None:
        cfg = IndexingColumnsConfig()
        # Columnas de filtro / lookup (códigos ABA canónicos de RVABREP).
        assert cfg.shortname_column == "ABABCD"
        assert cfg.system_id_column == "ABAACD"
        assert cfg.txn_num_column == "ABAANB"
        assert cfg.delete_code_column == "ABACST"
        # Columna de `type-join`.
        assert cfg.index7_column == "ABAHCD"
        # Columnas de archivo.
        assert cfg.image_type_column == "ABABST"
        assert cfg.file_name_column == "ABAJCD"
        # Columnas de fecha / numéricas.
        assert cfg.creation_date_column == "ABAADT"
        assert cfg.last_view_date_column == "ABABDT"
        assert cfg.total_pages_column == "ABABUN"

    def test_config_is_frozen(self) -> None:
        import dataclasses

        cfg = IndexingColumnsConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.shortname_column = "OTHER"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Grupo 2 — Lookup de un solo `trigger`
# ---------------------------------------------------------------------------


class TestSingleTriggerLookup:
    def test_vanilla_multi_match(self, service: IndexingService) -> None:
        # JUANPEREZ01 sistema 1 tiene 3 filas activas.
        docs = service.find_documents(_trigger("JUANPEREZ01"))
        assert len(docs) == 3
        assert {d.txn_num for d in docs} == {"TXN0000001", "TXN0000002", "TXN0000003"}

    def test_not_found_raises(self, service: IndexingService) -> None:
        with pytest.raises(RVABREPNotFoundError) as ei:
            service.find_documents(_trigger("DOES_NOT_EXIST"))
        assert ei.value.context["shortname"] == "DOES_NOT_EXIST"
        assert ei.value.context["system_id"] == "1"

    def test_all_deleted_raises(self, service: IndexingService) -> None:
        # MARIAGOMEZ02 sistema 1 tiene 2 filas, ambas eliminadas.
        with pytest.raises(RVABREPDeletedError) as ei:
            service.find_documents(_trigger("MARIAGOMEZ02"))
        assert ei.value.context["deleted_count"] == 2

    def test_mixed_deleted_returns_active_only(self, service: IndexingService) -> None:
        # PEPELOPEZ03 sistema 1 tiene 1 activa + 2 eliminadas.
        docs = service.find_documents(_trigger("PEPELOPEZ03"))
        assert len(docs) == 1
        assert docs[0].txn_num == "TXN0000006"
        assert not docs[0].is_deleted

    def test_cif_does_not_filter(self, service: IndexingService) -> None:
        # JUANPEREZ01 con `cif=None` devuelve 3 docs.
        none_cif_docs = service.find_documents(_trigger("JUANPEREZ01", cif=None))
        # JUANPEREZ01 con `cif="123456"` devuelve los MISMOS 3 docs
        # (`CIF` ignorado).
        cif_docs = service.find_documents(_trigger("JUANPEREZ01", cif="123456"))
        # Incluso con un `CIF` que no aparece en ninguna fila.
        wrong_cif_docs = service.find_documents(_trigger("JUANPEREZ01", cif="999999"))
        assert len(none_cif_docs) == 3
        assert len(cif_docs) == 3
        assert len(wrong_cif_docs) == 3


# ---------------------------------------------------------------------------
# Grupo 3 — Manejo de `txn_num` duplicado
# ---------------------------------------------------------------------------


class TestDuplicateHandling:
    def test_duplicate_txn_num_warns_and_drops(
        self,
        service: IndexingService,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # DUPECLIENT tiene 2 filas, ambas con `txn_num='TXN0000009'`.
        with caplog.at_level(logging.WARNING, logger="cmcourier.services.indexing"):
            docs = service.find_documents(_trigger("DUPECLIENT"))
        assert len(docs) == 1
        assert docs[0].txn_num == "TXN0000009"
        # Se emitió WARNING, nombra `shortname` y el conteo de duplicados.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "DUPECLIENT" in r.getMessage() or r.__dict__.get("shortname") == "DUPECLIENT"
            for r in warnings
        )

    def test_duplicate_does_not_raise(self, service: IndexingService) -> None:
        # Aserción puramente funcional — no debe levantar excepción.
        docs = service.find_documents(_trigger("DUPECLIENT"))
        assert isinstance(docs, list)


# ---------------------------------------------------------------------------
# Grupo 5 — Coerción de filas
# ---------------------------------------------------------------------------


class TestRowCoercion:
    def test_cymmdd_round_trip(self, service: IndexingService) -> None:
        # La primera fila de JUANPEREZ01 tiene `creation_date='1251117'`
        # y `last_view='1251018'`.
        docs = service.find_documents(_trigger("JUANPEREZ01"))
        first = next(d for d in docs if d.txn_num == "TXN0000001")
        assert first.creation_date == datetime(2025, 11, 17)
        assert first.last_view_date == datetime(2025, 10, 18)

    def test_last_view_date_zero_becomes_none(self, service: IndexingService) -> None:
        # La fila PDF de JUANPEREZ01 tiene `last_view_date='0'`.
        docs = service.find_documents(_trigger("JUANPEREZ01"))
        pdf = next(d for d in docs if d.txn_num == "TXN0000002")
        assert pdf.last_view_date is None

    def test_last_view_date_empty_becomes_none(self, service: IndexingService) -> None:
        # EDGEDATES tiene `last_view_date=''` (celda vacía).
        docs = service.find_documents(_trigger("EDGEDATES"))
        assert docs[0].last_view_date is None

    def test_total_pages_is_int(self, service: IndexingService) -> None:
        # La primera fila de JUANPEREZ01 tiene `total_pages='540'`.
        docs = service.find_documents(_trigger("JUANPEREZ01"))
        first = next(d for d in docs if d.txn_num == "TXN0000001")
        assert isinstance(first.total_pages, int)
        assert first.total_pages == 540


# ---------------------------------------------------------------------------
# 051 — enriquecimiento de fila conocida: las filas con `delete-code` son
# un filtro de primera clase
# ---------------------------------------------------------------------------


def _row(shortname: str = "ACME01", system_id: str = "1", delete_code: str = "") -> dict[str, str]:
    """Una fila RVABREP mínima indexada por los nombres de columna amigables."""
    return {
        "shortname": shortname,
        "system_id": system_id,
        "delete_code": delete_code,
        "txn_num": "TXN999",
        "index2": "",
        "index3": "",
        "index4": "",
        "index5": "",
        "index6": "",
        "index7": "CC03",
        "image_type": "B",
        "image_path": "p",
        "file_name": "DAAA.001",
        "creation_date": "1251117",
        "last_view_date": "0",
        "total_pages": "1",
    }


def _row_trigger(row: dict[str, str]) -> RvabrepRowTrigger:
    return RvabrepRowTrigger(
        row=row, col_shortname="shortname", col_cif="index2", col_system_id="system_id"
    )


class TestEnrichKnownRow051:
    """Un `RvabrepRowTrigger` con `delete-code` levanta
    `RVABREPDeletedError` en vez del silencioso ``return []`` pre-051
    — para que el orquestador pueda contarlo como un outcome de
    primera clase "filtrado en S1"."""

    def test_active_row_yields_one_document(self, service: IndexingService) -> None:
        docs = service.enrich(_row_trigger(_row(delete_code="")))
        assert len(docs) == 1
        assert docs[0].txn_num == "TXN999"

    def test_delete_coded_row_raises_rvabrep_deleted(self, service: IndexingService) -> None:
        with pytest.raises(RVABREPDeletedError) as ei:
            service.enrich(_row_trigger(_row(shortname="GONE01", delete_code="D")))
        # El error carga la identidad de la fila para trazabilidad.
        assert ei.value.shortname == "GONE01"

    def test_delete_coded_row_is_not_a_silent_empty_list(self, service: IndexingService) -> None:
        # Resguardo de regresión: antes de 051 esto devolvía [] sin trazabilidad.
        with pytest.raises(RVABREPDeletedError):
            service.enrich(_row_trigger(_row(delete_code="X")))


# ---------------------------------------------------------------------------
# Grupo 5b — 148 REQ-004: el censo de S1, las filas borradas dejan de caerse
# ---------------------------------------------------------------------------


class TestEnrichCensus148:
    """``enrich`` devuelve sólo los vivos; ``enrich_census`` devuelve
    ADEMÁS los borrados, uno por uno y con su ``txn_num`` real.

    Pre-148 ``_classify`` los tiraba con un ``[r for r in rows if not
    delete_code]``: sin contador, sin log y sin fila. Y el orquestador,
    cuando TODAS venían borradas, escribía UNA sola fila con una clave
    sintética por identidad de trigger — N documentos borrados del mismo
    cliente colapsaban en 1.
    """

    def test_mixed_deleted_are_returned_as_excluded(self, service: IndexingService) -> None:
        # PEPELOPEZ03: 1 activa + 2 borradas.
        outcome = service.enrich_census(_trigger("PEPELOPEZ03"))
        assert [d.txn_num for d in outcome.documents] == ["TXN0000006"]
        assert {e.txn_num for e in outcome.excluded} == {"TXN0000007", "TXN0000008"}
        assert {e.reason_code for e in outcome.excluded} == {ReasonCode.DELETED_AT_SOURCE}

    def test_all_deleted_yields_one_excluded_per_row_not_one_collapsed(
        self, service: IndexingService
    ) -> None:
        """La colisión de la clave sintética: 2 filas borradas ⇒ 2 items."""
        outcome = service.enrich_census(_trigger("MARIAGOMEZ02"))
        assert outcome.documents == ()
        assert [e.txn_num for e in outcome.excluded] == ["TXN0000004", "TXN0000005"]

    def test_excluded_carries_id_rvi_for_grouping(self, service: IndexingService) -> None:
        outcome = service.enrich_census(_trigger("MARIAGOMEZ02"))
        assert {e.id_rvi for e in outcome.excluded} == {"FF17"}
        assert {e.shortname for e in outcome.excluded} == {"MARIAGOMEZ02"}

    def test_known_row_deleted_is_excluded_not_an_exception(self, service: IndexingService) -> None:
        outcome = service.enrich_census(_row_trigger(_row(shortname="GONE01", delete_code="D")))
        assert outcome.documents == ()
        assert len(outcome.excluded) == 1
        assert outcome.excluded[0].reason_code is ReasonCode.DELETED_AT_SOURCE
        assert outcome.excluded[0].txn_num == "TXN999"

    def test_not_found_still_raises(self, service: IndexingService) -> None:
        with pytest.raises(RVABREPNotFoundError):
            service.enrich_census(_trigger("DOES_NOT_EXIST"))

    def test_enrich_keeps_its_pre_148_contract(self, service: IndexingService) -> None:
        """``doctor`` y cualquier caller viejo siguen viendo lo de siempre."""
        assert len(service.enrich(_trigger("PEPELOPEZ03"))) == 1
        with pytest.raises(RVABREPDeletedError) as ei:
            service.enrich(_trigger("MARIAGOMEZ02"))
        assert ei.value.deleted_count == 2


# ---------------------------------------------------------------------------
# Grupo 6 — Envoltura de errores
# ---------------------------------------------------------------------------


class _BrokenSource(IDataSource):
    """Un `IDataSource` cuyos métodos levantan un `RuntimeError` sintético."""

    def get_all(self) -> Iterator[dict[str, Any]]:
        raise RuntimeError("synthetic adapter failure")
        yield  # pragma: no cover  # inalcanzable, hace feliz al type checker

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        raise RuntimeError("synthetic adapter failure")

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("synthetic adapter failure")

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        raise RuntimeError("synthetic adapter failure")
        yield  # pragma: no cover

    def get_by_fields_in(
        self,
        field: str,
        values: list[Any],
        fixed_filters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        raise RuntimeError("synthetic adapter failure")

    def stream_by_fields_in(  # 148 REQ-001
        self,
        field: str,
        values: list[Any],
        fixed_filters: Mapping[str, Any],
    ) -> Iterator[dict[str, Any]]:
        raise RuntimeError("synthetic adapter failure")
        yield  # pragma: no cover

    def count(self) -> int:
        return 0

    def close(self) -> None:
        return None


class TestErrorWrapping:
    def test_adapter_exception_becomes_indexing_error(self) -> None:
        service = IndexingService(_BrokenSource(), _friendly_config())
        with pytest.raises(IndexingError) as ei:
            service.find_documents(_trigger("JUANPEREZ01"))
        assert isinstance(ei.value.__cause__, RuntimeError)
        assert ei.value.context["shortname"] == "JUANPEREZ01"
        assert ei.value.context["system_id"] == "1"


# ---------------------------------------------------------------------------
# Grupo 7 — Disciplina de logging (Constitución VIII)
# ---------------------------------------------------------------------------


class TestLoggingDiscipline:
    def test_duplicate_warning_does_not_log_index_values(
        self,
        service: IndexingService,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="cmcourier.services.indexing"):
            service.find_documents(_trigger("DUPECLIENT"))
        # El valor de `CIF` de DUPECLIENT en el `fixture` es '456789'.
        # NO debe aparecer en ningún mensaje de log.
        for record in caplog.records:
            assert "456789" not in record.getMessage()
            assert "456789" not in str(record.__dict__.get("extra", ""))


# ---------------------------------------------------------------------------
# Grupo 8 — 144: lookup batcheado por txn_num (sync recover)
# ---------------------------------------------------------------------------


class TestFindDocumentsByTxns144:
    def test_returns_dict_keyed_by_txn_in_one_in_query(self, source: TabularDataSource) -> None:
        counting = _CallCountingSource(source)
        service = IndexingService(counting, _friendly_config())

        docs = service.find_documents_by_txns(["TXN0000001", "TXN0000013", "TXN0000006"])

        assert set(docs) == {"TXN0000001", "TXN0000013", "TXN0000006"}
        assert docs["TXN0000001"].index7 == "FF17"
        assert docs["TXN0000013"].image_type == "O"
        assert counting.get_by_fields_in_calls == 1
        assert counting.get_by_fields_calls == 0

    def test_missing_txns_are_simply_absent(self, service: IndexingService) -> None:
        docs = service.find_documents_by_txns(["TXN0000001", "NOPE-1", "NOPE-2"])
        assert list(docs) == ["TXN0000001"]

    def test_empty_input_does_not_query(self, source: TabularDataSource) -> None:
        counting = _CallCountingSource(source)
        service = IndexingService(counting, _friendly_config())
        assert service.find_documents_by_txns([]) == {}
        assert counting.get_by_fields_in_calls == 0

    def test_duplicate_txn_rows_keep_first_like_single_lookup(
        self, service: IndexingService
    ) -> None:
        # DUPECLIENT tiene 2 filas con TXN0000009; ``find_document_by_txn``
        # devuelve la primera — el batch hace lo mismo.
        single = service.find_document_by_txn("TXN0000009")
        batch = service.find_documents_by_txns(["TXN0000009"])
        assert single is not None
        assert batch["TXN0000009"] == single

    def test_deleted_rows_are_returned_like_single_lookup(self, service: IndexingService) -> None:
        # El recover re-deriva campos de un doc YA subido: la marca de
        # borrado no lo excluye (mismo contrato que ``find_document_by_txn``).
        docs = service.find_documents_by_txns(["TXN0000004"])
        assert docs["TXN0000004"].delete_code == "D"
