"""144: ``cmcourier sync recover`` imprime el progreso en stderr.

Una línea por evento :class:`SyncProgress`; el reporte final sigue en
stdout (así ``cmcourier sync recover > plan.txt`` no se ensucia).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import cmcourier.cli.commands.sync as sync_cmd
from cmcourier.cli.commands.sync import sync_group
from cmcourier.services.recovery import RecoveryItem, RecoveryResult, SyncProgress

pytestmark = pytest.mark.unit


def _fake_recover(config: Any, secrets: Any, *, batch_id: Any, apply: bool, on_progress: Any):
    on_progress(SyncProgress("leyendo tracking", 0, 0))
    on_progress(SyncProgress("consultando NIARVILOG", 0, 3))
    on_progress(SyncProgress("consultando RVABREP", 0, 2))
    if apply:
        on_progress(SyncProgress("insertando", 0, 2))
        on_progress(SyncProgress("insertando", 2, 2))
    return RecoveryResult(recovered=["t1", "t2"], consistent=["t0"])


def _fake_recover_con_divergencia(
    config: Any, secrets: Any, *, batch_id: Any, apply: bool, on_progress: Any
):
    """151 REQ-003: una corrida con los cuatro grupos poblados."""
    return RecoveryResult(
        recovered=["t1", "t2"],
        updated=["t3"],
        consistent=["t0"],
        divergent=[RecoveryItem("t4", "objidn_mismatch: AS400 'cm-x' vs local 'cm-y'")],
        unrecoverable=[],
    )


class TestSyncRecoverProgress144:
    def test_progress_goes_to_stderr_and_report_to_stdout(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text("x: 1\n")
        with (
            patch.object(sync_cmd, "_load", return_value=(MagicMock(), MagicMock())),
            patch.object(sync_cmd, "sync_recover", side_effect=_fake_recover),
        ):
            result = CliRunner().invoke(sync_group, ["recover", "--config", str(cfg), "--apply"])

        assert result.exit_code == 0, result.output
        assert result.stderr.splitlines() == [
            "leyendo tracking 0/0",
            "consultando NIARVILOG 0/3",
            "consultando RVABREP 0/2",
            "insertando 0/2",
            "insertando 2/2",
        ]
        assert "sync recover [APPLY]: recuperadas=2" in result.stdout
        assert "insertando" not in result.stdout

    def test_report_names_the_three_groups_and_the_divergences(self, tmp_path: Path) -> None:
        """151 REQ-003: ``already_present`` escondía las filas
        desactualizadas detrás de un conteo que sonaba a éxito."""
        cfg = tmp_path / "c.yaml"
        cfg.write_text("x: 1\n")
        with (
            patch.object(sync_cmd, "_load", return_value=(MagicMock(), MagicMock())),
            patch.object(sync_cmd, "sync_recover", side_effect=_fake_recover_con_divergencia),
        ):
            result = CliRunner().invoke(sync_group, ["recover", "--config", str(cfg), "--apply"])

        assert result.exit_code == 0, result.output
        assert "already_present" not in result.output
        assert "actualizadas=1" in result.stdout
        assert "consistentes=1" in result.stdout
        assert "divergentes=1" in result.stdout
        # La divergencia se nombra: el operador tiene que poder resolverla.
        assert "objidn_mismatch" in result.output
        assert "t4" in result.output
