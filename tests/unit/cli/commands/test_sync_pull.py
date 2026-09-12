"""151 — ``cmcourier sync pull`` y el ``sync status`` que ya no miente.

``sync pull`` es la dirección AS400 → local: dry-run por default,
``--apply`` para escribir, espejo de ``sync recover``. ``sync status``
reporta las divergencias de verdad (REQ-005): antes decía que las
reportaba y sólo limpiaba los ``'I'`` vencidos.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import cmcourier.cli.commands.sync as sync_cmd
from cmcourier.cli.commands.sync import sync_group
from cmcourier.cli.sync_ops import StatusResult
from cmcourier.services.pull import PullItem, PullResult
from cmcourier.services.sync_progress import SyncProgress

pytestmark = pytest.mark.unit


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    path = tmp_path / "c.yaml"
    path.write_text("x: 1\n")
    return path


def _invoke(args: list[str], **patches: Any):
    with ExitStack() as stack:
        loaded = (MagicMock(), MagicMock())
        stack.enter_context(patch.object(sync_cmd, "_load", return_value=loaded))
        for name, fn in patches.items():
            stack.enter_context(patch.object(sync_cmd, name, side_effect=fn))
        return CliRunner().invoke(sync_group, args)


class TestSyncPull151:
    def test_dry_run_por_default(self, cfg: Path) -> None:
        seen: list[bool] = []

        def fake_pull(config: Any, secrets: Any, *, apply: bool, on_progress: Any = None):
            seen.append(apply)
            return PullResult(scanned=7, imported_uploaded=2, imported_failed=1, consistent=4)

        result = _invoke(["pull", "--config", str(cfg)], sync_pull=fake_pull)

        assert result.exit_code == 0, result.output
        assert seen == [False]
        assert "sync pull [DRY-RUN]" in result.stdout
        assert "escaneadas=7" in result.stdout
        assert "importadas_ok=2" in result.stdout
        assert "importadas_fallidas=1" in result.stdout
        assert "consistentes=4" in result.stdout
        assert "--apply" in result.stdout  # le dice al operador cómo ejecutarlo

    def test_apply_escribe(self, cfg: Path) -> None:
        seen: list[bool] = []

        def fake_pull(config: Any, secrets: Any, *, apply: bool, on_progress: Any = None):
            seen.append(apply)
            return PullResult(scanned=1, imported_uploaded=1)

        result = _invoke(["pull", "--config", str(cfg), "--apply"], sync_pull=fake_pull)

        assert result.exit_code == 0, result.output
        assert seen == [True]
        assert "sync pull [APPLY]" in result.stdout

    def test_las_divergencias_se_nombran_y_no_se_tocan(self, cfg: Path) -> None:
        def fake_pull(config: Any, secrets: Any, *, apply: bool, on_progress: Any = None):
            return PullResult(
                scanned=2,
                consistent=1,
                divergent=[PullItem("0000001", "AS400 STSCOD='F' vs local S5_DONE")],
            )

        result = _invoke(["pull", "--config", str(cfg), "--apply"], sync_pull=fake_pull)

        assert result.exit_code == 0, result.output
        assert "divergentes=1" in result.stdout
        assert "0000001" in result.output
        assert "sync resolve" in result.output

    def test_el_progreso_va_a_stderr(self, cfg: Path) -> None:
        """Igual que ``recover``: stdout queda limpio para el reporte."""

        def fake_pull(config: Any, secrets: Any, *, apply: bool, on_progress: Any = None):
            on_progress(SyncProgress("listando NIARVILOG", 0, 0))
            on_progress(SyncProgress("listando NIARVILOG", 500, 0))
            return PullResult(scanned=500)

        result = _invoke(["pull", "--config", str(cfg)], sync_pull=fake_pull)

        assert result.exit_code == 0, result.output
        assert "listando NIARVILOG 500/0" in result.stderr
        assert "listando" not in result.stdout


class TestSyncStatus151:
    def test_reporta_las_divergencias(self, cfg: Path) -> None:
        def fake_status(config: Any, secrets: Any, *, on_progress: Any = None):
            return StatusResult(
                stale_cleaned=2,
                scanned=10,
                importable=3,
                divergences=[PullItem("0000009", "AS400 STSCOD='O' vs local S5_FAILED")],
            )

        result = _invoke(["status", "--config", str(cfg)], sync_status=fake_status)

        assert result.exit_code == 0, result.output
        assert "stale_cleaned=2" in result.stdout
        assert "divergentes=1" in result.stdout
        assert "0000009" in result.output
        assert "sync pull" in result.output  # 3 importables: hay que traerlos

    def test_sin_divergencias_lo_dice(self, cfg: Path) -> None:
        def fake_status(config: Any, secrets: Any, *, on_progress: Any = None):
            return StatusResult(stale_cleaned=0, scanned=10, importable=0, divergences=[])

        result = _invoke(["status", "--config", str(cfg)], sync_status=fake_status)

        assert result.exit_code == 0, result.output
        assert "divergentes=0" in result.stdout
        assert "escaneadas=10" in result.stdout
