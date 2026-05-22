"""Tests de ``cmcourier.services.duration.parse_duration`` (103, REQ-001)."""

from __future__ import annotations

import pytest

from cmcourier.services.duration import parse_duration

pytestmark = pytest.mark.unit


class TestParseDurationHappyPath:
    """REQ-001: parsea duraciones humanas a segundos."""

    def test_seconds(self) -> None:
        assert parse_duration("45s") == 45.0

    def test_minutes(self) -> None:
        assert parse_duration("30m") == 1800.0

    def test_hours(self) -> None:
        assert parse_duration("2h") == 7200.0

    def test_days(self) -> None:
        assert parse_duration("1d") == 86400.0

    def test_compound_hours_minutes(self) -> None:
        assert parse_duration("1h30m") == 5400.0

    def test_compound_all_units(self) -> None:
        assert parse_duration("1h30m15s") == 5415.0

    def test_bare_number_is_seconds(self) -> None:
        assert parse_duration("90") == 90.0

    def test_decimal_component(self) -> None:
        assert parse_duration("1.5h") == 5400.0

    def test_whitespace_tolerated(self) -> None:
        assert parse_duration("  1h 30m  ") == 5400.0

    @pytest.mark.parametrize("text", ["2H", "30M", "45S", "1D"])
    def test_case_insensitive_unit(self, text: str) -> None:
        assert parse_duration(text) == parse_duration(text.lower())


class TestParseDurationRejects:
    """REQ-001: entradas inválidas levantan ``ValueError``."""

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "abc",
            "-5m",
            "5x",
            "1.2.3h",
            "h",
            "5m3",
            "30 minutes",
        ],
    )
    def test_invalid_inputs_raise(self, bad: str) -> None:
        with pytest.raises(ValueError, match="invalid duration"):
            parse_duration(bad)

    @pytest.mark.parametrize("zero", ["0", "0s", "0m", "0h"])
    def test_zero_duration_rejected(self, zero: str) -> None:
        with pytest.raises(ValueError, match="positive"):
            parse_duration(zero)

    def test_error_message_quotes_input(self) -> None:
        with pytest.raises(ValueError, match=r"'totally not a duration'"):
            parse_duration("totally not a duration")
