"""Tests del estado de sesión de la consola (123)."""

from __future__ import annotations

import pytest

from cmcourier.cli.console.state import AS400_MAX_TRIES, ConsoleState, SessionCredentials
from cmcourier.cli.doctor import CheckResult, CheckStatus, DoctorReport

pytestmark = pytest.mark.unit


def _report(*statuses: CheckStatus) -> DoctorReport:
    return DoctorReport(
        results=tuple(
            CheckResult(name=f"c{i}", status=s, message="m") for i, s in enumerate(statuses)
        ),
        elapsed_seconds=0.1,
    )


class TestSessionCredentials:
    def test_from_env_prefills(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CMIS_USERNAME", "admin ")
        monkeypatch.setenv("CMIS_PASSWORD", "s3cr3t")
        monkeypatch.delenv("AS400_USERNAME", raising=False)
        creds = SessionCredentials.from_env()
        assert creds.cmis_username == "admin"
        assert creds.cmis_complete()
        assert not creds.as400_complete()

    def test_to_secrets_maps_fields(self) -> None:
        creds = SessionCredentials("u", "p", "au", "ap")
        s = creds.to_secrets()
        assert (s.cmis_username, s.cmis_password, s.as400_username, s.as400_password) == (
            "u",
            "p",
            "au",
            "ap",
        )


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
        st = ConsoleState(creds=SessionCredentials())
        assert not st.as400_needs_lockout_confirm()
        st.record_conn_result("as400", ok=False, message="401")
        assert st.conn["as400"].attempts == 1
        st.record_conn_result("as400", ok=False, message="401")
        assert st.conn["as400"].attempts == AS400_MAX_TRIES - 1
        assert st.as400_needs_lockout_confirm()
        st.record_conn_result("as400", ok=True, message="ok")
        st.reset_as400_attempts()
        assert st.conn["as400"].attempts == 0

    def test_cmis_failures_do_not_count_attempts(self) -> None:
        st = ConsoleState(creds=SessionCredentials())
        st.record_conn_result("cmis", ok=False, message="401")
        assert st.conn["cmis"].attempts == 0


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

    def test_next_steps_respects_as400_requirement(self) -> None:
        st = ConsoleState(creds=SessionCredentials())
        st.record_conn_result("cmis", ok=True, message="ok")
        assert st.next_steps(as400_required=True)[0][0] is False
        assert st.next_steps(as400_required=False)[0][0] is True
