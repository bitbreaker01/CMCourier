"""Tests del modelo draft/applied de overrides (124, C4)."""

from __future__ import annotations

import pytest

from cmcourier.cli.console.overrides import OverrideError, SessionOverrides
from cmcourier.cli.console.state import ConsoleState, SessionCredentials

pytestmark = pytest.mark.unit


class TestValidation:
    def test_valid_ranges_pass(self) -> None:
        ov = SessionOverrides(mode="batched", workers=64, prep_workers=1, bucket_size=10)
        assert ov.validated() is ov

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"workers": 0},
            {"workers": 65},
            {"prep_workers": 33},
            {"bucket_size": 5},
            {"mode": "turbo"},
            {"max_bandwidth_mbps": -1},
        ],
    )
    def test_out_of_range_raises(self, kwargs: dict) -> None:
        with pytest.raises(OverrideError):
            SessionOverrides(**kwargs).validated()


class TestPromotion:
    def test_promote_sets_applied_and_stales_doctor(self) -> None:
        from cmcourier.cli.doctor import CheckResult, CheckStatus, DoctorReport

        st = ConsoleState(creds=SessionCredentials())
        st.set_doctor_report(
            DoctorReport(
                results=(CheckResult(name="a", status=CheckStatus.PASS, message="ok"),),
                elapsed_seconds=0.1,
            ),
            group="all",
        )
        st.promote_overrides(SessionOverrides(workers=12))
        assert st.overrides.workers == 12
        assert st.doctor_stale

    def test_promote_invalid_keeps_previous_applied(self) -> None:
        st = ConsoleState(creds=SessionCredentials())
        st.promote_overrides(SessionOverrides(workers=12))
        with pytest.raises(OverrideError):
            st.promote_overrides(SessionOverrides(workers=999))
        assert st.overrides.workers == 12  # lo aplicado no se pisó


class TestSerialization:
    def test_summary_and_json_only_set_fields(self) -> None:
        ov = SessionOverrides(workers=8, unmask_pii=True)
        assert "workers=8" in ov.summary()
        assert "unmask_pii=True" in ov.summary()
        assert ov.to_json() == '{"workers": 8, "unmask_pii": true}'
        assert SessionOverrides().summary() == "ninguno"
        assert SessionOverrides().is_empty()


class TestTriggerOverride:
    """127 / E1 + E5: el trigger es un override más — pero se elige en [5]."""

    def test_apply_overrides_replaces_trigger_and_keeps_yaml(self, tmp_path) -> None:
        from cmcourier.cli.console.overrides import apply_overrides
        from cmcourier.config.schema import RvabrepFiltersModel, RvabrepTriggerConfig
        from tests.unit.cli.console.test_console_app import _make_config

        config, _ = _make_config(tmp_path)
        trig = RvabrepTriggerConfig(
            kind="rvabrep", filters=RvabrepFiltersModel(systems=["1"], document_types=[])
        )
        ov = SessionOverrides(trigger=trig)
        eff = apply_overrides(config, ov)
        assert eff.trigger.kind == "rvabrep"
        assert config.trigger.kind == "csv"  # el YAML no se toca
        assert not ov.is_empty()
        assert "pipeline=rvabrep" in ov.summary()
        assert '"trigger": {' in ov.to_json() and '"kind": "rvabrep"' in ov.to_json()

    def test_cleared_keeps_trigger(self) -> None:
        from cmcourier.config.schema import SingleDocTriggerConfig

        ov = SessionOverrides(workers=8, trigger=SingleDocTriggerConfig(kind="single_doc"))
        cleared = ov.cleared()
        assert cleared.workers is None
        assert cleared.trigger is not None and cleared.trigger.kind == "single_doc"
