"""Tests del estado de sesión de la consola (123) — por alias desde 131."""

from __future__ import annotations

import pytest

from cmcourier.cli.console.state import (
    AS400_MAX_TRIES,
    ConnInfo,
    ConsoleState,
    SessionCredentials,
    connection_infos,
)
from cmcourier.cli.doctor import CheckResult, CheckStatus, DoctorReport
from cmcourier.config.loader import Credential
from cmcourier.config.schema import As400ConnectionConfig, ConnectionRef, MssqlConnectionConfig

pytestmark = pytest.mark.unit


def _report(*statuses: CheckStatus) -> DoctorReport:
    return DoctorReport(
        results=tuple(
            CheckResult(name=f"c{i}", status=s, message="m") for i, s in enumerate(statuses)
        ),
        elapsed_seconds=0.1,
    )


def _as400_ref(alias: str, site: str, host: str = "as400.test") -> ConnectionRef:
    spec = As400ConnectionConfig(host=host, port=446, database="RVILIB")
    return ConnectionRef(alias=alias, kind="as400", spec=spec, site=site)


def _mssql_ref(alias: str, site: str, host: str = "127.0.0.1") -> ConnectionRef:
    spec = MssqlConnectionConfig(host=host, database="cmcourier")
    return ConnectionRef(alias=alias, kind="mssql", spec=spec, site=site)


class _Cfg:
    connections: dict[str, object] = {}  # 138: sin conexiones declaradas sin uso

    def __init__(self, *refs: ConnectionRef) -> None:
        self._refs = refs

    def connection_refs(self) -> tuple[ConnectionRef, ...]:
        return self._refs


class TestSessionCredentials:
    def test_from_env_prefills_cmis_and_requested_aliases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """131 E4: ``from_env(aliases)`` lee ``<ALIAS>_*`` de cada alias pedido."""
        monkeypatch.setenv("CMIS_USERNAME", "admin ")
        monkeypatch.setenv("CMIS_PASSWORD", "s3cr3t")
        monkeypatch.setenv("CLIENTES_SQL_USERNAME", "sa")
        monkeypatch.setenv("CLIENTES_SQL_PASSWORD", "pw")
        monkeypatch.delenv("AS400_USERNAME", raising=False)
        monkeypatch.delenv("AS400_PASSWORD", raising=False)
        creds = SessionCredentials.from_env(["clientes_sql", "as400"])
        assert creds.get("cmis") == Credential("admin", "s3cr3t")
        assert creds.complete("cmis")
        assert creds.complete("clientes_sql")
        assert not creds.complete("as400")
        assert creds.to_secrets().get("clientes_sql") == Credential("sa", "pw")

    def test_set_get_and_unknown_alias_is_empty(self) -> None:
        creds = SessionCredentials()
        assert creds.get("nadie") == Credential("", "")
        assert not creds.complete("nadie")
        creds.set("rvi", "u", "p")
        assert creds.complete("rvi")
        assert creds.to_secrets().require("rvi") == Credential("u", "p")

    def test_to_secrets_always_carries_cmis(self) -> None:
        secrets = SessionCredentials().to_secrets()
        assert secrets.cmis == Credential("", "")

    def test_set_strips_both_halves_like_load_secrets(self) -> None:
        """Antagonista M4: la CLI (`load_secrets`) strippea usuario Y
        contraseña; la consola debe mandar la MISMA credencial al AS400 —
        una divergencia gasta intentos de lockout."""
        creds = SessionCredentials()
        creds.set("rvi", " u ", " p ")
        assert creds.get("rvi") == Credential("u", "p")


