"""Base ODBC compartida por las fuentes de base de datos (130).

:class:`OdbcDataSource` implementa el contrato :class:`IDataSource` completo
sobre ``pyodbc`` — ``query`` / ``query_stream``, filtros ``WHERE`` con
chunks para ``IN`` (1000 valores), ``get_all``, ``count``, ``close`` — más el
pool thread-local (106) y la normalización de filas (074). Lo que cambia
entre motores queda en la subclase:

* ``_build_connection_string()`` — ``SYSTEM=`` es de IBM i, ``SERVER=h,p``
  es de SQL Server;
* ``_driver_module()`` — cada módulo concreto conserva su sentinel global
  ``pyodbc`` (import lazy) para que ``monkeypatch.setattr(modulo, "pyodbc",
  fake)`` siga funcionando en los tests;
* ``LABEL`` — ``"as400"`` / ``"mssql"``: da el ``kind`` del evento
  ``cmcourier.metrics.network`` (``<label>_query``) y el prefijo de los
  mensajes de :class:`IndexingError`.

Todas las excepciones ``pyodbc.Error`` se envuelven en
:class:`cmcourier.domain.exceptions.IndexingError`; el SQLSTATE se extrae de
``exc.args[0]`` cuando está presente.

Principio VIII de la Constitución: las consultas y sus parámetros PUEDEN
contener valores de clientes. La base NUNCA loguea el cuerpo del SQL ni los
parámetros — sólo un prefijo de 80 caracteres.
"""

from __future__ import annotations

__all__ = ["OdbcDataSource", "extract_sqlstate", "normalize_row"]

import logging
import re
import time
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

from cmcourier.adapters.connection_pool import ThreadLocalConnectionPool
from cmcourier.domain.exceptions import ConfigurationError, IndexingError
from cmcourier.domain.ports import IDataSource

_network_log = logging.getLogger("cmcourier.metrics.network")

_IN_CHUNK_SIZE = 1000
_STREAM_BATCH_SIZE = 500
_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")


class OdbcDataSource(IDataSource):
    """``IDataSource`` sobre una conexión ODBC; ver docstring del módulo.

    Acepta o bien ``table`` (identificador, con esquema si el motor lo usa)
    o bien un ``query`` de prefetch (un ``SELECT ...`` completo), pero nunca
    ambos. En modo query el SQL se envuelve como tabla derivada
    (``(query) AS T``) para que ``get_all`` / ``count`` / ``get_by_fields*``
    sigan funcionando. El "modo raw" (ninguno) sirve a callers que sólo usan
    :meth:`query` / :meth:`query_stream`.
    """

    LABEL: ClassVar[str] = "odbc"

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
                f"{type(self).__name__}: `table` and `query` are mutually exclusive",
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

    # ------------------------------------------------------------- subclase

    @abstractmethod
    def _build_connection_string(self) -> str:
        """Connection string ODBC del motor concreto."""

    @abstractmethod
    def _driver_module(self) -> Any:
        """Módulo ``pyodbc`` (o su fake) visto desde el módulo de la subclase."""

    # ------------------------------------------------------------------ puertos

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cursor = self._connect().cursor()
        t0 = time.monotonic()
        try:
            cursor.execute(sql, params or [])
            columns = [col[0] for col in cursor.description or []]
            rows = [normalize_row(columns, row) for row in cursor.fetchall()]
            self._log_query(sql, t0, len(rows))
            return rows
        except self._error_type() as exc:
            raise IndexingError(
                f"{self.LABEL.upper()} query failed",
                sql_prefix=sql[:80],
                sqlstate=extract_sqlstate(exc),
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
                    self._log_query(sql, t0, yielded)
                    return
                for row in rows:
                    yielded += 1
                    yield normalize_row(columns, row)
        except self._error_type() as exc:
            raise IndexingError(
                f"{self.LABEL.upper()} query_stream failed",
                sql_prefix=sql[:80],
                sqlstate=extract_sqlstate(exc),
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
        driver = self._driver_module()
        try:
            return driver.connect(self._build_connection_string())
        except self._error_type() as exc:
            raise IndexingError(
                f"{self.LABEL.upper()} connection failed",
                host=self._host,
                sqlstate=extract_sqlstate(exc),
            ) from exc

    def _error_type(self) -> type[BaseException]:
        """``pyodbc.Error`` si el módulo ya se importó; ``RuntimeError`` si no."""
        driver = self._driver_module()
        if driver is None:
            return RuntimeError
        return driver.Error  # type: ignore[no-any-return]

    def _log_query(self, sql: str, t0: float, row_count: int) -> None:
        kind = f"{self.LABEL}_query"
        _network_log.info(
            kind,
            extra={
                "kind": kind,
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
                "sql_prefix": sql[:80],
                "row_count": row_count,
            },
        )


# ---------------------------------------------------------------------------
# Helpers a nivel de módulo
# ---------------------------------------------------------------------------


def normalize_row(columns: list[str], row: Any) -> dict[str, Any]:
    """074: strippea whitespace de valores ``str`` al materializar una fila.

    Los campos ``CHAR(N)`` de DB2 / iSeries (y de SQL Server) vuelven
    *padded* a longitud fija con espacios — un ``CHAR(1)`` vacío llega como
    ``" "``, un ``CHAR(8)`` con ``"SHORT1"`` llega como ``"SHORT1  "``.
    Pre-074 ese padding filtraba al dominio y rompía el check de "deleted",
    el matching de triggers contra el RVABREP y la idempotency key.

    Strippeamos en la frontera adapter-dominio. Sólo afecta valores ``str``;
    tipos numéricos, ``date`` / ``datetime``, ``bool``, ``bytes`` y ``None``
    pasan sin tocar.
    """
    return {
        col: (value.strip() if isinstance(value, str) else value)
        for col, value in zip(columns, row, strict=False)
    }


def extract_sqlstate(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], str) and _SQLSTATE_RE.match(args[0]):
        return args[0]
    return ""
