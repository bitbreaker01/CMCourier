"""Tests de :class:`MssqlDataSource` (130) con el ``pyodbc`` falso de
``test_as400.py``, parcheado en ``cmcourier.adapters.sources.mssql``.

El contrato ``IDataSource`` es el mismo que AS400 (base compartida
``OdbcDataSource``); acá se verifica lo que difiere: la connection string
T-SQL, el ``LABEL`` en errores y log de red, y que ``table`` admite esquema
(``dbo.clientes``).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from cmcourier.adapters.sources.as400 import As400DataSource
from cmcourier.adapters.sources.mssql import MssqlDataSource
from cmcourier.adapters.sources.odbc_base import OdbcDataSource
from cmcourier.domain.exceptions import ConfigurationError, IndexingError
from tests.integration.adapters.test_as400 import (
    _FakeAs400Connection,
    _FakeAs400Cursor,
    _FakePyodbcModule,
)

pytestmark = pytest.mark.integration


def _patch_mssql_pyodbc(
    monkeypatch: pytest.MonkeyPatch, connection: _FakeAs400Connection
) -> list[str]:
    captured: list[str] = []

    def _fake_connect(connection_string: str) -> _FakeAs400Connection:
        captured.append(connection_string)
        return connection

    import cmcourier.adapters.sources.mssql as mssql_module

    monkeypatch.setattr(mssql_module, "pyodbc", _FakePyodbcModule(_fake_connect))
    return captured


def _make_source(
    *, table: str = "dbo.clientes", query: str | None = None, **overrides: Any
) -> MssqlDataSource:
    kwargs: dict[str, Any] = {
        "host": "h",
        "port": 1433,
        "database": "d",
        "driver": "ODBC Driver 18 for SQL Server",
        "username": "u",
        "password": "p",
        "encrypt": True,
        "trust_server_certificate": True,
        "table": table,
        "query": query,
    }
    kwargs.update(overrides)
    return MssqlDataSource(**kwargs)


class TestConstruction:
    """E1."""

    def test_connection_string_is_tsql_shaped(self) -> None:
        assert _make_source()._build_connection_string() == (
            "DRIVER={ODBC Driver 18 for SQL Server};SERVER=h,1433;DATABASE=d;"
            "UID=u;PWD=p;Encrypt=yes;TrustServerCertificate=yes;"
        )

    def test_encrypt_and_trust_flags_render_no(self) -> None:
        cs = _make_source(encrypt=False, trust_server_certificate=False)._build_connection_string()
        assert "Encrypt=no;" in cs
        assert "TrustServerCertificate=no;" in cs

    def test_table_and_query_are_exclusive(self) -> None:
        with pytest.raises(ConfigurationError):
            _make_source(table="dbo.x", query="SELECT 1")

    def test_shares_the_odbc_base_with_as400(self) -> None:
        assert issubclass(MssqlDataSource, OdbcDataSource)
        assert issubclass(As400DataSource, OdbcDataSource)
        assert MssqlDataSource.LABEL == "mssql"
        assert As400DataSource.LABEL == "as400"


class TestQueries:
    """E2."""

    def test_get_by_fields_keeps_schema_qualified_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cursor = _FakeAs400Cursor(rows=[["1", "MARIA"]], columns=["CIF", "Nombre_Cliente"])
        _patch_mssql_pyodbc(monkeypatch, _FakeAs400Connection(cursor))
        rows = _make_source().get_by_fields({"CIF": "1"})
        assert cursor.executions == [("SELECT * FROM dbo.clientes WHERE CIF = ?", ["1"])]
        assert rows == [{"CIF": "1", "Nombre_Cliente": "MARIA"}]

    def test_query_mode_wraps_as_derived_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(rows=[[7]], columns=["CNT"])
        _patch_mssql_pyodbc(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source(table="", query="SELECT CIF FROM dbo.clientes WHERE Activo = 1")
        assert src.count() == 7
        sql = cursor.executions[0][0]
        assert sql == (
            "SELECT COUNT(*) AS CNT FROM (SELECT CIF FROM dbo.clientes WHERE Activo = 1) AS T"
        )

    def test_get_by_fields_in_chunks_at_1000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(columns=["CIF"])
        _patch_mssql_pyodbc(monkeypatch, _FakeAs400Connection(cursor))
        _make_source().get_by_fields_in("CIF", [str(i) for i in range(1500)], {})
        assert len(cursor.executions) == 2

    def test_pyodbc_error_becomes_indexing_error_with_sqlstate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cursor = _FakeAs400Cursor(raise_on_execute=_FakePyodbcModule.Error("28000", "login failed"))
        _patch_mssql_pyodbc(monkeypatch, _FakeAs400Connection(cursor))
        with pytest.raises(IndexingError) as ei:
            _make_source().query("SELECT 1")
        assert ei.value.context["sqlstate"] == "28000"
        assert str(ei.value).startswith("MSSQL query failed")

    def test_connection_failure_names_mssql(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import cmcourier.adapters.sources.mssql as mssql_module

        def _boom(_cs: str) -> Any:
            raise _FakePyodbcModule.Error("08001", "unreachable")

        monkeypatch.setattr(mssql_module, "pyodbc", _FakePyodbcModule(_boom))
        with pytest.raises(IndexingError) as ei:
            _make_source().query("SELECT 1")
        assert str(ei.value).startswith("MSSQL connection failed")
        assert ei.value.context["host"] == "h"

    def test_network_log_kind_is_mssql_query(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        cursor = _FakeAs400Cursor(rows=[[1]], columns=["X"])
        _patch_mssql_pyodbc(monkeypatch, _FakeAs400Connection(cursor))
        with caplog.at_level(logging.INFO, logger="cmcourier.metrics.network"):
            _make_source().query("SELECT 1")
        kinds = [getattr(r, "kind", None) for r in caplog.records]
        assert "mssql_query" in kinds
