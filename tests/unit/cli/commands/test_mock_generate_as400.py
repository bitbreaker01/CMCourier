"""Tests unitarios para ``mock generate --rvabrep-as400`` (073)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.cli.commands import mock as mock_cmd
from cmcourier.config.loader import Credential, Secrets
from cmcourier.config.schema import As400ConnectionConfig, As400RvabrepSource, ConnectionRef

pytestmark = pytest.mark.unit


@dataclass
class _StubConfig:
    indexing: Any

    def connection_ref(self, site: str) -> ConnectionRef | None:
        if site != "indexing":
            return None
        conn = self.indexing.source.connection
        return ConnectionRef(alias="as400", kind="as400", spec=conn, site=site)


@dataclass
class _StubIndexing:
    source: Any


def _make_config(
    *, query: str | None = None, table: str | None = None, database: str = "RVILIB"
) -> _StubConfig:
    conn = As400ConnectionConfig(host="as400.test", port=446, database=database, table=table)
    source = As400RvabrepSource(kind="as400", connection=conn, query=query or "")
    return _StubConfig(indexing=_StubIndexing(source=source))


@pytest.fixture(autouse=True)
def _stub_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_build_source`` llama ``load_secrets`` adentro; le proveemos credenciales fake."""

    def fake_load_secrets(config: Any = None, *, require_cmis: bool = True) -> Secrets:
        return Secrets({"cmis": Credential("c", "c"), "as400": Credential("user", "pass")})

    monkeypatch.setattr(mock_cmd, "load_secrets", fake_load_secrets)


@pytest.fixture
def captured_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Captura los kwargs con que se construye ``As400DataSource``."""

    captured: dict[str, Any] = {}

    def fake_ctor(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(mock_cmd, "As400DataSource", fake_ctor)
    return captured


class TestRespectsSourceQuery:
    """073 — si el YAML define ``indexing.source.query``, mock generate debe
    pasárselo al adapter. Pre-073 lo ignoraba y leía la tabla entera.
    """

    def test_query_with_where_is_forwarded(self, captured_kwargs: dict[str, Any]) -> None:
        config = _make_config(
            query="SELECT * FROM RVILIB.RVABREP WHERE ABACST <> 'D' AND ABAACD = 'BAC'"
        )
        mock_cmd._build_source(  # type: ignore[attr-defined]
            rvabrep_csv=None,
            rvabrep_as400=True,
            config=config,  # type: ignore[arg-type]
        )
        assert (
            captured_kwargs.get("query")
            == "SELECT * FROM RVILIB.RVABREP WHERE ABACST <> 'D' AND ABAACD = 'BAC'"
        )
        # El path "table" NO se usa cuando hay query.
        assert "table" not in captured_kwargs

    def test_whitespace_only_query_is_treated_as_empty(
        self, captured_kwargs: dict[str, Any]
    ) -> None:
        # Edge: el YAML puede tener ``query: " "``. Tratarlo como vacío
        # para caer al fallback con schema.
        config = _make_config(query="   ")
        mock_cmd._build_source(  # type: ignore[attr-defined]
            rvabrep_csv=None,
            rvabrep_as400=True,
            config=config,  # type: ignore[arg-type]
        )
        assert "query" not in captured_kwargs
        assert captured_kwargs.get("table") == "RVILIB.RVABREP"


class TestFallbackTablePrependsSchema:
    """073 — cuando no hay query ni table, el fallback debe ser
    ``{database}.RVABREP`` (con schema), no ``RVABREP`` bare. Pre-073 el
    SELECT ejecutado era ``SELECT * FROM RVABREP`` que falla con table
    not found cuando la library no está en la library list del usuario.
    """

    def test_no_query_no_table_falls_back_to_schema_qualified(
        self, captured_kwargs: dict[str, Any]
    ) -> None:
        config = _make_config(query=None, table=None, database="RVILIB")
        mock_cmd._build_source(  # type: ignore[attr-defined]
            rvabrep_csv=None,
            rvabrep_as400=True,
            config=config,  # type: ignore[arg-type]
        )
        assert captured_kwargs.get("table") == "RVILIB.RVABREP"

    def test_no_query_no_table_uses_custom_database(self, captured_kwargs: dict[str, Any]) -> None:
        config = _make_config(query=None, table=None, database="CUSTOMLIB")
        mock_cmd._build_source(  # type: ignore[attr-defined]
            rvabrep_csv=None,
            rvabrep_as400=True,
            config=config,  # type: ignore[arg-type]
        )
        assert captured_kwargs.get("table") == "CUSTOMLIB.RVABREP"

    def test_explicit_table_is_respected_without_modification(
        self, captured_kwargs: dict[str, Any]
    ) -> None:
        # Si el operador pone `connection.table: "MYLIB.MYTABLE"`, lo
        # respetamos sin pisar — incluso si difiere del database.
        config = _make_config(query=None, table="MYLIB.MYTABLE", database="RVILIB")
        mock_cmd._build_source(  # type: ignore[attr-defined]
            rvabrep_csv=None,
            rvabrep_as400=True,
            config=config,  # type: ignore[arg-type]
        )
        assert captured_kwargs.get("table") == "MYLIB.MYTABLE"


class TestNoCmisCredentialsNeeded:
    """``mock generate --rvabrep-as400`` sólo lee el AS400: exigir
    ``CMIS_USERNAME`` / ``CMIS_PASSWORD`` era un error (el operador en
    PowerShell sin las env vars de CMIS no podía materializar el árbol)."""

    def test_loads_secrets_without_requiring_cmis(
        self, monkeypatch: pytest.MonkeyPatch, captured_kwargs: dict[str, Any]
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_load_secrets(config: Any = None, *, require_cmis: bool = True) -> Secrets:
            seen["require_cmis"] = require_cmis
            return Secrets({"cmis": Credential("", ""), "as400": Credential("user", "pass")})

        monkeypatch.setattr(mock_cmd, "load_secrets", fake_load_secrets)
        mock_cmd._build_source(  # type: ignore[attr-defined]
            rvabrep_csv=None,
            rvabrep_as400=True,
            config=_make_config(table="RVABREP"),  # type: ignore[arg-type]
        )
        assert seen["require_cmis"] is False
        assert captured_kwargs["username"] == "user"
