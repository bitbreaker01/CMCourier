"""130 E6 — ``MssqlDataSource`` contra el SQL Server REAL del banco local.

Se salta salvo que ``CMCOURIER_MSSQL_LIVE`` esté seteado. Requisitos:

* ``docker compose -f scripts/staging/mssql-compose.yml up -d`` +
  ``bash scripts/staging/mssql-seed.sh`` (``cmcourier.dbo.clientes``, 1406 filas
  desde ``sample/clients.csv``);
* ``msodbcsql18`` instalado en el host;
* ``CLIENTES_SQL_USERNAME`` / ``CLIENTES_SQL_PASSWORD`` (default ``sa`` /
  ``CmCourier!2026``, la clave del compose).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from cmcourier.adapters.sources.mssql import MssqlDataSource
from cmcourier.cli.doctor import CheckStatus, run_doctor
from cmcourier.config.loader import Credential, Secrets, load_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.mssql_live,
    pytest.mark.skipif(
        not os.environ.get("CMCOURIER_MSSQL_LIVE"),
        reason="set CMCOURIER_MSSQL_LIVE=1 with the local SQL Server container running",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_CONFIG = _ROOT / "sample" / "config-local-mssql.yaml"


def _credential() -> Credential:
    return Credential(
        os.environ.get("CLIENTES_SQL_USERNAME", "sa"),
        os.environ.get("CLIENTES_SQL_PASSWORD", "CmCourier!2026"),
    )


@pytest.fixture
def source() -> Iterator[MssqlDataSource]:
    cred = _credential()
    src = MssqlDataSource(
        host="127.0.0.1",
        port=1433,
        database="cmcourier",
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
        rows = source.get_by_fields_in("CIF", ["396302", "does-not-exist"])
        assert len(rows) == 1

    def test_doctor_metadata_sources_pass(self) -> None:
        config = load_config(_CONFIG)
        secrets = Secrets({"cmis": Credential("x", "x"), "clientes_sql": _credential()})
        report = run_doctor(config, secrets, selected="metadata_sources")
        check = next(r for r in report.results if r.name == "metadata_sources")
        assert check.status == CheckStatus.PASS, check.message

    def test_doctor_mssql_connectivity_pass(self) -> None:
        config = load_config(_CONFIG)
        secrets = Secrets({"cmis": Credential("x", "x"), "clientes_sql": _credential()})
        report = run_doctor(config, secrets, selected="mssql_connectivity")
        check = next(r for r in report.results if r.name == "mssql_connectivity")
        assert check.status == CheckStatus.PASS, check.message
