"""Fuente de datos AS400 vía ODBC — :class:`IDataSource` concreto sobre pyodbc.

Import lazy de ``pyodbc`` dentro de :meth:`_connect` para que importar este
módulo en entornos sin headers de unixODBC no rompa (el error se manifiesta
en la primera llamada real).

El driver ODBC de AS400 NO es thread-safe a nivel conexión
(``threadsafety = 1``). 106: cada thread cachea su propia conexión vía
:class:`ThreadLocalConnectionPool`; las conexiones de threads muertos se podan
cuando un thread nuevo abre la suya.

Todas las excepciones :class:`pyodbc.Error` se envuelven en
:class:`cmcourier.domain.exceptions.IndexingError`. Los códigos SQLSTATE se
extraen de ``exc.args[0]`` cuando están presentes.

Principio VIII de la Constitución: las consultas SQL y sus parámetros PUEDEN
contener valores de clientes (CIF, nombres). El adaptador NUNCA loguea el
cuerpo del SQL ni los parámetros; los callers son responsables de su propia
auditoría.
"""

from __future__ import annotations

__all__ = ["As400DataSource"]

import logging
import re
import time
from collections.abc import Iterator, Mapping
from typing import Any

from cmcourier.adapters.connection_pool import ThreadLocalConnectionPool
from cmcourier.domain.exceptions import ConfigurationError, IndexingError
from cmcourier.domain.ports import IDataSource

_network_log = logging.getLogger("cmcourier.metrics.network")

# Import lazy: el nombre `pyodbc` se resuelve dentro de `_connect()` para que
# los entornos de test sin headers de unixODBC puedan `import` este módulo.
pyodbc: Any = None

_log = logging.getLogger(__name__)

_IN_CHUNK_SIZE = 1000
_STREAM_BATCH_SIZE = 500
_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")


