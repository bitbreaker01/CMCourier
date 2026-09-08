"""130 E5 — check ``mssql_connectivity`` del doctor: una fila por conexión
``mssql`` del registro, ``SELECT 1`` como probe, SKIP sin conexiones."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.cli import doctor as doctor_module
from cmcourier.cli.doctor import (
    _CHECK_GROUPS,  # type: ignore[attr-defined]
    CHECK_NAMES,
    CheckStatus,
    _check_as400_connectivity,  # type: ignore[attr-defined]
    _check_mssql_connectivity,  # type: ignore[attr-defined]
    check_connection,
    group_of,
)
from cmcourier.config.loader import Credential, Secrets
from cmcourier.config.schema import As400ConnectionConfig, ConnectionRef, MssqlConnectionConfig

pytestmark = pytest.mark.unit


@dataclass
class _StubConfig:
    refs: tuple[ConnectionRef, ...]

    def connection_refs(self) -> tuple[ConnectionRef, ...]:
        return self.refs

    @property
    def connections(self) -> dict[str, MssqlConnectionConfig]:
        """138: `check_connection` cae al registro si el alias no tiene sitio."""
        return {r.alias: r.spec for r in self.refs if isinstance(r.spec, MssqlConnectionConfig)}


def _mssql_ref(alias: str, site: str, host: str = "sql.test") -> ConnectionRef:
    spec = MssqlConnectionConfig(host=host, database="cmcourier")
    return ConnectionRef(alias=alias, kind="mssql", spec=spec, site=site)


def _as400_ref(alias: str, site: str) -> ConnectionRef:
    spec = As400ConnectionConfig(host="as400.test", port=446, database="RVILIB")
    return ConnectionRef(alias=alias, kind="as400", spec=spec, site=site)


def _secrets(**aliases: tuple[str, str]) -> Secrets:
    creds = {"cmis": Credential("cmis", "cmis")}
    creds.update({alias: Credential(*pair) for alias, pair in aliases.items()})
    return Secrets(creds)


def _patch_sources(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[str, list[Any]]]]:
    """Parchea AMBOS adapters en el doctor y captura qué SQL recibe cada uno."""
    captured: dict[str, list[tuple[str, list[Any]]]] = {"mssql": [], "as400": []}

    def _fake(kind: str) -> Any:
        def _build(**kwargs: Any) -> MagicMock:
            src = MagicMock()
            src.query = MagicMock(
                side_effect=lambda sql, params: captured[kind].append((sql, params))
            )
            src.close = MagicMock()
            return src

        return _build

    monkeypatch.setattr(doctor_module, "MssqlDataSource", _fake("mssql"))
    monkeypatch.setattr(doctor_module, "As400DataSource", _fake("as400"))
    return captured


class TestMssqlConnectivityCheck:
    def test_registered_in_check_names_and_connections_group(self) -> None:
        assert "mssql_connectivity" in CHECK_NAMES
        assert (
            CHECK_NAMES.index("mssql_connectivity") == CHECK_NAMES.index("as400_connectivity") + 1
        )
        assert "mssql_connectivity" in _CHECK_GROUPS["connections"]
        assert group_of("mssql_connectivity") == "connections"

    def test_skips_without_mssql_connections(self) -> None:
        config = _StubConfig(refs=(_as400_ref("as400", "indexing"),))
        result = _check_mssql_connectivity(config, _secrets(as400=("u", "p")))  # type: ignore[arg-type]
        assert result.status == CheckStatus.SKIP
        assert result.details["reason"] == "no mssql connections in config"

    def test_fails_naming_alias_env_vars(self) -> None:
        config = _StubConfig(refs=(_mssql_ref("clientes_sql", "metadata:clientes"),))
        result = _check_mssql_connectivity(config, _secrets())  # type: ignore[arg-type]
        assert result.status == CheckStatus.FAIL
        assert "CLIENTES_SQL_USERNAME" in result.message
        assert "metadata:clientes" in result.message

    def test_probes_with_select_1_and_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_sources(monkeypatch)
        config = _StubConfig(refs=(_mssql_ref("clientes_sql", "metadata:clientes"),))
        result = _check_mssql_connectivity(config, _secrets(clientes_sql=("u", "p")))  # type: ignore[arg-type]
        assert result.status == CheckStatus.PASS
        assert captured["mssql"] == [("SELECT 1", [])]
        assert captured["as400"] == []
        assert "clientes_sql@sql.test" in result.message

    def test_as400_check_ignores_mssql_refs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Las dos familias no se mezclan: cada check prueba sólo su kind."""
        captured = _patch_sources(monkeypatch)
        config = _StubConfig(
            refs=(_as400_ref("as400", "indexing"), _mssql_ref("clientes_sql", "metadata:clientes"))
        )
        secrets = _secrets(as400=("u", "p"), clientes_sql=("u", "p"))
        as400 = _check_as400_connectivity(config, secrets)  # type: ignore[arg-type]
        mssql = _check_mssql_connectivity(config, secrets)  # type: ignore[arg-type]
        assert as400.status == CheckStatus.PASS and mssql.status == CheckStatus.PASS
        assert captured["as400"] == [("SELECT 1 FROM SYSIBM.SYSDUMMY1", [])]
        assert captured["mssql"] == [("SELECT 1", [])]
        assert "clientes_sql" not in as400.details
        assert "as400" not in mssql.details

    def test_details_keep_one_row_per_distinct_inline_connection(self) -> None:
        """Antagonista M1: dos objetos inline distintos comparten el alias
        implícito `as400`; el drill-down de [4] no puede perder una fila."""
        config = _StubConfig(
            refs=(
                ConnectionRef("as400", "as400", As400ConnectionConfig(host="host-a"), "indexing"),
                ConnectionRef(
                    "as400", "as400", As400ConnectionConfig(host="host-b"), "metadata:clientes"
                ),
            )
        )
        result = _check_as400_connectivity(config, _secrets())  # type: ignore[arg-type]
        assert result.status == CheckStatus.FAIL
        assert sorted(result.details) == ["as400@host-a", "as400@host-b"]
        assert "indexing" in result.details["as400@host-a"]
        assert "metadata:clientes" in result.details["as400@host-b"]


