"""Tests de integración para :class:`As400DataSource`.

pyodbc se mockea en la frontera del atributo del módulo
(``cmcourier.adapters.sources.as400.pyodbc.connect``) así no hace falta
el driver real de unixODBC para correr estos tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from cmcourier.adapters.sources.as400 import As400DataSource
from cmcourier.domain.exceptions import ConfigurationError, IndexingError

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# `Fakes` para el protocolo de cursor / conexión de pyodbc
# ---------------------------------------------------------------------------


class _FakeAs400Cursor:
    """Cursor estilo pyodbc programable para los tests."""

    def __init__(
        self,
        *,
        rows: Sequence[Sequence[Any]] = (),
        columns: Sequence[str] = (),
        raise_on_execute: BaseException | None = None,
    ) -> None:
        self._rows: list[list[Any]] = [list(r) for r in rows]
        self._columns = list(columns)
        self._raise_on_execute = raise_on_execute
        self.executions: list[tuple[str, list[Any]]] = []
        self.fetchmany_calls = 0
        self.closed = False

    @property
    def description(self) -> list[tuple[str, ...]] | None:
        return [(c,) for c in self._columns] if self._columns else None

    def execute(self, sql: str, params: list[Any] | None = None) -> _FakeAs400Cursor:
        self.executions.append((sql, list(params or [])))
        if self._raise_on_execute is not None:
            raise self._raise_on_execute
        return self

    def fetchall(self) -> list[list[Any]]:
        out = self._rows
        self._rows = []
        return out

    def fetchmany(self, size: int) -> list[list[Any]]:
        self.fetchmany_calls += 1
        chunk = self._rows[:size]
        self._rows = self._rows[size:]
        return chunk

    def fetchone(self) -> list[Any] | None:
        if not self._rows:
            return None
        return self._rows.pop(0)

    def close(self) -> None:
        self.closed = True


class _FakeAs400Connection:
    """Conexión estilo pyodbc programable para los tests."""

    def __init__(self, cursor: _FakeAs400Cursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _FakeAs400Cursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_cursor() -> _FakeAs400Cursor:
    return _FakeAs400Cursor()


@pytest.fixture
def fake_connection(fake_cursor: _FakeAs400Cursor) -> _FakeAs400Connection:
    return _FakeAs400Connection(fake_cursor)


def _patch_pyodbc_connect(
    monkeypatch: pytest.MonkeyPatch,
    connection: _FakeAs400Connection,
) -> list[str]:
    captured: list[str] = []

    def _fake_connect(connection_string: str) -> _FakeAs400Connection:
        captured.append(connection_string)
        return connection

    # Import perezoso: pyodbc se importa adentro de _connect(). Parcheamos
    # el atributo pyodbc del módulo después del import.
    import cmcourier.adapters.sources.as400 as as400_module

    monkeypatch.setattr(as400_module, "pyodbc", _FakePyodbcModule(_fake_connect))
    return captured


class _FakePyodbcModule:
    """Reemplazo mínimo del módulo pyodbc."""

    class Error(Exception):
        """Espeja pyodbc.Error para los chequeos de isinstance."""

    def __init__(self, connect_fn: Any) -> None:
        self._connect_fn = connect_fn

    def connect(self, connection_string: str) -> Any:
        return self._connect_fn(connection_string)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(table: str = "TRIGGERS") -> As400DataSource:
    return As400DataSource(
        host="10.0.0.1",
        port=446,
        database="RVILIB",
        driver="iSeries Access ODBC Driver",
        username="tester",
        password="secret-not-real",
        table=table,
    )


# ---------------------------------------------------------------------------
# Construcción + conexión
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_construction_does_not_connect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(_FakeAs400Cursor()))
        _make_source()
        assert captured == []  # todavía no se llamó a connect()

    def test_first_method_call_connects(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_connection: _FakeAs400Connection,
    ) -> None:
        captured = _patch_pyodbc_connect(monkeypatch, fake_connection)
        src = _make_source()
        src.count()  # cualquier método dispara el connect
        assert len(captured) == 1
        # El formato del connection string respeta REQ-002.
        cs = captured[0]
        assert "DRIVER={iSeries Access ODBC Driver}" in cs
        assert "SYSTEM=10.0.0.1" in cs
        assert "PORT=446" in cs
        assert "DATABASE=RVILIB" in cs
        assert "UID=tester" in cs
        assert "PWD=secret-not-real" in cs

    def test_subsequent_calls_reuse_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_connection: _FakeAs400Connection,
    ) -> None:
        captured = _patch_pyodbc_connect(monkeypatch, fake_connection)
        src = _make_source()
        src.count()
        src.count()
        assert len(captured) == 1  # solo se conectó una vez


# ---------------------------------------------------------------------------
# query / query_stream
# ---------------------------------------------------------------------------


class TestQuery:
    def test_query_materializes_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(
            rows=[("JUANPEREZ01", "123456"), ("MARIAGOMEZ02", "234567")],
            columns=("SHORTNAME", "CIF"),
        )
        conn = _FakeAs400Connection(cursor)
        _patch_pyodbc_connect(monkeypatch, conn)
        src = _make_source()
        result = src.query("SELECT SHORTNAME, CIF FROM T", [])
        assert result == [
            {"SHORTNAME": "JUANPEREZ01", "CIF": "123456"},
            {"SHORTNAME": "MARIAGOMEZ02", "CIF": "234567"},
        ]
        assert cursor.executions == [("SELECT SHORTNAME, CIF FROM T", [])]

    def test_query_with_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(rows=[("X",)], columns=("V",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source()
        src.query("SELECT V FROM T WHERE CIF = ?", ["123456"])
        assert cursor.executions[0][1] == ["123456"]

    def test_query_stream_uses_fetchmany(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [(str(i),) for i in range(750)]
        cursor = _FakeAs400Cursor(rows=rows, columns=("V",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source()
        materialized = list(src.query_stream("SELECT V FROM T", []))
        assert len(materialized) == 750
        # 750 filas / `batch` de 500 = 2 `batches` completos; el loop de
        # `streaming` llama a fetchmany hasta que devuelve < batch_size.
        assert cursor.fetchmany_calls >= 2


# ---------------------------------------------------------------------------
# get_by_fields and get_by_fields_in
# ---------------------------------------------------------------------------


class TestGetByFields:
    def test_get_by_fields_builds_where(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(rows=[("X", "1")], columns=("SHORTNAME", "SYS"))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source(table="RVABREP")
        src.get_by_fields({"SHORTNAME": "JUANPEREZ01", "SYS": "1"})
        sql, params = cursor.executions[0]
        assert "FROM RVABREP" in sql
        assert "SHORTNAME = ?" in sql
        assert "SYS = ?" in sql
        assert params == ["JUANPEREZ01", "1"]

    def test_get_by_fields_in_empty_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor()
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source()
        result = src.get_by_fields_in("CIF", values=[], fixed_filters={})
        assert result == []
        assert cursor.executions == []  # nunca se ejecutó

    def test_get_by_fields_in_chunks_at_1000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(columns=("CIF",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source()
        values = [str(i) for i in range(1500)]
        src.get_by_fields_in("CIF", values, fixed_filters={"SYS": "1"})
        # Dos `chunks`: 1000 + 500.
        assert len(cursor.executions) == 2
        first_sql, first_params = cursor.executions[0]
        second_sql, second_params = cursor.executions[1]
        # Cada `chunk` tiene sus params del IN-list + el valor del filtro fijo.
        assert len(first_params) == 1001
        assert first_params[-1] == "1"  # filtro fijo
        assert len(second_params) == 501


class TestStreamByFieldsIn148:
    """148 REQ-001: el equivalente en stream de ``get_by_fields_in``.

    Con el censo siempre activo el filtro por sistema es la configuración
    NORMAL y trae el sistema ENTERO: no puede terminar en un
    ``fetchall()``.
    """

    def test_uses_fetchmany_never_fetchall(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [(str(i),) for i in range(750)]
        cursor = _FakeAs400Cursor(rows=rows, columns=("V",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source(table="RVABREP")
        materialized = list(src.stream_by_fields_in("SYS", ["1"], fixed_filters={}))
        assert len(materialized) == 750
        assert cursor.fetchmany_calls >= 2

    def test_is_lazy_no_sql_until_first_next(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(rows=[("X",)], columns=("V",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source(table="RVABREP")
        stream = src.stream_by_fields_in("SYS", ["1"], fixed_filters={})
        assert cursor.executions == []  # todavía no se ejecutó nada
        next(iter(stream))
        assert len(cursor.executions) == 1

    def test_empty_values_yields_nothing_and_never_executes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cursor = _FakeAs400Cursor()
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source()
        assert list(src.stream_by_fields_in("SYS", [], fixed_filters={})) == []
        assert cursor.executions == []

    def test_chunks_the_in_list_at_1000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(columns=("SYS",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source()
        values = [str(i) for i in range(1500)]
        list(src.stream_by_fields_in("SYS", values, fixed_filters={"LIB": "P"}))
        assert len(cursor.executions) == 2
        assert len(cursor.executions[0][1]) == 1001
        assert cursor.executions[0][1][-1] == "P"  # filtro fijo
        assert len(cursor.executions[1][1]) == 501


# ---------------------------------------------------------------------------
# get_all + count
# ---------------------------------------------------------------------------


class TestGetAllCount:
    def test_get_all_yields_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [(str(i),) for i in range(3)]
        cursor = _FakeAs400Cursor(rows=rows, columns=("V",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source(table="RVABREP")
        result = list(src.get_all())
        assert len(result) == 3
        assert cursor.executions[0][0] == "SELECT * FROM RVABREP"

    def test_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(rows=[(42,)], columns=("CNT",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = _make_source(table="RVABREP")
        assert src.count() == 42
        executed_sql = cursor.executions[0][0]
        assert "SELECT COUNT(*)" in executed_sql
        assert "FROM RVABREP" in executed_sql


# ---------------------------------------------------------------------------
# Envoltura de errores
# ---------------------------------------------------------------------------


class TestErrorWrapping:
    def test_pyodbc_error_wrapped_in_indexing_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arma un pyodbc.Error fake que imita la forma (sqlstate, message).
        import cmcourier.adapters.sources.as400 as as400_module

        captured: list[str] = []

        def _fake_connect(cs: str) -> _FakeAs400Connection:
            captured.append(cs)
            cursor = _FakeAs400Cursor(
                raise_on_execute=as400_module.pyodbc.Error("42S02", "Table not found"),
            )
            return _FakeAs400Connection(cursor)

        monkeypatch.setattr(
            as400_module,
            "pyodbc",
            _FakePyodbcModule(_fake_connect),
        )
        src = _make_source()
        with pytest.raises(IndexingError) as ei:
            src.query("SELECT * FROM NOPE", [])
        assert isinstance(ei.value.__cause__, as400_module.pyodbc.Error)

    def test_pyodbc_connect_error_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import cmcourier.adapters.sources.as400 as as400_module

        def _raise(cs: str) -> _FakeAs400Connection:
            raise as400_module.pyodbc.Error("08001", "Connection refused")

        monkeypatch.setattr(as400_module, "pyodbc", _FakePyodbcModule(_raise))
        src = _make_source()
        with pytest.raises(IndexingError):
            src.count()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_close_idempotent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_connection: _FakeAs400Connection,
    ) -> None:
        _patch_pyodbc_connect(monkeypatch, fake_connection)
        src = _make_source()
        src.count()
        src.close()
        src.close()  # no-op
        assert fake_connection.closed


# ---------------------------------------------------------------------------
# 018: override de query por fuente
# ---------------------------------------------------------------------------


_BASE_KWARGS: dict[str, Any] = {
    "host": "10.0.0.1",
    "port": 446,
    "database": "RVILIB",
    "driver": "iSeries Access ODBC Driver",
    "username": "tester",
    "password": "secret-not-real",
}


class TestConstructionValidation:
    def test_both_table_and_query_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            As400DataSource(
                **_BASE_KWARGS,
                table="CUSTOMERS",
                query="SELECT * FROM CUSTOMERS",
            )

    def test_query_only_construction_ok(self) -> None:
        # No se espera excepción — modo query.
        As400DataSource(
            **_BASE_KWARGS,
            query="SELECT CIF, NAME FROM CUSTOMERS WHERE ACTIVE = 'Y'",
        )

    def test_neither_table_nor_query_ok_raw_mode(self) -> None:
        # Modo crudo para callers que usan query()/query_stream() directo
        # (por ejemplo As400TriggerStrategy).
        As400DataSource(**_BASE_KWARGS)


class TestQueryMode:
    def test_get_all_with_query_uses_subquery_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(rows=[("CIF1", "NAME1")], columns=("CIF", "NAME"))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = As400DataSource(
            **_BASE_KWARGS,
            query="SELECT CIF, NAME FROM CUSTOMERS WHERE ACTIVE = 'Y'",
        )
        list(src.get_all())
        sql = cursor.executions[0][0]
        assert sql == "SELECT * FROM (SELECT CIF, NAME FROM CUSTOMERS WHERE ACTIVE = 'Y') AS T"

    def test_count_with_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(rows=[(7,)], columns=("CNT",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = As400DataSource(
            **_BASE_KWARGS,
            query="SELECT 1 FROM SYSDUMMY1",
        )
        assert src.count() == 7
        sql = cursor.executions[0][0]
        assert "COUNT(*)" in sql
        assert "FROM (SELECT 1 FROM SYSDUMMY1) AS T" in sql

    def test_get_by_fields_with_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _FakeAs400Cursor(rows=[("123", "Name")], columns=("CIF", "NAME"))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = As400DataSource(
            **_BASE_KWARGS,
            query="SELECT CIF, NAME FROM CUSTOMERS",
        )
        src.get_by_fields({"CIF": "123"})
        sql, params = cursor.executions[0]
        assert "FROM (SELECT CIF, NAME FROM CUSTOMERS) AS T" in sql
        assert "CIF = ?" in sql
        assert params == ["123"]

    def test_table_mode_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regresión de backwards-compat: el modo tabla sigue usando el identificador pelado.
        cursor = _FakeAs400Cursor(rows=[(1,)], columns=("CNT",))
        _patch_pyodbc_connect(monkeypatch, _FakeAs400Connection(cursor))
        src = As400DataSource(**_BASE_KWARGS, table="CUSTOMERS")
        src.count()
        sql = cursor.executions[0][0]
        # La cláusula FROM referencia el identificador pelado de tabla; sin alias de tabla derivada.
        assert "FROM CUSTOMERS" in sql
        assert " AS T" not in sql