class As400DataSource(IDataSource):
    """IDataSource concreto sobre una conexión ODBC a AS400.

    Acepta o bien ``table`` (un identificador suelto) o bien un ``query`` de
    prefetch personalizado (una sentencia ``SELECT ...`` completa), pero
    nunca ambos. En modo query, el SQL se envuelve en un alias de tabla
    derivada (``(query) AS T``) para que el contrato IDataSource completo
    (``get_all``, ``count``, ``get_by_fields*``) siga funcionando de manera
    transparente. El "modo raw" (ninguno seteado) es válido para callers que
    solo invocan :meth:`query` o :meth:`query_stream` directamente (por
    ejemplo, trigger strategies que manejan su propio SQL).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        driver: str,
        username: str,
        password: str,
        table: str = "",
        query: str | None = None,
    ) -> None:
        if table and query:
            raise ConfigurationError(
                "As400DataSource: `table` and `query` are mutually exclusive",
            )
        self._host = host
        self._port = port
        self._database = database
        self._driver = driver
        self._username = username
        self._password = password
        self._source_expr = f"({query}) AS T" if query else table
        # 106: conexión por thread — pyodbc no permite compartir una
        # conexión entre threads (S1/S3 la golpean desde prep_workers).
        self._pool = ThreadLocalConnectionPool(self._open_connection)
        self._closed = False

    # ------------------------------------------------------------------ puertos

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cursor = self._connect().cursor()
        t0 = time.monotonic()
        try:
            cursor.execute(sql, params or [])
            columns = [col[0] for col in cursor.description or []]
            rows = [_normalize_row(columns, row) for row in cursor.fetchall()]
            _network_log.info(
                "as400_query",
                extra={
                    "kind": "as400_query",
                    "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
                    "sql_prefix": sql[:80],
                    "row_count": len(rows),
                },
            )
            return rows
        except _pyodbc_error_type() as exc:
            raise IndexingError(
                "AS400 query failed",
                sql_prefix=sql[:80],
                sqlstate=_extract_sqlstate(exc),
            ) from exc
        finally:
            cursor.close()

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        cursor = self._connect().cursor()
        t0 = time.monotonic()
        yielded = 0
        try:
            cursor.execute(sql, params or [])
            columns = [col[0] for col in cursor.description or []]
            while True:
                rows = cursor.fetchmany(_STREAM_BATCH_SIZE)
                if not rows:
                    _network_log.info(
                        "as400_query",
                        extra={
                            "kind": "as400_query",
                            "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
                            "sql_prefix": sql[:80],
                            "row_count": yielded,
                        },
                    )
                    return
                for row in rows:
                    yielded += 1
                    yield _normalize_row(columns, row)
        except _pyodbc_error_type() as exc:
            raise IndexingError(
                "AS400 query_stream failed",
                sql_prefix=sql[:80],
                sqlstate=_extract_sqlstate(exc),
            ) from exc
        finally:
            cursor.close()

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not filters:
            return self.query(f"SELECT * FROM {self._source_expr}", [])
        cols = list(filters.keys())
        where = " AND ".join(f"{c} = ?" for c in cols)
        sql = f"SELECT * FROM {self._source_expr} WHERE {where}"
        return self.query(sql, [filters[c] for c in cols])

    def get_by_fields_in(
        self,
        field: str,
        values: list[Any],
        fixed_filters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        if not values:
            return []
        fixed_cols = list(fixed_filters.keys())
        fixed_vals = [fixed_filters[c] for c in fixed_cols]
        fixed_clause = " AND " + " AND ".join(f"{c} = ?" for c in fixed_cols) if fixed_cols else ""
        results: list[dict[str, Any]] = []
        for start in range(0, len(values), _IN_CHUNK_SIZE):
            chunk = values[start : start + _IN_CHUNK_SIZE]
            placeholders = ", ".join("?" * len(chunk))
            sql = (
                f"SELECT * FROM {self._source_expr} WHERE {field} IN ({placeholders}){fixed_clause}"
            )
            results.extend(self.query(sql, list(chunk) + fixed_vals))
        return results

    def get_all(self) -> Iterator[dict[str, Any]]:
        return self.query_stream(f"SELECT * FROM {self._source_expr}", [])

    def count(self) -> int:
        rows = self.query(f"SELECT COUNT(*) AS CNT FROM {self._source_expr}", [])
        if not rows:
            return 0
        first = rows[0]
        for value in first.values():
            if isinstance(value, int):
                return value
        return 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.close_all()

    # ------------------------------------------------------------------ internos

    def _connect(self) -> Any:
        return self._pool.acquire()

    def _open_connection(self) -> Any:
        _import_pyodbc()
        try:
            return pyodbc.connect(self._build_connection_string())
        except _pyodbc_error_type() as exc:
            raise IndexingError(
                "AS400 connection failed",
                host=self._host,
                sqlstate=_extract_sqlstate(exc),
            ) from exc

    def _build_connection_string(self) -> str:
        return (
            f"DRIVER={{{self._driver}}};"
            f"SYSTEM={self._host};"
            f"PORT={self._port};"
            f"DATABASE={self._database};"
            f"UID={self._username};"
            f"PWD={self._password};"
        )


# ---------------------------------------------------------------------------
# Helpers a nivel de módulo
# ---------------------------------------------------------------------------


def _normalize_row(columns: list[str], row: Any) -> dict[str, Any]:
    """074: strippea whitespace de valores ``str`` al materializar
    una fila pyodbc.

    Los campos ``CHAR(N)`` de DB2 / iSeries vuelven *padded* a
    longitud fija con espacios — un ``CHAR(1)`` vacío llega como
    ``" "``, un ``CHAR(8)`` con ``"SHORT1"`` llega como
    ``"SHORT1  "``. Pre-074 ese padding filtraba al dominio y
    rompía:

    * el check de "deleted" (``if _str(row.get("ABACST")):``
      interpretaba ``" "`` como truthy y tiraba
      ``RVABREPDeletedError``);
    * el matching de triggers contra el RVABREP (``"SHORT1  "``
      != ``"SHORT1"``);
    * la idempotency key (``rvabrep_txn_num``) que terminaba en
      SQLite + CMIS con trailing spaces.

    Strippeamos en la frontera adapter-dominio. Solo afecta
    valores ``str``; tipos numéricos, ``date`` / ``datetime``,
    ``bool``, ``bytes`` y ``None`` pasan sin tocar.
    """
    return {
        col: (value.strip() if isinstance(value, str) else value)
        for col, value in zip(columns, row, strict=False)
    }


def _import_pyodbc() -> None:
    global pyodbc
    if pyodbc is not None:
        return
    import pyodbc as _pyodbc  # noqa: PLC0415 — import lazy intencional

    pyodbc = _pyodbc


def _pyodbc_error_type() -> type[BaseException]:
    if pyodbc is None:
        return RuntimeError
    return pyodbc.Error  # type: ignore[no-any-return]


def _extract_sqlstate(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], str) and _SQLSTATE_RE.match(args[0]):
        return args[0]
    return ""