class TestCheckConnection:
    """131 — chequeo puntual por ALIAS para el botón "probar" de la consola."""

    def test_probes_only_the_named_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_sources(monkeypatch)
        config = _StubConfig(
            refs=(_as400_ref("as400", "indexing"), _mssql_ref("clientes_sql", "metadata:clientes"))
        )
        secrets = _secrets(as400=("u", "p"), clientes_sql=("u", "p"))
        result = check_connection(config, secrets, "clientes_sql")  # type: ignore[arg-type]
        assert result.status == CheckStatus.PASS
        assert result.name == "connection:clientes_sql"
        assert "sql.test" in result.message
        assert captured["as400"] == []
        assert captured["mssql"] == [("SELECT 1", [])]

    def test_fails_naming_env_vars_when_credentials_missing(self) -> None:
        config = _StubConfig(refs=(_mssql_ref("clientes_sql", "metadata:clientes"),))
        result = check_connection(config, secrets=_secrets(), alias="clientes_sql")  # type: ignore[arg-type]
        assert result.status == CheckStatus.FAIL
        assert "CLIENTES_SQL_USERNAME" in result.message

    def test_declared_but_unused_alias_is_probed_from_the_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """138: una conexión recién creada desde [2] no tiene sitio todavía —
        el botón "probar" igual la prueba (el registro es la fuente)."""
        captured = _patch_sources(monkeypatch)
        config = _StubConfig(refs=(_mssql_ref("nueva", "metadata:x"),))
        monkeypatch.setattr(_StubConfig, "connection_refs", lambda self: ())
        result = check_connection(config, _secrets(nueva=("u", "p")), "nueva")  # type: ignore[arg-type]
        assert result.status == CheckStatus.PASS
        assert captured["mssql"] == [("SELECT 1", [])]

    def test_unknown_alias_fails_without_probing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_sources(monkeypatch)
        config = _StubConfig(refs=(_mssql_ref("clientes_sql", "metadata:clientes"),))
        result = check_connection(config, _secrets(), "fantasma")  # type: ignore[arg-type]
        assert result.status == CheckStatus.FAIL
        assert "fantasma" in result.message
        assert captured == {"mssql": [], "as400": []}
