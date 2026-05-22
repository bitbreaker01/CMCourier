"""Tests del desglose de error por tipo en el tab UPLOAD (104, REQ-006)."""

from __future__ import annotations

from typing import Any

import pytest

from cmcourier.tui.data_provider import TUISnapshot
from cmcourier.tui.upload_tab import render_upload

pytestmark = pytest.mark.unit


def _snap(**overrides: Any) -> TUISnapshot:
    base: dict[str, Any] = {
        "pipeline": "rvabrep",
        "batch_id": "b1",
        "elapsed_s": 10.0,
        "throughput_docs_per_s": 5.0,
        "is_complete": False,
    }
    base.update(overrides)
    return TUISnapshot(**base)


def test_clean_run_shows_none_yet() -> None:
    out = render_upload(_snap())
    assert "ERRORS BY TYPE (0 total)" in out
    assert "(none yet)" in out


def test_breakdown_lists_categories_and_status() -> None:
    out = render_upload(
        _snap(
            failed_total=12,
            failures_by_type={"http_5xx": 10, "timeout": 2},
            failures_by_status={503: 8, 500: 2},
        )
    )
    assert "ERRORS BY TYPE (12 total)" in out
    assert "http_5xx" in out
    assert "timeout" in out
    assert "503×8" in out
    assert "500×2" in out


def test_zero_count_categories_are_omitted() -> None:
    out = render_upload(_snap(failed_total=3, failures_by_type={"timeout": 3}))
    assert "timeout" in out
    assert "http_4xx" not in out
