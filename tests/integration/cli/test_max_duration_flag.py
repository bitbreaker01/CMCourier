"""Integration tests del flag ``--max-duration`` (103, REQ-001).

El disparo del deadline y el drain están cubiertos a nivel unitario en
``tests/unit/services/test_deadline.py``; acá se valida solo el wiring
del CLI: que la opción exista en los cuatro comandos ``run`` y que un
valor inválido se rechace temprano con código 2.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cmcourier.cli.app import main

pytestmark = pytest.mark.integration

_RUN_GROUPS = (
    "csv-trigger-pipeline",
    "rvabrep-pipeline",
    "local-scan-pipeline",
    "single-doc",
)


def test_max_duration_invalid_rejected_early(tmp_path: Path) -> None:
    """Un --max-duration mal formado sale con código 2 antes de cargar config."""
    dummy_config = tmp_path / "config.yaml"
    dummy_config.write_text("")
    result = CliRunner().invoke(
        main,
        [
            "rvabrep-pipeline",
            "run",
            "--config",
            str(dummy_config),
            "--max-duration",
            "garbage",
        ],
    )
    assert result.exit_code == 2
    assert "--max-duration" in result.stderr


def test_max_duration_zero_rejected(tmp_path: Path) -> None:
    """Una duración de cero no tiene sentido operativo: código 2."""
    dummy_config = tmp_path / "config.yaml"
    dummy_config.write_text("")
    result = CliRunner().invoke(
        main,
        [
            "rvabrep-pipeline",
            "run",
            "--config",
            str(dummy_config),
            "--max-duration",
            "0m",
        ],
    )
    assert result.exit_code == 2


def test_help_lists_max_duration() -> None:
    for group in _RUN_GROUPS:
        result = CliRunner().invoke(main, [group, "run", "--help"])
        assert result.exit_code == 0
        assert "--max-duration" in result.output, f"--max-duration missing on {group}"