class TestConnectionInfos:
    def test_dedupes_by_alias_and_collects_sites(self) -> None:
        """131 E2: una sola tarjeta `as400` con subtítulo `indexing · tracking`."""
        cfg = _Cfg(_as400_ref("as400", "indexing"), _as400_ref("as400", "tracking.as400_sync"))
        infos = connection_infos(cfg)  # type: ignore[arg-type]
        assert infos == [
            ConnInfo(alias="as400", kind="as400", host="as400.test", sites=("indexing", "tracking"))
        ]

    def test_preserves_order_and_kinds(self) -> None:
        cfg = _Cfg(_as400_ref("rvi", "indexing"), _mssql_ref("clientes_sql", "metadata:clientes"))
        infos = connection_infos(cfg)  # type: ignore[arg-type]
        assert [(i.alias, i.kind, i.host) for i in infos] == [
            ("rvi", "as400", "as400.test"),
            ("clientes_sql", "mssql", "127.0.0.1"),
        ]
        assert infos[1].sites == ("metadata:clientes",)
        assert infos[1].title == "clientes_sql · mssql · 127.0.0.1"


class TestForConfig:
    def test_conn_map_is_cmis_plus_aliases(self) -> None:
        cfg = _Cfg(_mssql_ref("clientes_sql", "metadata:clientes"))
        st = ConsoleState.for_config(cfg, creds=SessionCredentials())  # type: ignore[arg-type]
        assert list(st.conn) == ["cmis", "clientes_sql"]
        assert st.conn["clientes_sql"].kind == "mssql"
        assert st.conn["cmis"].kind == "cmis"

    def test_rebuild_keeps_surviving_states(self) -> None:
        """127 puede cambiar las conexiones por override: las tarjetas se recomponen
        sin perder lo ya probado."""
        st = ConsoleState.for_config(
            _Cfg(_as400_ref("rvi", "indexing")),  # type: ignore[arg-type]
            creds=SessionCredentials(),
        )
        st.record_conn_result("rvi", ok=True, message="ok")
        changed = st.rebuild_conn(
            _Cfg(
                _mssql_ref("clientes_sql", "metadata:clientes"),
                _as400_ref("rvi", "tracking.as400_sync"),
            )
        )  # type: ignore[arg-type]
        assert changed
        assert list(st.conn) == ["cmis", "clientes_sql", "rvi"]
        assert st.conn["rvi"].status == "ok"
        assert not st.rebuild_conn(
            _Cfg(_mssql_ref("clientes_sql", "metadata:clientes"), _as400_ref("rvi", "indexing"))
        )  # type: ignore[arg-type]

    def test_rebuild_prefills_new_aliases_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Antagonista M5: un alias que aparece tras el rebuild recibe el
        prefill de `<ALIAS>_*` igual que los del arranque; uno ya tipeado no
        se pisa."""
        monkeypatch.setenv("CLIENTES_SQL_USERNAME", "sa")
        monkeypatch.setenv("CLIENTES_SQL_PASSWORD", "pw")
        monkeypatch.setenv("RVI_USERNAME", "env-user")
        st = ConsoleState.for_config(
            _Cfg(_as400_ref("rvi", "indexing")),  # type: ignore[arg-type]
            creds=SessionCredentials(),
        )
        st.creds.set("rvi", "typed", "typed")
        st.rebuild_conn(
            _Cfg(_mssql_ref("clientes_sql", "metadata:clientes"), _as400_ref("rvi", "indexing"))
        )  # type: ignore[arg-type]
        assert st.creds.get("clientes_sql") == Credential("sa", "pw")
        assert st.creds.get("rvi") == Credential("typed", "typed")

    def test_vanished_alias_is_a_noop_everywhere(self) -> None:
        """Antagonista I2: el callback del worker puede llegar DESPUÉS de que
        la tarjeta desapareció — ningún método debe tirar KeyError."""
        st = ConsoleState.for_config(
            _Cfg(_as400_ref("rvi", "indexing")),  # type: ignore[arg-type]
            creds=SessionCredentials(),
        )
        st.rebuild_conn(_Cfg(_mssql_ref("otro", "metadata:x")))  # type: ignore[arg-type]
        st.invalidate_conn("rvi")
        st.record_conn_result("rvi", ok=True, message="tarde")
        st.reset_attempts("rvi")
        assert st.as400_needs_lockout_confirm("rvi") is False
        assert list(st.conn) == ["cmis", "otro"]


class TestConnLifecycle:
    def test_edit_invalidates_and_stales_doctor(self) -> None:
        st = ConsoleState(creds=SessionCredentials())
        st.record_conn_result("cmis", ok=True, message="ok")
        st.set_doctor_report(_report(CheckStatus.PASS), group="all")
        assert st.doctor_verdict() == "aprobado"

        st.invalidate_conn("cmis")
        assert st.conn["cmis"].status == "idle"
        assert st.doctor_stale
        assert st.doctor_verdict() == "desactualizado"

    def test_as400_attempts_and_lockout_gate(self) -> None:
        st = ConsoleState.for_config(
            _Cfg(_as400_ref("rvi", "indexing")), creds=SessionCredentials()
        )  # type: ignore[arg-type]
        assert not st.as400_needs_lockout_confirm("rvi")
        st.record_conn_result("rvi", ok=False, message="401")
        assert st.conn["rvi"].attempts == 1
        st.record_conn_result("rvi", ok=False, message="401")
        assert st.conn["rvi"].attempts == AS400_MAX_TRIES - 1
        assert st.as400_needs_lockout_confirm("rvi")
        st.record_conn_result("rvi", ok=True, message="ok")
        st.reset_attempts("rvi")
        assert st.conn["rvi"].attempts == 0

    def test_only_as400_kind_counts_attempts(self) -> None:
        """131 E2: cmis y mssql NUNCA acumulan intentos ni piden confirmación."""
        st = ConsoleState.for_config(
            _Cfg(_mssql_ref("clientes_sql", "metadata:clientes")), creds=SessionCredentials()
        )  # type: ignore[arg-type]
        for _ in range(3):
            st.record_conn_result("cmis", ok=False, message="401")
            st.record_conn_result("clientes_sql", ok=False, message="18456")
        assert st.conn["cmis"].attempts == 0
        assert st.conn["clientes_sql"].attempts == 0
        assert not st.as400_needs_lockout_confirm("clientes_sql")

    def test_query_phase_failure_does_not_spend_lockout_attempt(self) -> None:
        """Queja del operador: el login al iSeries fue ACEPTADO y falló la
        consulta de prueba — eso no es un sign-on inválido y no puede acercar
        al perfil al lockout (QMAXSIGN sólo cuenta sign-ons fallidos)."""
        st = ConsoleState.for_config(
            _Cfg(_as400_ref("rvi", "indexing")), creds=SessionCredentials()
        )  # type: ignore[arg-type]
        for _ in range(3):
            st.record_conn_result("rvi", ok=False, message="HY000", counts_attempt=False)
        assert st.conn["rvi"].status == "err"
        assert st.conn["rvi"].message == "HY000"
        assert st.conn["rvi"].attempts == 0
        assert not st.as400_needs_lockout_confirm("rvi")


class TestDoctorVerdict:
    def test_verdict_progression(self) -> None:
        st = ConsoleState(creds=SessionCredentials())
        assert st.doctor_verdict() == "sin correr"
        st.set_doctor_report(_report(CheckStatus.PASS, CheckStatus.WARN), group="all")
        assert st.doctor_verdict() == "aprobado"
        st.set_doctor_report(_report(CheckStatus.FAIL), group="all")
        assert st.doctor_verdict() == "con fallas"
        st.set_doctor_report(_report(CheckStatus.PASS), group="connections")
        assert st.doctor_verdict() == "parcial (connections)"

    def test_next_steps_requires_every_alias(self) -> None:
        """131 E1: con `clientes_sql` requerida, cmis sola no tilda el primer paso."""
        st = ConsoleState.for_config(
            _Cfg(_mssql_ref("clientes_sql", "metadata:clientes")), creds=SessionCredentials()
        )  # type: ignore[arg-type]
        st.record_conn_result("cmis", ok=True, message="ok")
        assert st.next_steps(required=("clientes_sql",))[0][0] is False
        assert st.next_steps(required=())[0][0] is True
        st.record_conn_result("clientes_sql", ok=True, message="ok")
        assert st.creds_ready(required=("clientes_sql",))
