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
    check_connection,
)
from cmcourier.config.loader import Credential, Secrets
from cmcourier.config.schema import As400ConnectionConfig, ConnectionRef
from cmcourier.domain.exceptions import IndexingError

pytestmark = pytest.mark.unit


@dataclass
class _StubConfig:
    """Stub minimal: ``_check_as400_connectivity`` sólo mira ``connection_refs()``."""

    refs: tuple[ConnectionRef, ...]

    def connection_refs(self) -> tuple[ConnectionRef, ...]:
        return self.refs


def _ref(
    alias: str,
    site: str,
    host: str = "as400.test",
    probe_sql: str | None = "SELECT 1 FROM RVILIB.RVABREP FETCH FIRST 1 ROW ONLY",
    probe_query: str | None = None,
) -> ConnectionRef:
    spec = As400ConnectionConfig(host=host, port=446, database="RVILIB", probe_query=probe_query)
    return ConnectionRef(alias=alias, kind="as400", spec=spec, site=site, probe_sql=probe_sql)


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
    """143: el health-check toca la tabla REAL del sitio (``probe_sql``
    derivada en ``connection_refs()``), nunca ``SYSIBM.SYSDUMMY1``: con
    SafeNet/i en el iSeries sólo hay permiso sobre objetos whitelisteados
    por perfil y la pseudo-tabla canónica se rechaza (``PWS9801``).
    """

    def test_health_check_uses_site_derived_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_source(monkeypatch)
        config = _StubConfig(refs=(_ref("as400", "indexing"),))
        result = _check_as400_connectivity(config, _secrets(as400=("dbuser", "dbpass")))  # type: ignore[arg-type]

        assert result.status == CheckStatus.PASS
        assert captured == [("SELECT 1 FROM RVILIB.RVABREP FETCH FIRST 1 ROW ONLY", [])]
        assert "SYSDUMMY1" not in result.details["as400"]

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


def _raising(exc: BaseException, cause: BaseException) -> Any:
    def _raise(*_: Any, **__: Any) -> None:
        raise exc from cause

    return _raise


class TestProbeFailureDetail:
    """Queja del operador: la tarjeta decía sólo ``AS400 query failed
    [sql_prefix=..., sqlstate='HY000']`` — HY000 es "error general" y el
    texto REAL del driver (``[IBM]... SQL0332 ...``) quedaba en el
    ``__cause__`` que nadie mostraba. Además "query failed" significa que
    el login YA fue aceptado: un fallo de la consulta de prueba no es un
    intento de sign-on y no puede gastar el contador de lockout."""

    def _config(self) -> _StubConfig:
        return _StubConfig(refs=(_ref("rvi", "indexing"),))

    def test_query_failure_shows_driver_message_and_marks_phase_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = MagicMock()
        src.query = _raising(
            IndexingError("AS400 query failed", sql_prefix="SELECT 1", sqlstate="HY000"),
            RuntimeError("('HY000', '[IBM][System i Access ODBC Driver]SQL0332 CCSID 65535')"),
        )
        monkeypatch.setattr(doctor_module, "As400DataSource", lambda **kwargs: src)

        result = check_connection(self._config(), _secrets(rvi=("u", "p")), "rvi")  # type: ignore[arg-type]

        assert result.status == CheckStatus.FAIL
        assert "SQL0332" in result.message
        assert "conectó" in result.message  # el login pasó: que el operador lo sepa
        assert result.details["phase"] == "query"
        src.close.assert_called_once()

    def test_connect_failure_marks_phase_connect_and_skips_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = MagicMock()
        src.ping = _raising(
            IndexingError("AS400 connection failed", host="h", sqlstate="28000"),
            RuntimeError("('28000', 'CWBSY0002 - Password for user U not valid')"),
        )
        monkeypatch.setattr(doctor_module, "As400DataSource", lambda **kwargs: src)

        result = check_connection(self._config(), _secrets(rvi=("u", "p")), "rvi")  # type: ignore[arg-type]

        assert result.status == CheckStatus.FAIL
        assert "CWBSY0002" in result.message
        assert result.details["phase"] == "connect"
        src.query.assert_not_called()
        src.close.assert_called_once()

    def test_pass_pings_before_probe_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        src = MagicMock()
        monkeypatch.setattr(doctor_module, "As400DataSource", lambda **kwargs: src)

        result = check_connection(self._config(), _secrets(rvi=("u", "p")), "rvi")  # type: ignore[arg-type]

        assert result.status == CheckStatus.PASS
        src.ping.assert_called_once()
        src.query.assert_called_once_with("SELECT 1 FROM RVILIB.RVABREP FETCH FIRST 1 ROW ONLY", [])
        assert "derivada de indexing" in result.message


class TestProbeQuerySelection:
    """143 — orden: ``probe_query`` explícita → derivada del sitio → sólo ping."""

    def _probe(self, monkeypatch: pytest.MonkeyPatch, ref: ConnectionRef) -> tuple[Any, Any]:
        src = MagicMock()
        monkeypatch.setattr(doctor_module, "As400DataSource", lambda **kwargs: src)
        config = _StubConfig(refs=(ref,))
        return src, check_connection(config, _secrets(rvi=("u", "p")), "rvi")  # type: ignore[arg-type]

    def test_explicit_probe_query_wins_over_derived(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ref = _ref("rvi", "indexing", probe_query="SELECT 1 FROM RVILIB.PERMITIDA")
        src, result = self._probe(monkeypatch, ref)
        assert result.status == CheckStatus.PASS
        src.query.assert_called_once_with("SELECT 1 FROM RVILIB.PERMITIDA", [])
        assert "probe_query" in result.message
        assert "RVILIB.PERMITIDA" in result.message

    def test_without_any_query_only_pings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Conexión recién creada desde [2]: sin sitio no hay tabla que derivar.
        src, result = self._probe(monkeypatch, _ref("rvi", "connections", probe_sql=None))
        assert result.status == CheckStatus.PASS
        src.ping.assert_called_once()
        src.query.assert_not_called()
        assert "sin tabla asignada" in result.message

    def test_query_failure_names_the_query_and_its_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = MagicMock()
        src.query = _raising(
            IndexingError("AS400 query failed", sql_prefix="SELECT 1", sqlstate="HY000"),
            RuntimeError("PWS9801 - Function rejected by user exit program SAFENET"),
        )
        monkeypatch.setattr(doctor_module, "As400DataSource", lambda **kwargs: src)
        config = _StubConfig(refs=(_ref("rvi", "metadata:clientes"),))
        result = check_connection(config, _secrets(rvi=("u", "p")), "rvi")  # type: ignore[arg-type]
        assert result.status == CheckStatus.FAIL
        assert "PWS9801" in result.message
        assert "derivada de metadata:clientes" in result.message
        assert result.details["phase"] == "query"
