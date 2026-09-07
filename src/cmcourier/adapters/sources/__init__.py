"""Adaptadores de fuentes de datos: CSV/XLSX (pandas), AS400 y SQL Server (pyodbc).

Implementaciones concretas de :class:`cmcourier.domain.ports.IDataSource`.
El adaptador tabular (CSV/XLSX) es el sustituto canónico de las bases para
dev/test según el Principio VI de la Constitución. Los adaptadores ODBC
comparten :class:`OdbcDataSource` (130): contrato, pool thread-local (106) y
normalización de filas (074); cada uno aporta su connection string.
"""

from __future__ import annotations

__all__ = ["As400DataSource", "MssqlDataSource", "OdbcDataSource", "TabularDataSource"]

from cmcourier.adapters.sources.as400 import As400DataSource
from cmcourier.adapters.sources.mssql import MssqlDataSource
from cmcourier.adapters.sources.odbc_base import OdbcDataSource
from cmcourier.adapters.sources.tabular import TabularDataSource
