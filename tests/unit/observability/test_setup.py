"""Tests unitarios para ``cmcourier.observability.setup.configure`` (041)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from cmcourier.config.schema import ObservabilityConfig
from cmcourier.observability.setup import configure


@pytest.fixture
def obs_config(tmp_path: Path) -> ObservabilityConfig:
    return ObservabilityConfig(
        enabled=True,
        log_dir=tmp_path / "logs",
        log_format="text",
        pipeline_metrics=True,
        network_metrics=True,
        rotation_mb=5,
        slow_op_threshold_ms=1000.0,
        slow_op_top_n=10,
    )


@pytest.fixture(autouse=True)
def _reset_cmcourier_logger() -> Iterator[None]:
    """Resetea cada `logger` ``cmcourier*`` antes y después del test.

    ``configure()`` setea ``propagate=False`` en ``cmcourier.metrics.pipeline
    / network / slow_ops`` (para que los `handler`s hijos no dupliquen
    emisiones). Sin resetear ese flag explícitamente después de cada
    test unitario, los tests de integración que corren después en la
    misma invocación de `pytest` no pueden capturar eventos de red vía
    ``caplog`` — `propagate` queda apagado y los eventos nunca llegan
    al logger root al que `caplog` se engancha. Antes del fix esto se
    manifestaba como fallas inestables en
    ``TestUploadPayloadTraceEvents`` cada vez que
    ``tests/unit/observability`` corría en la misma sesión.
    """
    targets = (
        "cmcourier",
        "cmcourier.metrics.pipeline",
        "cmcourier.metrics.network",
        "cmcourier.metrics.slow_ops",
        "cmcourier.metrics.reconcile",
    )
    for name in targets:
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            logger.removeHandler(h)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
    yield
    for name in targets:
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            logger.removeHandler(h)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def _stderr_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, RotatingFileHandler)
        and getattr(h, "stream", None) is sys.stderr
    ]


def _file_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]


def test_configure_default_attaches_stderr_and_file(obs_config: ObservabilityConfig) -> None:
    configure(obs_config, "INFO")
    root = logging.getLogger("cmcourier")
    assert len(_stderr_handlers(root)) == 1, "default mode must keep the stderr handler"
    assert len(_file_handlers(root)) == 1, "default mode must keep the rotating file handler"


def test_configure_tui_active_omits_stderr(obs_config: ObservabilityConfig) -> None:
    configure(obs_config, "INFO", tui_active=True)
    root = logging.getLogger("cmcourier")
    assert _stderr_handlers(root) == [], (
        "tui_active=True must NOT attach the stderr StreamHandler (would tear the TUI frame)"
    )
    assert len(_file_handlers(root)) == 1, (
        "tui_active=True still needs the rotating file handler so logs persist to disk"
    )


def test_configure_stderr_only_overrides_tui_active(obs_config: ObservabilityConfig) -> None:
    """El camino de fallo temprano de `doctor` pasa stderr_only=True; debe seguir imprimiendo."""
    configure(obs_config, "INFO", stderr_only=True, tui_active=True)
    root = logging.getLogger("cmcourier")
    assert len(_stderr_handlers(root)) == 1, (
        "stderr_only=True is the diagnostic escape hatch — it must always print to stderr"
    )
    assert _file_handlers(root) == [], "stderr_only=True must skip file handlers"


def test_configure_tui_active_with_disabled_observability_still_skips_stderr() -> None:
    """El gating de `tui_active` debe funcionar incluso sin `ObservabilityConfig`."""
    configure(None, "INFO", tui_active=True)
    root = logging.getLogger("cmcourier")
    assert _stderr_handlers(root) == []
    assert _file_handlers(root) == []


def test_configure_idempotent_replaces_handlers(obs_config: ObservabilityConfig) -> None:
    """Llamar `configure` con otro modo cambia el set de `handler`s sin leaks."""
    configure(obs_config, "INFO")
    configure(obs_config, "INFO", tui_active=True)
    root = logging.getLogger("cmcourier")
    assert _stderr_handlers(root) == []
    assert len(_file_handlers(root)) == 1, "exactly one file handler after re-configure"


def test_configure_attaches_reconcile_log(obs_config: ObservabilityConfig) -> None:
    """096: el logger cmcourier.metrics.reconcile escribe a reconcile-*.jsonl."""
    configure(obs_config, "INFO")
    reconcile_log = logging.getLogger("cmcourier.metrics.reconcile")
    file_handlers = _file_handlers(reconcile_log)
    assert len(file_handlers) == 1, "reconcile log must have a rotating file handler"
    assert reconcile_log.propagate is False
    reconcile_log.info("reconcile_pass", extra={"event": "reconcile_pass"})
    for h in file_handlers:
        h.flush()
    written = list((obs_config.log_dir).glob("reconcile-*.jsonl"))
    assert len(written) == 1
    assert "reconcile_pass" in written[0].read_text(encoding="utf-8")
