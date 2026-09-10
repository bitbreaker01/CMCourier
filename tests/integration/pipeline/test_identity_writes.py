"""147 REQ-004: un solo origen para las tres escrituras.

Esta es LA divergencia que la spec 147 existe para cerrar. Pre-147:

* el self-healing de CIF sólo reconstruía el trigger cuando era un
  ``ClientTrigger`` (``metadata.py:281``), y el camino de PRODUCCIÓN es un
  ``RvabrepRowTrigger`` con fila inmutable;
* ``migration_log`` escribía el CIF curado (``staged.py:1123``) mientras
  ``try_claim`` proyectaba ``audit_row()`` crudo (``staged.py:1383`` →
  ``as400_niarvilog.py:578``).

Resultado: **las dos tablas discrepaban sobre el mismo documento**, y del
lado de AS400 un CIF no numérico se escribía como cliente ``0``.

El test recorre el camino de producción de punta a punta: fila RVABREP →
S2 (identidad + mapeo) → ``MigrationRecord`` → INSERT de NIARVILOG, y
compara lo que llega a cada tabla.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.tracking import as400_niarvilog as niarvilog_module
from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore
from cmcourier.config.schema import As400ConnectionConfig
from cmcourier.domain.models import (
    CMMapping,
    MigrationRecord,
    RVABREPDocument,
    RvabrepRowTrigger,
    StageStatus,
)
from cmcourier.domain.ports import IDataSource
from cmcourier.orchestrators.staged import StagedPipeline, _StageItem
from cmcourier.services.identity import IdentityConfig, IdentityResolver, IdentitySlotConfig
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSource(IDataSource):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def get_all(self) -> Iterator[dict[str, Any]]:
        yield from self.rows

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [row for row in self.rows if all(row.get(k) == v for k, v in filters.items())]

    def get_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get(field) in values]

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def count(self) -> int:
        return len(self.rows)

    def close(self) -> None:
        return None


class _FakeCursor:
    """Registra cada tupla (sql, params). Mismo patrón que
    ``tests/integration/adapters/test_as400_niarvilog.py`` — el Principio VI
    permite fakear los bindings del driver, nunca el servidor."""

    def __init__(self, rowcounts: list[int]) -> None:
        self.executions: list[tuple[str, list[Any]]] = []
        self._rowcounts = rowcounts
        self.rowcount = -1
        self.description: list[tuple[str, ...]] = []

    def execute(self, sql: str, params: list[Any] | None = None) -> _FakeCursor:
        self.executions.append((sql, list(params or [])))
        self.rowcount = self._rowcounts.pop(0) if self._rowcounts else -1
        return self

    def fetchall(self) -> list[list[Any]]:
        return []

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakePyodbc:
    class Error(Exception):
        pass

    class IntegrityError(Error):
        pass

    class OperationalError(Error):
        pass

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self, cs: str) -> _FakeConn:  # noqa: ARG002
        return self._conn


# ---------------------------------------------------------------------------
# Escenario
# ---------------------------------------------------------------------------

#: La fila RVABREP del camino de producción: shortname viejo, CIF crudo que
#: la corrida original nunca curó (columnas físicas AS400).
_RAW_ROW = {"ABABCD": "VIEJOSA01", "ABACCD": "999999", "ABAACD": "1"}
#: Lo que la cadena resuelve para ese cliente.
_RESOLVED_SHORTNAME = "ACMESA01"
_RESOLVED_CIF = "123456789"


def _document() -> RVABREPDocument:
    return RVABREPDocument(
        system_code="1",
        txn_num="0000001",
        index1="HIJO-1",
        index2="",
        index3="",
        index4="",
        index5="",
        index6="",
        index7="CC03",
        image_type="B",
        image_path="x",
        file_name="DAAAH9X4.001",
        creation_date=datetime(2025, 11, 17, tzinfo=UTC),
        last_view_date=None,
        total_pages=1,
        delete_code="",
    )


def _mapping() -> CMMapping:
    return CMMapping(
        clase_id="01.02.04.01.01",
        id_rvi="CC03",
        id_corto="CN01",
        clase_name="Clase",
        required_metadata_fields=(),
        cmis_type="MyType",
    )


def _metadata_service() -> MetadataService:
    afiliados = _FakeSource([{"AFIHIJ": "HIJO-1", "AFIPAD": "PADRE-7"}])
    clientes = _FakeSource(
        [{"CUSAFI": "PADRE-7", "CUSSHN": _RESOLVED_SHORTNAME, "CUSCIF": _RESOLVED_CIF}]
    )
    config = MetadataConfig(
        field_aliases={},
        field_sources={
            "BAC_Afiliado_Hijo": FieldSourceConfig(
                sources=(SourceConfig(source_type="rvabrep", lookup_value_column="index1"),),
            ),
            "BAC_Afiliado_Padre": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="as400:afiliados",
                        lookup_value_column="AFIPAD",
                        lookup_key_column="AFIHIJ",
                        lookup_value_source="field.BAC_Afiliado_Hijo",
                    ),
                ),
            ),
            "BAC_Shortname": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="as400:clientes",
                        lookup_value_column="CUSSHN",
                        lookup_key_column="CUSAFI",
                        lookup_value_source="field.BAC_Afiliado_Padre",
                    ),
                ),
            ),
            "BAC_CIF": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="as400:clientes",
                        lookup_value_column="CUSCIF",
                        lookup_key_column="CUSSHN",
                        lookup_value_source="field.BAC_Shortname",
                    ),
                ),
            ),
        },
        prefetch_enabled=False,
    )
    return MetadataService(config, {"afiliados": afiliados, "clientes": clientes})


def _record_for_production_path() -> MigrationRecord:
    """Corre S2 sobre el trigger de producción y devuelve el record."""
    metadata_service = _metadata_service()
    mapping_service = MagicMock()
    mapping_service.get_mapping.return_value = _mapping()
    tracking = MagicMock()
    tracking.is_stage_done.return_value = True
    pipeline = StagedPipeline(
        trigger_strategy=MagicMock(),
        indexing_service=MagicMock(),
        mapping_service=mapping_service,
        metadata_service=metadata_service,
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=tracking,
        workers=1,
        identity_resolver=IdentityResolver(
            IdentityConfig(
                shortname=IdentitySlotConfig(field="BAC_Shortname"),
                cif=IdentitySlotConfig(field="BAC_CIF", max_digits=9),
            ),
            metadata_service,
        ),
    )
    item = _StageItem(trigger=RvabrepRowTrigger(row=_RAW_ROW), document=_document())
    survivor, _ = pipeline._s2_one(item, "B1", pipeline._metrics)
    assert survivor is not None
    return pipeline._build_record(survivor, "B1", StageStatus.S5_PENDING)


def _claim_params(monkeypatch: pytest.MonkeyPatch, record: MigrationRecord) -> list[Any]:
    """El INSERT de NIARVILOG que ``try_claim`` dispara para ese record."""
    cursor = _FakeCursor([0, 1])  # el UPDATE no matchea → INSERT
    monkeypatch.setattr(niarvilog_module, "pyodbc", _FakePyodbc(_FakeConn(cursor)))
    store = As400NiarvilogStore(
        connection=As400ConnectionConfig(host="10.0.0.1", database="RVILIB"),
        username="tester",
        password="secret",
        library="RVILIB",
        table="RVIMGLOG",
    )
    store.try_claim(
        record=record,
        document=_document(),
        mapping=_mapping(),
        trigger=RvabrepRowTrigger(row=_RAW_ROW),
    )
    return cursor.executions[1][1]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUnSoloOrigen:
    def test_el_tracking_guarda_la_identidad_resuelta(self) -> None:
        record = _record_for_production_path()
        assert record.trigger_shortname == _RESOLVED_SHORTNAME
        assert record.trigger_cif == _RESOLVED_CIF

    def test_ctecif_y_ctenum_reciben_exactamente_lo_mismo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = _record_for_production_path()
        params = _claim_params(monkeypatch, record)
        # Éste es el assert que pre-147 fallaba: NIARVILOG recibía
        # "VIEJOSA01" / 999999 mientras migration_log guardaba el resuelto.
        assert record.trigger_shortname in params
        assert int(record.trigger_cif) in params
        assert "VIEJOSA01" not in params
        assert 999999 not in params
