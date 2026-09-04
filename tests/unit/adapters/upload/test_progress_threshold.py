"""Tests del threshold de eventos de progreso (111).

Pre-111 ``_PROGRESS_THRESHOLD_BYTES`` era 1 MiB: un record de logging
(json.dumps + escritura a disco + dos handlers) por cada MiB
transmitido, desde los worker threads de S5. 111 lo sube a 8 MiB — 8×
menos eventos, mismas curvas en el chart de bandwidth (el completion
acredita el remanente).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.upload.cmis_uploader import (
    _PROGRESS_THRESHOLD_BYTES,
    CmisConfig,
    CmisUploader,
)
from cmcourier.domain.models import StagedFile

pytestmark = pytest.mark.unit

_MIB = 1_048_576


class _CaptureHandler(logging.Handler):
    """Handler directo sobre ``cmcourier.metrics.network`` — el logger
    es no-propagante por diseño (setup.py), así que ``caplog`` (que
    captura vía root) no ve sus records cuando otro test ya corrió
    ``configure()``."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def network_records() -> Iterator[list[logging.LogRecord]]:
    logger = logging.getLogger("cmcourier.metrics.network")
    handler = _CaptureHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler.records
    logger.removeHandler(handler)
    logger.setLevel(previous_level)


def _upload_file_of(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size_bytes: int) -> None:
    def fake_get(self: Any, url: str, **kwargs: Any) -> Any:
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"repositoryId": "repo"}
        return r

    def fake_post(self: Any, url: str, **kwargs: Any) -> Any:
        content = kwargs.get("content")
        if content is not None:
            for _ in content:  # drena: dispara los callbacks del monitor
                pass
        r = MagicMock()
        r.status_code = 201
        r.json.return_value = {"succinctProperties": {"cmis:objectId": "oid"}}
        r.text = "{}"
        return r

    monkeypatch.setattr("httpx.Client.get", fake_get)
    monkeypatch.setattr("httpx.Client.post", fake_post)

    cfg = CmisConfig(
        base_url="http://cm.test/cmis",
        repo_id="repo",
        username="u",
        password="p",
        timeout_seconds=30.0,
        verify_ssl=False,
        max_bandwidth_mbps=0.0,
        retry_max_attempts=1,
        retry_base_delay_s=0.01,
        pool_size=2,
        unmask_pii=False,
    )
    uploader = CmisUploader(cfg)
    p = tmp_path / "big.pdf"
    p.write_bytes(b"x" * size_bytes)
    staged = StagedFile(path=p, size_bytes=size_bytes, page_count=1)
    uploader.upload(
        file=staged,
        folder_path="F",
        object_type_id="cmis:document",
        document_name="big.pdf",
        mime_type="application/pdf",
        properties={},
        batch_id="b1",
    )


class TestProgressThreshold:
    def test_threshold_is_8_mib(self) -> None:
        assert _PROGRESS_THRESHOLD_BYTES == 8 * _MIB

    def test_17_mib_upload_emits_2_progress_events(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        network_records: list[logging.LogRecord],
    ) -> None:
        # E1: 17 MiB → eventos a los 8 y 16 MiB (pre-111 eran ~17).
        _upload_file_of(tmp_path, monkeypatch, 17 * _MIB)
        progress = [r for r in network_records if getattr(r, "kind", "") == "cmis_upload_progress"]
        assert len(progress) == 2, f"esperados 2 eventos de progreso, hubo {len(progress)}"

    def test_small_upload_emits_no_progress_events(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        network_records: list[logging.LogRecord],
    ) -> None:
        # Un doc chico no genera ruido de progreso; el completion
        # (cmis_upload) acredita sus bytes igual.
        _upload_file_of(tmp_path, monkeypatch, 2 * _MIB)
        progress = [r for r in network_records if getattr(r, "kind", "") == "cmis_upload_progress"]
        assert progress == []
        completions = [r for r in network_records if getattr(r, "kind", "") == "cmis_upload"]
        assert len(completions) == 1
