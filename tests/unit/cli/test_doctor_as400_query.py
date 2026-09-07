"""Tests unitarios para los queries que ``cmcourier doctor`` envía a AS400 (073)
y para la prueba por conexión del registro (129)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.cli import doctor as doctor_module
from cmcourier.cli.doctor import (
    CheckStatus,
    _check_as400_connectivity,  # type: ignore[attr-defined]
)
from cmcourier.config.loader import Credential, Secrets
from cmcourier.config.schema import As400ConnectionConfig, ConnectionRef

pytestmark = pytest.mark.unit


@dataclass
class _StubConfig:
    """Stub minimal: ``_check_as400_connectivity`` sólo mira ``connection_refs()``."""

    refs: tuple[ConnectionRef, ...]

    def connection_refs(self) -> tuple[ConnectionRef, ...]:
        return self.refs


def _ref(alias: str, site: str, host: str = "as400.test") -> ConnectionRef:
    spec = As400ConnectionConfig(host=host, port=446, database="RVILIB")
    return ConnectionRef(alias=alias, kind="as400", spec=spec, site=site)


def _secrets(**aliases: tuple[str, str]) -> Secrets:
    creds = {"cmis": Credential("cmis", "cmis")}
    creds.update({alias: Credential(*pair) for alias, pair in aliases.items()})
    return Secrets(creds)


def _patch_source(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[Any]]]:
    captured: list[tuple[str, list[Any]]] = []
    fake_source = MagicMock()
    fake_source.query = MagicMock(side_effect=lambda sql, params: captured.append((sql, params)))
    fake_source.close = MagicMock()
    monkeypatch.setattr(doctor_module, "As400DataSource", lambda **kwargs: fake_source)
    return captured


class TestAs400ConnectivityQuery:
    """073: el health-check debe usar ``SELECT 1 FROM SYSIBM.SYSDUMMY1``,
    la pseudo-tabla canónica de DB2 / iSeries. ``SELECT 1`` solo (sin
    ``FROM``) es legal en MySQL/Postgres/SQL Server pero DB2 lo rechaza
    con sqlstate 42000 (syntax error).
    """

    def test_health_check_uses_sysdummy1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_source(monkeypatch)
        config = _StubConfig(refs=(_ref("as400", "indexing"),))
        result = _check_as400_connectivity(config, _secrets(as400=("dbuser", "dbpass")))  # type: ignore[arg-type]

        assert result.status == CheckStatus.PASS
        assert captured == [("SELECT 1 FROM SYSIBM.SYSDUMMY1", [])]

    def test_skips_when_no_as400_connection(self) -> None:
        # Sin conexiones AS400 el check debe skipear sin tocar pyodbc.
        result = _check_as400_connectivity(_StubConfig(refs=()), _secrets())  # type: ignore[arg-type]
        assert result.status == CheckStatus.SKIP

    def test_fails_when_credentials_missing(self) -> None:
        config = _StubConfig(refs=(_ref("as400", "indexing"),))
        result = _check_as400_connectivity(config, _secrets())  # type: ignore[arg-type]
        assert result.status == CheckStatus.FAIL
        assert "credentials" in result.message.lower()
        assert "AS400_USERNAME" in result.message


class TestAs400ConnectivityPerConnection:
    """129 E5 — se prueba CADA conexión as400 (por alias + sitio)."""

    def test_second_connection_without_credentials_fails_naming_site(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_source(monkeypatch)
        config = _StubConfig(
            refs=(_ref("rvi", "indexing"), _ref("personas", "metadata:clientes", host="h2"))
        )
        result = _check_as400_connectivity(config, _secrets(rvi=("u", "p")))  # type: ignore[arg-type]
        assert result.status == CheckStatus.FAIL
        assert "metadata:clientes" in result.message
        assert "PERSONAS_USERNAME" in result.message
        assert result.details["rvi"].startswith("PASS")
        assert result.details["personas"].startswith("FAIL")

    def test_same_alias_and_spec_probed_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_source(monkeypatch)
        config = _StubConfig(refs=(_ref("as400", "indexing"), _ref("as400", "tracking.as400_sync")))
        result = _check_as400_connectivity(config, _secrets(as400=("u", "p")))  # type: ignore[arg-type]
        assert result.status == CheckStatus.PASS
        assert len(captured) == 1
        assert "indexing" in result.details["as400"]
        assert "tracking.as400_sync" in result.details["as400"]
