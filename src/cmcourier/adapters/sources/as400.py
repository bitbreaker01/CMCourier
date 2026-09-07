"""Fuente de datos AS400 vía ODBC — :class:`IDataSource` concreto sobre pyodbc.

Desde 130 el contrato ``IDataSource``, el pool thread-local (106) y la
normalización de filas (074) viven en
:class:`cmcourier.adapters.sources.odbc_base.OdbcDataSource`; acá queda lo
específico de IBM i: la connection string (``SYSTEM=``) y el sentinel
``pyodbc`` del módulo.

Import lazy de ``pyodbc`` dentro de :func:`_import_pyodbc` para que importar
este módulo en entornos sin headers de unixODBC no rompa (el error se
manifiesta en la primera llamada real). Los tests parchean
``cmcourier.adapters.sources.as400.pyodbc``.

El driver ODBC de AS400 NO es thread-safe a nivel conexión
(``threadsafety = 1``): cada thread cachea su propia conexión.
"""

from __future__ import annotations

__all__ = ["As400DataSource"]

from typing import Any, ClassVar

from cmcourier.adapters.sources.odbc_base import (
    OdbcDataSource,
    extract_sqlstate,
    normalize_row,
)

# Import lazy: el nombre `pyodbc` se resuelve dentro de `_import_pyodbc()`
# para que los entornos de test sin headers de unixODBC puedan `import` este
# módulo.
pyodbc: Any = None


class As400DataSource(OdbcDataSource):
    """IDataSource concreto sobre una conexión ODBC a AS400 (DB2 / iSeries)."""

    LABEL: ClassVar[str] = "as400"

    def _driver_module(self) -> Any:
        _import_pyodbc()
        return pyodbc

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
# Helpers a nivel de módulo (compat: ``_normalize_row`` sigue importable de acá)
# ---------------------------------------------------------------------------

_normalize_row = normalize_row
_extract_sqlstate = extract_sqlstate


def _import_pyodbc() -> None:
    global pyodbc
    if pyodbc is not None:
        return
    import pyodbc as _pyodbc  # noqa: PLC0415 — import lazy intencional

    pyodbc = _pyodbc
