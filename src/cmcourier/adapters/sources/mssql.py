"""Fuente de datos SQL Server vía ODBC (130) — :class:`IDataSource` sobre pyodbc.

Hereda el contrato completo de
:class:`cmcourier.adapters.sources.odbc_base.OdbcDataSource`; acá vive sólo
la connection string T-SQL (``SERVER=host,port`` + ``Encrypt`` /
``TrustServerCertificate``) y el sentinel ``pyodbc`` del módulo, que los
tests parchean con ``monkeypatch.setattr(mssql_module, "pyodbc", fake)``.

``table`` admite identificadores con esquema (``dbo.clientes``); ``query``
se envuelve como ``(query) AS T`` — T-SQL exige el alias en tablas
derivadas, así que la forma es la misma que en DB2.
"""

from __future__ import annotations

__all__ = ["MssqlDataSource"]

from typing import Any, ClassVar

from cmcourier.adapters.sources.odbc_base import OdbcDataSource

# Import lazy: ver `as400.py`.
pyodbc: Any = None


class MssqlDataSource(OdbcDataSource):
    """IDataSource concreto sobre una conexión ODBC a SQL Server."""

    LABEL: ClassVar[str] = "mssql"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        driver: str,
        username: str,
        password: str,
        encrypt: bool = True,
        trust_server_certificate: bool = False,
        table: str = "",
        query: str | None = None,
    ) -> None:
        super().__init__(
            host=host,
            port=port,
            database=database,
            driver=driver,
            username=username,
            password=password,
            table=table,
            query=query,
        )
        self._encrypt = encrypt
        self._trust_server_certificate = trust_server_certificate

    def _driver_module(self) -> Any:
        _import_pyodbc()
        return pyodbc

    def _build_connection_string(self) -> str:
        return (
            f"DRIVER={{{self._driver}}};"
            f"SERVER={self._host},{self._port};"
            f"DATABASE={self._database};"
            f"UID={self._username};"
            f"PWD={self._password};"
            f"Encrypt={_yes_no(self._encrypt)};"
            f"TrustServerCertificate={_yes_no(self._trust_server_certificate)};"
        )


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def _import_pyodbc() -> None:
    global pyodbc
    if pyodbc is not None:
        return
    import pyodbc as _pyodbc  # noqa: PLC0415 — import lazy intencional

    pyodbc = _pyodbc
