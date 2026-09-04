"""Tests unitarios para el rebobinado del stream en el retry de 401 (105).

Pre-105 el path del 401 en ``_post_with_retries`` hacía ``continue`` sin
incrementar ``real_attempts``, y el ``stream.seek(0)`` era condicional a
``real_attempts > 0``. Un 401 en el primer intento dejaba el file handle
en EOF: el reintento anunciaba el ``Content-Length`` completo pero el
file part rendía 0 bytes → el server esperaba el resto del body hasta el
read timeout (300 s por documento).

Estos tests validan que el body del reintento post-401 está completo y
que el 401 sigue sin consumir presupuesto de retries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.upload.cmis_uploader import (
    CmisConfig,
    CmisUploader,
)
from cmcourier.domain.models import StagedFile

pytestmark = pytest.mark.unit

_FILE_BYTES = b"FULL_BODY_BYTES_401"


def _make_uploader(*, retry_max_attempts: int = 3) -> CmisUploader:
    cfg = CmisConfig(
        base_url="http://cm.test/cmis",
        repo_id="repo",
        username="u",
        password="p",
        timeout_seconds=30.0,
        verify_ssl=False,
        max_bandwidth_mbps=0.0,
        retry_max_attempts=retry_max_attempts,
        retry_base_delay_s=0.001,
        pool_size=4,
        unmask_pii=False,
    )
    return CmisUploader(cfg)


def _make_staged_file(tmp_path: Path) -> StagedFile:
    p = tmp_path / "doc-401.pdf"
    p.write_bytes(_FILE_BYTES)
    return StagedFile(path=p, size_bytes=len(_FILE_BYTES), page_count=1)


def _fake_get(self: Any, url: str, **kwargs: Any) -> Any:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"repositoryId": "repo"}
    return r


def _fake_response(status_code: int, *, object_id: str = "cmis:objid-401") -> Any:
    r = MagicMock()
    r.status_code = status_code
    if status_code == 201:
        r.json.return_value = {"succinctProperties": {"cmis:objectId": object_id}}
        r.text = '{"succinctProperties":{"cmis:objectId":"' + object_id + '"}}'
    else:
        r.text = f"status {status_code}"
    return r


def _install_post_sequence(
    monkeypatch: pytest.MonkeyPatch, statuses: list[int]
) -> tuple[list[bytes], dict[str, int]]:
    """Mockea ``httpx.Client.post`` con una secuencia de status codes.

    Drena el ``content`` de cada POST (igual que httpx lo haría contra el
    socket) y captura el body transmitido — así el stream queda en EOF
    entre intentos, exactamente como en producción.
    """
    bodies_sent: list[bytes] = []
    call_count = {"n": 0}

    def fake_post(self: Any, url: str, **kwargs: Any) -> Any:
        idx = call_count["n"]
        call_count["n"] += 1
        content = kwargs.get("content")
        bodies_sent.append(b"".join(content) if content is not None else b"")
        status = statuses[idx] if idx < len(statuses) else 201
        return _fake_response(status)

    monkeypatch.setattr("httpx.Client.get", _fake_get)
    monkeypatch.setattr("httpx.Client.post", fake_post)
    return bodies_sent, call_count


class TestRetry401RewindsStream:
    """105: el reintento post-401 debe transmitir el body completo."""

    def test_401_on_first_attempt_resends_full_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # E1: 401 → 201. Pre-105 el segundo body salía sin el archivo
        # (stream en EOF) y el server quedaba esperando bytes.
        bodies_sent, call_count = _install_post_sequence(monkeypatch, [401, 201])

        uploader = _make_uploader()
        staged = _make_staged_file(tmp_path)
        result = uploader.upload(
            file=staged,
            folder_path="F",
            object_type_id="cmis:document",
            document_name="doc-401.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="b1",
        )

        assert call_count["n"] == 2, "expected exactly one auth retry"
        assert _FILE_BYTES in bodies_sent[0]
        assert _FILE_BYTES in bodies_sent[1], (
            "el body del reintento post-401 no contiene el archivo — "
            "el stream no se rebobinó antes de reconstruir el encoder"
        )
        assert result == "cmis:objid-401"

    def test_401_then_500_then_success_all_bodies_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # E2: 401 → 500 → 201. Los tres bodies completos; el presupuesto
        # de retries solo se consume con el 500.
        bodies_sent, call_count = _install_post_sequence(monkeypatch, [401, 500, 201])

        uploader = _make_uploader(retry_max_attempts=2)
        staged = _make_staged_file(tmp_path)
        uploader.upload(
            file=staged,
            folder_path="F",
            object_type_id="cmis:document",
            document_name="doc-401.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="b1",
        )

        assert call_count["n"] == 3
        for i, body in enumerate(bodies_sent):
            assert _FILE_BYTES in body, f"body del intento {i} incompleto"

    def test_content_length_matches_transmitted_body_after_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # El Content-Length anunciado en el reintento debe coincidir con
        # lo efectivamente transmitido — la discrepancia es lo que
        # producía el cuelgue de 300 s.
        headers_sent: list[dict[str, str]] = []
        bodies_sent: list[bytes] = []
        call_count = {"n": 0}

        def fake_post(self: Any, url: str, **kwargs: Any) -> Any:
            idx = call_count["n"]
            call_count["n"] += 1
            headers_sent.append(dict(kwargs.get("headers", {})))
            content = kwargs.get("content")
            bodies_sent.append(b"".join(content) if content is not None else b"")
            return _fake_response(401 if idx == 0 else 201)

        monkeypatch.setattr("httpx.Client.get", _fake_get)
        monkeypatch.setattr("httpx.Client.post", fake_post)

        uploader = _make_uploader()
        staged = _make_staged_file(tmp_path)
        uploader.upload(
            file=staged,
            folder_path="F",
            object_type_id="cmis:document",
            document_name="doc-401.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="b1",
        )

        announced = int(headers_sent[1]["Content-Length"])
        assert announced == len(bodies_sent[1]), (
            f"Content-Length anuncia {announced} bytes pero se transmitieron {len(bodies_sent[1])}"
        )
