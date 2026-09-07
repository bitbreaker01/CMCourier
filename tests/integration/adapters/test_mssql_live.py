"""130 E6 — ``MssqlDataSource`` contra el SQL Server REAL del banco local.

Se salta salvo que ``CMCOURIER_MSSQL_LIVE`` esté seteado. Requisitos:

* ``docker compose -f scripts/staging/mssql-compose.yml up -d`` +
  ``bash scripts/staging/mssql-seed.sh`` (``cmcourier.dbo.clientes``, 1406 filas
  desde ``sample/clients.csv``);
* ``msodbcsql18`` instalado en el host;
* ``CLIENTES_SQL_USERNAME`` / ``CLIENTES_SQL_PASSWORD`` (default ``sa`` /
  ``CmCourier!2026``, la clave del compose).

La config del doctor se genera en ``tmp_path`` sobre los fixtures del repo
(``sample/`` está gitignoreado: en otro clon no existe).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from cmcourier.adapters.sources.mssql import MssqlDataSource
from cmcourier.cli.doctor import CheckStatus, run_doctor
from cmcourier.config.loader import Credential, Secrets, load_config
from cmcourier.config.schema import PipelineConfig
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.mssql_live,
    pytest.mark.skipif(
        not os.environ.get("CMCOURIER_MSSQL_LIVE"),
        reason="set CMCOURIER_MSSQL_LIVE=1 with the local SQL Server container running",
    ),
]

_HOST, _PORT, _DATABASE = "127.0.0.1", 1433, "cmcourier"


def _credential() -> Credential:
    return Credential(
        os.environ.get("CLIENTES_SQL_USERNAME", "sa"),
        os.environ.get("CLIENTES_SQL_PASSWORD", "CmCourier!2026"),
    )


def _live_config(tmp_path: Path) -> PipelineConfig:
    """Config de fixtures + registro ``clientes_sql`` + fuente ``mssql:clientes``."""
    _, path = _make_config(tmp_path)
    text = (
        "connections:\n"
        f"  clientes_sql: {{kind: mssql, host: {_HOST}, port: {_PORT}, database: {_DATABASE},"
        " encrypt: true, trust_server_certificate: true}\n"
    ) + path.read_text()
    text = text.replace(
        "metadata:\n  field_aliases: {}\n  field_sources: {}\n",
        "metadata:\n  field_aliases: {}\n  field_sources: {}\n  sources:\n"
        "    - kind: mssql\n      alias: clientes\n      connection: clientes_sql\n"
        "      table: dbo.clientes\n",
    )
    path.write_text(text)
    return load_config(path)


def _secrets() -> Secrets:
    return Secrets({"cmis": Credential("x", "x"), "clientes_sql": _credential()})


@pytest.fixture
def source() -> Iterator[MssqlDataSource]:
    cred = _credential()
    src = MssqlDataSource(
        host=_HOST,
        port=_PORT,
        database=_DATABASE,
        driver="ODBC Driver 18 for SQL Server",
        username=cred.username,
        password=cred.password,
        trust_server_certificate=True,
        table="dbo.clientes",
    )
    try:
        yield src
    finally:
        src.close()


class TestMssqlLive:
    def test_count_matches_seed(self, source: MssqlDataSource) -> None:
        assert source.count() == 1406

    def test_lookup_by_cif(self, source: MssqlDataSource) -> None:
        rows = source.get_by_fields({"CIF": "396302"})
        assert [r["Nombre_Cliente"] for r in rows] == ["MARIA13"]

    def test_get_by_fields_in_chunks(self, source: MssqlDataSource) -> None:
        rows = source.get_by_fields_in("CIF", ["396302", "does-not-exist"], {})
        assert len(rows) == 1

    def test_doctor_metadata_sources_pass(self, tmp_path: Path) -> None:
        report = run_doctor(_live_config(tmp_path), _secrets(), selected="metadata_sources")
        check = next(r for r in report.results if r.name == "metadata_sources")
        assert check.status == CheckStatus.PASS, check.message

    def test_doctor_mssql_connectivity_pass(self, tmp_path: Path) -> None:
        report = run_doctor(_live_config(tmp_path), _secrets(), selected="mssql_connectivity")
        check = next(r for r in report.results if r.name == "mssql_connectivity")
        assert check.status == CheckStatus.PASS, check.message
