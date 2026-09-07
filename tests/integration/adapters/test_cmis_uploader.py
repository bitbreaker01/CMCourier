"""Tests de integración para :class:`CmisUploader`.

Ejercita el adapter end-to-end contra la librería real ``httpx``, con la
red `stubeada` por la librería ``respx`` (Principio VI de la Constitución:
sin `mockear` los internos de ``httpx`` — solo la red). 060 migró de
``responses`` (específico de requests) a ``respx``.

Los tests de la política de retry hacen `monkey-patch` de ``time.sleep``
dentro del namespace del módulo cmis_uploader, así los retries no esperan
de verdad. El test del limitador de ancho de banda usa el ``time.sleep``
real porque hace asserts sobre el tiempo transcurrido.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from cmcourier.adapters.upload.cmis_uploader import (
    BandwidthLimiter,
    CmisConfig,
    CmisUploader,
    TokenBucket,
)
from cmcourier.domain.exceptions import (
    CMISClientError,
    CMISServerError,
    RetriesExhaustedError,
)
from cmcourier.domain.models import StagedFile
from cmcourier.observability.metrics import MetricsRecorder

pytestmark = pytest.mark.integration

_BASE_URL = "http://cmis.example.test:9080/opencmcmis/browser"
_REPO_ID = "$x!testrepo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> CmisConfig:
    defaults: dict[str, Any] = {
        "base_url": _BASE_URL,
        "repo_id": _REPO_ID,
        "username": "tester",
        "password": "secret-not-real",
        "timeout_seconds": 5.0,
        "verify_ssl": False,
        "max_bandwidth_mbps": 0.0,
        "retry_max_attempts": 3,
        "retry_base_delay_s": 0.0,
    }
    defaults.update(overrides)
    return CmisConfig(**defaults)


def _make_staged(tmp_path: Path, *, size_bytes: int = 1024) -> StagedFile:
    """Escribe un PDF sintético y devuelve un :class:`StagedFile`."""
    path = tmp_path / "TXN0000001.pdf"
    body = b"%PDF-1.4\n" + (b"x" * max(0, size_bytes - 9))
    path.write_bytes(body)
    return StagedFile(path=path, size_bytes=path.stat().st_size, page_count=1)


def _repo_info_url() -> str:
    return f"{_BASE_URL}/{_REPO_ID}"


def _root_url(folder_path: str = "") -> str:
    suffix = f"/{folder_path}" if folder_path else ""
    return f"{_BASE_URL}/{_REPO_ID}/root{suffix}"


def _stub_warmup(router: respx.MockRouter) -> None:
    """Registra una respuesta exitosa de repositoryInfo."""
    router.get(_repo_info_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "repositoryId": _REPO_ID,
                "productName": "IBM Content Manager",
                "productVersion": "8.7",
                "vendorName": "IBM",
            },
        )
    )


def _stub_warmup_alfresco_style(router: respx.MockRouter) -> None:
    """Igual que ``_stub_warmup`` pero matchea la URL de Alfresco (sin segmento de repo_id)."""
    router.get(_BASE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "repositoryId": "-default-",
                "productName": "Alfresco Community",
                "productVersion": "23.4.1",
                "vendorName": "Alfresco",
            },
        )
    )


def _skip_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cmcourier.adapters.upload.cmis_uploader.time.sleep",
        lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Grupo 1 — CmisConfig
# ---------------------------------------------------------------------------


class TestCmisConfig:
    def test_default_field_values(self) -> None:
        cfg = CmisConfig(
            base_url=_BASE_URL,
            repo_id=_REPO_ID,
            username="u",
            password="p",
        )
        assert cfg.timeout_seconds == 300.0
        assert cfg.verify_ssl is False
        assert cfg.max_bandwidth_mbps == 0.0
        assert cfg.retry_max_attempts == 3
        assert cfg.retry_base_delay_s == 2.0

    def test_config_is_frozen(self) -> None:
        cfg = _make_config()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.username = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Grupo 2 — Warmup
# ---------------------------------------------------------------------------


class TestWarmup:
    def test_construction_makes_no_http_call(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            CmisUploader(_make_config())
            assert len(mock.calls) == 0

    @respx.mock
    def test_warmup_runs_on_first_state_change(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        uploader = CmisUploader(_make_config())
        uploader.test_connection()
        assert len(respx_mock.calls) == 1
        assert "cmisselector=repositoryInfo" in str(respx_mock.calls[0].request.url)

    @respx.mock
    def test_warmup_5xx_raises_server_error(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(_repo_info_url()).mock(
            return_value=httpx.Response(503, json={"error": "boom"})
        )
        uploader = CmisUploader(_make_config())
        with pytest.raises(CMISServerError) as ei:
            uploader.test_connection()
        assert ei.value.status_code == 503


# ---------------------------------------------------------------------------
# Grupo 2b — Dimensionamiento del pool de conexiones + warm-up agresivo (038)
# ---------------------------------------------------------------------------


class TestConnectionPoolSizing:
    def test_default_pool_size_is_ten(self) -> None:
        cfg = CmisConfig(base_url=_BASE_URL, repo_id=_REPO_ID, username="u", password="p")
        assert cfg.pool_size == 10

    def test_client_built_with_configured_limits(self) -> None:
        # 060: httpx reemplaza al HTTPAdapter de requests. El pool_size fluye
        # hacia httpx.Limits: verificamos que tanto max_connections como
        # max_keepalive_connections lleven el valor configurado (si no, los
        # `workers` concurrentes pelean por el pool).
        uploader = CmisUploader(_make_config(pool_size=32))
        pool = uploader._client._transport._pool  # noqa: SLF001
        assert pool._max_connections == 32  # noqa: SLF001
        assert pool._max_keepalive_connections == 32  # noqa: SLF001


class TestWarmConnectionPool:
    @respx.mock
    def test_warm_n_connections_fires_n_requests(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        uploader = CmisUploader(_make_config(pool_size=8))
        succeeded = uploader.warm_connection_pool(8)
        assert succeeded == 8
        # repositoryInfo recibió 8 hits.
        info_calls = [
            c for c in respx_mock.calls if "cmisselector=repositoryInfo" in str(c.request.url)
        ]
        assert len(info_calls) == 8

    def test_warm_zero_is_noop(self) -> None:
        uploader = CmisUploader(_make_config())
        # Sin `mocks` HTTP registrados → daría error si saliera un request.
        assert uploader.warm_connection_pool(0) == 0
        assert uploader.warm_connection_pool(-3) == 0

    @respx.mock
    def test_warm_swallows_individual_failures(self, respx_mock: respx.MockRouter) -> None:
        # La primera llamada anda; las siguientes son 503 así que casi todas fallan.
        respx_mock.get(_repo_info_url()).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "repositoryId": _REPO_ID,
                        "productName": "IBM",
                        "productVersion": "8.7",
                        "vendorName": "IBM",
                    },
                ),
                httpx.Response(503, json={"error": "boom"}),
                httpx.Response(503, json={"error": "boom"}),
                httpx.Response(503, json={"error": "boom"}),
            ]
        )
        uploader = CmisUploader(_make_config(pool_size=4))
        # NO debería levantar — las fallas solo loguean.
        succeeded = uploader.warm_connection_pool(4)
        # El orden de finalización es no determinístico así que solo
        # se verifica que esté dentro de [0, 4].
        assert 0 <= succeeded <= 4


# ---------------------------------------------------------------------------
# Grupo 3 — test_connection
# ---------------------------------------------------------------------------


class TestTestConnection:
    @respx.mock
    def test_parses_repository_info(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        uploader = CmisUploader(_make_config())
        info = uploader.test_connection()
        assert info["repository_id"] == _REPO_ID
        assert info["product_name"] == "IBM Content Manager"
        assert info["product_version"] == "8.7"
        assert info["vendor_name"] == "IBM"

    @respx.mock
    def test_missing_keys_become_empty_string(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(_repo_info_url()).mock(return_value=httpx.Response(200, json={}))
        uploader = CmisUploader(_make_config())
        info = uploader.test_connection()
        assert info["repository_id"] == ""
        assert info["product_name"] == ""

    @respx.mock
    def test_4xx_raises_client_error(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(_repo_info_url()).mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        uploader = CmisUploader(_make_config())
        with pytest.raises(CMISClientError) as ei:
            uploader.test_connection()
        assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# Grupo 4 — verify_folder_exists
# ---------------------------------------------------------------------------


class TestVerifyFolderExists:
    @respx.mock
    def test_returns_true_for_existing_folder(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        respx_mock.get(_root_url("EXISTS")).mock(
            return_value=httpx.Response(
                200, json={"properties": {"cmis:baseTypeId": {"value": "cmis:folder"}}}
            )
        )
        uploader = CmisUploader(_make_config())
        assert uploader.verify_folder_exists("/EXISTS") is True

    @respx.mock
    def test_returns_true_for_succinct_response(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        respx_mock.get(_root_url("EXISTS")).mock(
            return_value=httpx.Response(
                200, json={"succinctProperties": {"cmis:baseTypeId": "cmis:folder"}}
            )
        )
        uploader = CmisUploader(_make_config())
        assert uploader.verify_folder_exists("/EXISTS") is True

    @respx.mock
    def test_returns_false_on_404(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        respx_mock.get(_root_url("MISSING")).mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        uploader = CmisUploader(_make_config())
        assert uploader.verify_folder_exists("/MISSING") is False

    @respx.mock
    def test_returns_false_when_path_is_document_not_folder(
        self, respx_mock: respx.MockRouter
    ) -> None:
        _stub_warmup(respx_mock)
        respx_mock.get(_root_url("DOC_AT_THIS_PATH")).mock(
            return_value=httpx.Response(
                200, json={"properties": {"cmis:baseTypeId": {"value": "cmis:document"}}}
            )
        )
        uploader = CmisUploader(_make_config())
        assert uploader.verify_folder_exists("/DOC_AT_THIS_PATH") is False

    @respx.mock
    def test_raises_on_401(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        respx_mock.get(_root_url("ANY")).mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        uploader = CmisUploader(_make_config())
        with pytest.raises(CMISClientError) as ei:
            uploader.verify_folder_exists("/ANY")
        assert ei.value.status_code == 401

    @respx.mock
    def test_raises_on_5xx(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        respx_mock.get(_root_url("ANY")).mock(
            return_value=httpx.Response(503, json={"error": "server down"})
        )
        uploader = CmisUploader(_make_config())
        with pytest.raises(CMISServerError) as ei:
            uploader.verify_folder_exists("/ANY")
        assert ei.value.status_code == 503

    @respx.mock
    def test_does_not_post_anything(self, respx_mock: respx.MockRouter) -> None:
        """Contrato read-only: nunca se crea una carpeta."""
        _stub_warmup(respx_mock)
        respx_mock.get(_root_url("X")).mock(
            return_value=httpx.Response(
                200, json={"properties": {"cmis:baseTypeId": {"value": "cmis:folder"}}}
            )
        )
        uploader = CmisUploader(_make_config())
        uploader.verify_folder_exists("/X")
        post_calls = [c for c in respx_mock.calls if c.request.method == "POST"]
        assert post_calls == []


# ---------------------------------------------------------------------------
# Grupo 5 — Upload happy path
# ---------------------------------------------------------------------------


class TestUploadHappyPath:
    @respx.mock
    def test_succinct_properties_object_id(
        self, respx_mock: respx.MockRouter, tmp_path: Path
    ) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(
                201, json={"succinctProperties": {"cmis:objectId": "abc-123"}}
            )
        )
        uploader = CmisUploader(_make_config())
        result = uploader.upload(
            file=_make_staged(tmp_path),
            folder_path="/BAC_X",
            object_type_id="$t!-2_BAC_Xv-1",
            document_name="TXN0000001.pdf",
            mime_type="application/pdf",
            properties={"clbNonGroup.BAC_CIF": "000000"},
            batch_id="B-succinct",
        )
        assert result == "abc-123"

    @respx.mock
    def test_standard_properties_object_id_fallback(
        self, respx_mock: respx.MockRouter, tmp_path: Path
    ) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_Y")).mock(
            return_value=httpx.Response(
                201, json={"properties": {"cmis:objectId": {"value": "def-456"}}}
            )
        )
        uploader = CmisUploader(_make_config())
        result = uploader.upload(
            file=_make_staged(tmp_path),
            folder_path="/BAC_Y",
            object_type_id="t",
            document_name="TXN0000002.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="B-standard",
        )
        assert result == "def-456"

    @respx.mock
    def test_id_field_object_id_fallback(
        self, respx_mock: respx.MockRouter, tmp_path: Path
    ) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_Z")).mock(
            return_value=httpx.Response(201, json={"id": "ghi-789"})
        )
        uploader = CmisUploader(_make_config())
        result = uploader.upload(
            file=_make_staged(tmp_path),
            folder_path="/BAC_Z",
            object_type_id="t",
            document_name="TXN0000003.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="B-idfield",
        )
        assert result == "ghi-789"

    @respx.mock
    def test_content_type_is_multipart(self, respx_mock: respx.MockRouter, tmp_path: Path) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "id"}})
        )
        uploader = CmisUploader(_make_config())
        uploader.upload(
            file=_make_staged(tmp_path),
            folder_path="/BAC_X",
            object_type_id="t",
            document_name="TXN0000004.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="B-multipart",
        )
        upload_call = respx_mock.calls[-1]
        assert upload_call.request.headers["content-type"].startswith(
            "multipart/form-data; boundary="
        )


# ---------------------------------------------------------------------------
# Grupo 6 — Política de retry
# ---------------------------------------------------------------------------


class TestUploadRetry:
    @respx.mock
    def test_5xx_then_201(
        self, respx_mock: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _skip_sleep(monkeypatch)
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_R")).mock(
            side_effect=[
                httpx.Response(503, json={}),
                httpx.Response(503, json={}),
                httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "ok"}}),
            ]
        )
        uploader = CmisUploader(_make_config(retry_max_attempts=3))
        result = uploader.upload(
            file=_make_staged(tmp_path),
            folder_path="/BAC_R",
            object_type_id="t",
            document_name="TXN0000010.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="B-5xx-then-201",
        )
        assert result == "ok"
        upload_attempts = [c for c in respx_mock.calls if str(c.request.url).endswith("/BAC_R")]
        assert len(upload_attempts) == 3

    @respx.mock
    def test_4xx_fail_fast(self, respx_mock: respx.MockRouter, tmp_path: Path) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_F")).mock(
            return_value=httpx.Response(400, json={"err": "bad"})
        )
        uploader = CmisUploader(_make_config())
        with pytest.raises(CMISClientError) as ei:
            uploader.upload(
                file=_make_staged(tmp_path),
                folder_path="/BAC_F",
                object_type_id="t",
                document_name="TXN0000011.pdf",
                mime_type="application/pdf",
                properties={},
                batch_id="B-4xx-fail-fast",
            )
        assert ei.value.status_code == 400
        upload_attempts = [c for c in respx_mock.calls if str(c.request.url).endswith("/BAC_F")]
        assert len(upload_attempts) == 1

    @respx.mock
    def test_401_rewarms_and_retries_once(
        self, respx_mock: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _skip_sleep(monkeypatch)
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_A")).mock(
            side_effect=[
                httpx.Response(401, json={}),
                httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "ok"}}),
            ]
        )
        uploader = CmisUploader(_make_config(retry_max_attempts=3))
        result = uploader.upload(
            file=_make_staged(tmp_path),
            folder_path="/BAC_A",
            object_type_id="t",
            document_name="TXN0000012.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="B-401-rewarm",
        )
        assert result == "ok"
        warmup_calls = [c for c in respx_mock.calls if c.request.method == "GET"]
        # 401 dispara re-warmup — 2 GETs en total (inicial + re-warm).
        assert len(warmup_calls) == 2

    @respx.mock
    def test_retries_exhausted(
        self, respx_mock: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _skip_sleep(monkeypatch)
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_E")).mock(return_value=httpx.Response(503, json={}))
        uploader = CmisUploader(_make_config(retry_max_attempts=3))
        with pytest.raises(RetriesExhaustedError) as ei:
            uploader.upload(
                file=_make_staged(tmp_path),
                folder_path="/BAC_E",
                object_type_id="t",
                document_name="TXN0000013.pdf",
                mime_type="application/pdf",
                properties={},
                batch_id="B-retries-exhausted",
            )
        assert ei.value.attempts == 3
        assert isinstance(ei.value.__cause__, CMISServerError)


# ---------------------------------------------------------------------------
# Grupo 7 — Windows 10053
# ---------------------------------------------------------------------------


class TestUploadWindows10053:
    def test_10053_doubles_delay_and_logs_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        captured_delays: list[float] = []
        monkeypatch.setattr(
            "cmcourier.adapters.upload.cmis_uploader.time.sleep",
            lambda s: captured_delays.append(s),
        )

        @respx.mock
        def _run(respx_mock: respx.MockRouter) -> str:
            _stub_warmup(respx_mock)
            respx_mock.post(_root_url("BAC_W")).mock(
                side_effect=[
                    httpx.ConnectError("WSA error 10053"),
                    httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "ok"}}),
                ]
            )
            uploader = CmisUploader(_make_config(retry_base_delay_s=1.0))
            with caplog.at_level(logging.ERROR, logger="cmcourier.adapters.upload.cmis_uploader"):
                return uploader.upload(
                    file=_make_staged(tmp_path),
                    folder_path="/BAC_W",
                    object_type_id="t",
                    document_name="TXN0000020.pdf",
                    mime_type="application/pdf",
                    properties={},
                    batch_id="B-win10053",
                )

        result = _run()
        assert result == "ok"
        # El sleep de 10053 se duplica: base 1.0 * 2^0 * 2 = 2.0
        assert any(s >= 2.0 for s in captured_delays), captured_delays
        assert any("10053" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Grupo 8 — BandwidthLimiter
# ---------------------------------------------------------------------------


class TestTokenBucket:
    """Tests directos del nuevo `token bucket` compartido (029, REQ-001)."""

    def test_zero_mbps_is_noop(self) -> None:
        bucket = TokenBucket(mbps=0.0)
        start = time.monotonic()
        bucket.consume(10_000_000)  # 10 MB
        elapsed = time.monotonic() - start
        assert elapsed < 0.05  # sin throttling

    def test_single_thread_throttles_to_rate(self) -> None:
        # 081: ``mbps`` ahora son megabits/s. 4 Mbps = 0.5 MB/s.
        # Para 1 MB a 0.5 MB/s ≈ 2.0 s nominal.
        bucket = TokenBucket(mbps=4.0)
        start = time.monotonic()
        # Drena 1 MB en 10×100 KB de `chunks` (imita un upload real).
        for _ in range(10):
            bucket.consume(100_000)
        elapsed = time.monotonic() - start
        assert 1.5 < elapsed < 3.0, elapsed

    def test_n_concurrent_workers_share_cap(self) -> None:
        """Test de propiedad REQ-004: N `workers` consumiendo concurrentemente
        contra un `bucket` compartido no pueden exceder la tasa configurada.

        4 `workers` × 0.5 MB cada uno a 1 MB/s → ≈2.0 s agregado.
        Cada `worker` drenando solo tardaría 0.5 s; un `bucket` por `worker`
        los dejaría terminar en paralelo en 0.5 s y probaría el bug. El
        `bucket` compartido los fuerza a serializar los tokens.
        """
        import threading as _threading

        # 081: 8 Mbps = 1 MB/s agregado.
        bucket = TokenBucket(mbps=8.0)
        bytes_per_worker = 500_000  # 0.5 MB cada uno
        n_workers = 4
        results: list[float] = []

        def _worker() -> None:
            t0 = time.monotonic()
            for _ in range(10):
                bucket.consume(bytes_per_worker // 10)
            results.append(time.monotonic() - t0)

        threads = [_threading.Thread(target=_worker, name=f"w_{i}") for i in range(n_workers)]
        wall_start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall_elapsed = time.monotonic() - wall_start

        total_bytes = bytes_per_worker * n_workers  # 2 MB
        # A 1 MB/s agregado, 2 MB tarda ~2.0 s. Cualquier valor muy debajo
        # de 1.5 s probaría que el tope se filtró. Es tolerante con la cota
        # superior (ruido de CI / GIL) — la cota inferior es el assert real.
        assert wall_elapsed > 1.5, (
            f"shared cap leaked: {n_workers} workers drained "
            f"{total_bytes} B in {wall_elapsed:.2f}s "
            f"(expected ≥ {total_bytes / 1_000_000:.1f}s at 1 MB/s)"
        )
        assert wall_elapsed < 4.0, wall_elapsed


class TestBandwidthLimiter:
    def test_throttles_via_shared_bucket(self, tmp_path: Path) -> None:
        size = 1_000_000  # 1 MB
        path = tmp_path / "blob.bin"
        path.write_bytes(b"x" * size)
        # 081: 4 Mbps = 0.5 MB/s. 1 MB en 0.5 MB/s ≈ 2.0 s nominal.
        bucket = TokenBucket(mbps=4.0)
        with path.open("rb") as fh:
            limiter = BandwidthLimiter(fh, bucket)
            start = time.monotonic()
            consumed = 0
            while True:
                chunk = limiter.read(100_000)
                if not chunk:
                    break
                consumed += len(chunk)
            elapsed = time.monotonic() - start
        assert consumed == size
        assert 1.5 < elapsed < 3.0, elapsed

    def test_zero_bucket_passes_through(self) -> None:
        stream = io.BytesIO(b"abcdef")
        limiter = BandwidthLimiter(stream, TokenBucket(mbps=0.0))
        start = time.monotonic()
        data = limiter.read(6)
        elapsed = time.monotonic() - start
        assert data == b"abcdef"
        assert elapsed < 0.1  # sin throttling

    def test_passthrough_methods(self) -> None:
        stream = io.BytesIO(b"abcdef")
        limiter = BandwidthLimiter(stream, TokenBucket(mbps=10.0))
        assert limiter.tell() == 0
        limiter.read(3)
        assert limiter.tell() == 3
        limiter.seek(0)
        assert limiter.tell() == 0
        limiter.close()
        assert stream.closed


# ---------------------------------------------------------------------------
# Grupo 9 — Disciplina de logging (Principio VIII de la Constitución)
# ---------------------------------------------------------------------------


class TestLoggingDiscipline:
    @respx.mock
    def test_retry_log_carries_keys_not_values(
        self,
        respx_mock: respx.MockRouter,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _skip_sleep(monkeypatch)
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_L")).mock(
            side_effect=[
                httpx.Response(503, json={}),
                httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "ok"}}),
            ]
        )
        uploader = CmisUploader(_make_config(retry_max_attempts=3))
        sensitive = "BAC_VALUE_THAT_MUST_NOT_LEAK_999999"
        with caplog.at_level(logging.INFO, logger="cmcourier.adapters.upload.cmis_uploader"):
            uploader.upload(
                file=_make_staged(tmp_path),
                folder_path="/BAC_L",
                object_type_id="t",
                document_name="TXN0000030.pdf",
                mime_type="application/pdf",
                properties={"clbNonGroup.BAC_CIF": sensitive},
                batch_id="B-pii-mask",
            )
        for record in caplog.records:
            assert sensitive not in record.getMessage()
            assert sensitive not in str(record.__dict__.get("extra", ""))


# ---------------------------------------------------------------------------
# Grupo 10 — get_type_definition
# ---------------------------------------------------------------------------


class TestGetTypeDefinition:
    @respx.mock
    def test_200_returns_parsed_dict(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        respx_mock.get(_repo_info_url()).mock(
            return_value=httpx.Response(
                200, json={"id": "$t!-2_BAC_01_02_04_01_01v-1", "displayName": "Auth SMS"}
            )
        )
        uploader = CmisUploader(_make_config())
        result = uploader.get_type_definition("$t!-2_BAC_01_02_04_01_01v-1")
        assert result["id"] == "$t!-2_BAC_01_02_04_01_01v-1"
        assert result["displayName"] == "Auth SMS"

    @respx.mock
    def test_404_raises_client_error(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        respx_mock.get(_repo_info_url()).mock(
            return_value=httpx.Response(404, json={"error": "type not found"})
        )
        uploader = CmisUploader(_make_config())
        with pytest.raises(CMISClientError) as ei:
            uploader.get_type_definition("MISSING")
        assert ei.value.status_code == 404

    @respx.mock
    def test_500_raises_server_error(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        respx_mock.get(_repo_info_url()).mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        uploader = CmisUploader(_make_config())
        with pytest.raises(CMISServerError) as ei:
            uploader.get_type_definition("X")
        assert ei.value.status_code == 500


# ---------------------------------------------------------------------------
# Conformidad del port (019, Principio I de la Constitución)
# ---------------------------------------------------------------------------


class TestPortConformance:
    def test_cmis_uploader_is_iuploader(self) -> None:
        from cmcourier.domain.ports import IUploader

        uploader = CmisUploader(_make_config())
        assert isinstance(uploader, IUploader)


# ---------------------------------------------------------------------------
# 038 — eventos s5_upload_attempt + s5_upload_failed
# ---------------------------------------------------------------------------


class TestUploadPayloadTraceEvents:
    @respx.mock
    def test_attempt_event_emitted_on_success(
        self, respx_mock: respx.MockRouter, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "abc"}})
        )
        uploader = CmisUploader(_make_config())
        with caplog.at_level(logging.INFO, logger="cmcourier.metrics.network"):
            uploader.upload(
                file=_make_staged(tmp_path),
                folder_path="/BAC_X",
                object_type_id="D:cmcourier:bacDoc",
                document_name="TXN1.pdf",
                mime_type="application/pdf",
                properties={"clbNonGroup.BAC_CIF": "00123456"},
                batch_id="B-attempt-event",
            )
        attempts = [r for r in caplog.records if getattr(r, "event", "") == "s5_upload_attempt"]
        assert len(attempts) == 1
        rec = attempts[0]
        assert rec.object_type_id == "D:cmcourier:bacDoc"
        assert rec.document_name == "TXN1.pdf"
        # PII enmascarada por default: el valor de CIF no debería aparecer.
        assert "00123456" not in rec.properties_json
        # `cmis:name` y mime son seguros — aparecen crudos.
        assert "TXN1.pdf" in rec.properties_json
        assert "application/pdf" in rec.properties_json

    @respx.mock
    def test_failed_event_emitted_with_curl_equivalent(
        self,
        respx_mock: respx.MockRouter,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_warmup(respx_mock)
        _skip_sleep(monkeypatch)
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(
                400, json={"error": "constraint", "message": "property unknown"}
            )
        )
        uploader = CmisUploader(_make_config())
        with (
            caplog.at_level(logging.INFO, logger="cmcourier.metrics.network"),
            pytest.raises(CMISClientError),
        ):
            uploader.upload(
                file=_make_staged(tmp_path),
                folder_path="/BAC_X",
                object_type_id="D:cmcourier:bacDoc",
                document_name="TXN2.pdf",
                mime_type="application/pdf",
                properties={"clbNonGroup.BAC_CIF": "00123456"},
                batch_id="B-failed-event",
            )
        attempts = [r for r in caplog.records if getattr(r, "event", "") == "s5_upload_attempt"]
        failures = [r for r in caplog.records if getattr(r, "event", "") == "s5_upload_failed"]
        assert len(attempts) == 1
        assert len(failures) == 1
        fail = failures[0]
        assert fail.status_code == 400
        assert "createDocument" in fail.curl_equivalent
        assert "D:cmcourier:bacDoc" in fail.curl_equivalent
        # PII enmascarada: el CIF crudo no debe estar ni en las propiedades
        # JSON ni en el dump curl-equivalent.
        assert "00123456" not in fail.properties_json
        assert "00123456" not in fail.curl_equivalent

    @respx.mock
    def test_unmask_pii_emits_raw_values(
        self, respx_mock: respx.MockRouter, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "abc"}})
        )
        uploader = CmisUploader(_make_config(unmask_pii=True))
        with caplog.at_level(logging.INFO, logger="cmcourier.metrics.network"):
            uploader.upload(
                file=_make_staged(tmp_path),
                folder_path="/BAC_X",
                object_type_id="D:cmcourier:bacDoc",
                document_name="TXN3.pdf",
                mime_type="application/pdf",
                properties={"clbNonGroup.BAC_CIF": "00123456"},
                batch_id="B-unmask-pii",
            )
        attempts = [r for r in caplog.records if getattr(r, "event", "") == "s5_upload_attempt"]
        assert len(attempts) == 1
        assert "00123456" in attempts[0].properties_json


# ---------------------------------------------------------------------------
# 040 — Compatibilidad de URL de Alfresco (semántica de repo_id="")
# ---------------------------------------------------------------------------


class TestServiceUrl:
    """Nivel unitario — ejercita el helper ``_service_url`` directamente."""

    def test_empty_repo_id_no_suffix(self) -> None:
        uploader = CmisUploader(_make_config(repo_id=""))
        assert uploader._service_url() == _BASE_URL

    def test_set_repo_id_no_suffix(self) -> None:
        uploader = CmisUploader(_make_config(repo_id="$x!something"))
        assert uploader._service_url() == f"{_BASE_URL}/$x!something"

    def test_empty_repo_id_with_suffix(self) -> None:
        uploader = CmisUploader(_make_config(repo_id=""))
        assert uploader._service_url("root/A/B") == f"{_BASE_URL}/root/A/B"

    def test_set_repo_id_with_suffix(self) -> None:
        uploader = CmisUploader(_make_config(repo_id="$x!something"))
        expected = f"{_BASE_URL}/$x!something/root/A/B"
        assert uploader._service_url("root/A/B") == expected


class TestAlfrescoStyleUrls:
    """Nivel wire — cuando ``repo_id=""`` el adapter NO debe emitir
    ``.../base//root/...`` (Alfresco rechaza con HTTP 405).
    """

    @respx.mock
    def test_verify_folder_exists_emits_no_repo_id_segment(
        self, respx_mock: respx.MockRouter, tmp_path: Path
    ) -> None:
        _stub_warmup_alfresco_style(respx_mock)
        respx_mock.get(f"{_BASE_URL}/root/X").mock(
            return_value=httpx.Response(
                200, json={"properties": {"cmis:baseTypeId": {"value": "cmis:folder"}}}
            )
        )
        uploader = CmisUploader(_make_config(repo_id=""))
        assert uploader.verify_folder_exists("/X") is True
        # La URL del GET no debe tener un slash duplicado en ningún lado después del host.
        for call in respx_mock.calls:
            url_str = str(call.request.url)
            path = url_str.split(_BASE_URL, 1)[1]
            assert "//" not in path, url_str

    @respx.mock
    def test_upload_emits_no_repo_id_segment(
        self, respx_mock: respx.MockRouter, tmp_path: Path
    ) -> None:
        _stub_warmup_alfresco_style(respx_mock)
        respx_mock.post(f"{_BASE_URL}/root/X").mock(
            return_value=httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "id"}})
        )
        uploader = CmisUploader(_make_config(repo_id=""))
        result = uploader.upload(
            file=_make_staged(tmp_path),
            folder_path="/X",
            object_type_id="D:cmcourier:bacDoc",
            document_name="TXN.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="B-no-repo-id",
        )
        assert result == "id"
        # La URL del POST no debe tener un slash duplicado.
        post = [c for c in respx_mock.calls if c.request.method == "POST"][0]
        path = str(post.request.url).split(_BASE_URL, 1)[1]
        assert "//" not in path

    @respx.mock
    def test_get_type_definition_no_repo_id_segment(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup_alfresco_style(respx_mock)
        respx_mock.get(_BASE_URL).mock(
            return_value=httpx.Response(200, json={"id": "D:cmcourier:bacDoc"})
        )
        uploader = CmisUploader(_make_config(repo_id=""))
        result = uploader.get_type_definition("D:cmcourier:bacDoc")
        assert result["id"] == "D:cmcourier:bacDoc"


# ---------------------------------------------------------------------------
# 045 — recuperación idempotente de 409
# ---------------------------------------------------------------------------


class TestUpload409Recovery045:
    @respx.mock
    def test_409_recovered_returns_existing_object_id(
        self, respx_mock: respx.MockRouter, tmp_path: Path
    ) -> None:
        _stub_warmup(respx_mock)
        # El POST de documento colisiona en cmis:name → 409 de Alfresco.
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(409, json={"exception": "contentAlreadyExists"})
        )
        # El lookup de hijos de la misma carpeta devuelve el huérfano anterior.
        respx_mock.get(_root_url("BAC_X")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "objects": [
                        {
                            "object": {
                                "properties": {
                                    "cmis:name": "TXN0000050.pdf",
                                    "cmis:objectId": "recovered-xyz",
                                }
                            }
                        }
                    ]
                },
            )
        )
        uploader = CmisUploader(_make_config())
        result = uploader.upload(
            file=_make_staged(tmp_path),
            folder_path="/BAC_X",
            object_type_id="t",
            document_name="TXN0000050.pdf",
            mime_type="application/pdf",
            properties={"clbNonGroup.BAC_CIF": "000000"},
            batch_id="B-409-recovered",
        )
        assert result == "recovered-xyz"

    @respx.mock
    def test_409_with_no_matching_child_reraises(
        self, respx_mock: respx.MockRouter, tmp_path: Path
    ) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(409, json={"exception": "contentAlreadyExists"})
        )
        # El lookup de hijos NO matchea — el 409 fue por otro motivo.
        respx_mock.get(_root_url("BAC_X")).mock(
            return_value=httpx.Response(200, json={"objects": []})
        )
        uploader = CmisUploader(_make_config())
        with pytest.raises(CMISClientError) as exc:
            uploader.upload(
                file=_make_staged(tmp_path),
                folder_path="/BAC_X",
                object_type_id="t",
                document_name="TXN0000051.pdf",
                mime_type="application/pdf",
                properties={},
                batch_id="B-409-no-match",
            )
        assert exc.value.status_code == 409

    @respx.mock
    def test_200_does_not_trigger_lookup(
        self, respx_mock: respx.MockRouter, tmp_path: Path
    ) -> None:
        """El happy path NO debe llamar al lookup de recuperación (sin drift de comportamiento)."""
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(
                201, json={"succinctProperties": {"cmis:objectId": "fresh-abc"}}
            )
        )
        # Si el uploader llamara al GET de hijos, respx levantaría en la
        # ruta no matcheada (comportamiento por default).
        uploader = CmisUploader(_make_config())
        result = uploader.upload(
            file=_make_staged(tmp_path),
            folder_path="/BAC_X",
            object_type_id="t",
            document_name="TXN0000052.pdf",
            mime_type="application/pdf",
            properties={},
            batch_id="B-200-no-lookup",
        )
        assert result == "fresh-abc"


# ---------------------------------------------------------------------------
# 055 — los eventos de red llevan el batch_id así los handlers por-batch de
# ancho de banda + slow-op realmente los reciben.
# ---------------------------------------------------------------------------


class TestNetworkEventBatchId055:
    @respx.mock
    def test_upload_event_reaches_bandwidth_and_slowop_handlers(
        self, respx_mock: respx.MockRouter, tmp_path: Path
    ) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "id"}})
        )
        recorder = MetricsRecorder(
            log_dir=tmp_path / "logs",
            slow_op_threshold_ms=0.0,
            slow_op_top_n=10,
            enabled=True,
            pipeline_metrics_enabled=True,
        )
        net_log = logging.getLogger("cmcourier.metrics.network")
        prev_level = net_log.level
        net_log.setLevel(logging.INFO)
        recorder.start_batch(pipeline="csv-trigger", batch_id="B1")
        try:
            CmisUploader(_make_config()).upload(
                file=_make_staged(tmp_path, size_bytes=64_000),
                folder_path="/BAC_X",
                object_type_id="t",
                document_name="TXN0000060.pdf",
                mime_type="application/pdf",
                properties={},
                batch_id="B1",
            )
            # El sampler de ancho de banda recibió los bytes subidos...
            assert recorder.bandwidth.cumulative_bytes() > 0
            assert recorder.bandwidth.peak_mbps() > 0.0
            # ...y el agregador de `slow-op` vio la op cmis_upload.
            assert any(op.get("kind") == "cmis_upload" for op in recorder.aggregator_snapshot())
        finally:
            net_log.setLevel(prev_level)
            recorder.close_batch(pipeline="csv-trigger", batch_id="B1", total_docs=1, elapsed_s=1.0)

    @respx.mock
    def test_emit_network_record_carries_batch_id(
        self, respx_mock: respx.MockRouter, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _stub_warmup(respx_mock)
        respx_mock.post(_root_url("BAC_X")).mock(
            return_value=httpx.Response(201, json={"succinctProperties": {"cmis:objectId": "id"}})
        )
        with caplog.at_level(logging.INFO, logger="cmcourier.metrics.network"):
            CmisUploader(_make_config()).upload(
                file=_make_staged(tmp_path),
                folder_path="/BAC_X",
                object_type_id="t",
                document_name="TXN0000061.pdf",
                mime_type="application/pdf",
                properties={},
                batch_id="B-carries",
            )
        uploads = [r for r in caplog.records if getattr(r, "kind", "") == "cmis_upload"]
        assert len(uploads) == 1
        assert uploads[0].batch_id == "B-carries"


# ---------------------------------------------------------------------------
# 060 — Negociación HTTP/2
# ---------------------------------------------------------------------------


class TestHttp2Enabled060:
    def test_client_built_with_http2_enabled(self) -> None:
        # El adapter anuncia HTTP/2 vía ALPN. El servidor decide si lo
        # negocia. Acá fijamos que el cliente lo *ofrezca*.
        uploader = CmisUploader(_make_config())
        # httpx.Client._transport es HTTPTransport; el flag http2 está en
        # la config del pool. Invariante más sencillo: el AsyncClient/Client
        # lleva http2 = True al construirse.
        assert uploader._client._transport._pool._http2 is True  # noqa: SLF001


# ---------------------------------------------------------------------------
# Grupo — 132: credenciales en caliente
# ---------------------------------------------------------------------------


def _basic(user: str, pwd: str) -> str:
    import base64

    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


class TestSetCredentials132:
    @respx.mock
    def test_next_call_rewarms_with_new_auth_and_no_old_cookie(
        self, respx_mock: respx.MockRouter
    ) -> None:
        # Warmup viejo deja una cookie de sesión.
        respx_mock.get(_repo_info_url()).mock(
            return_value=httpx.Response(
                200,
                json={"repositoryId": _REPO_ID, "productName": "x", "productVersion": "1"},
                headers={"Set-Cookie": "JSESSIONID=OLD; Path=/"},
            )
        )
        respx_mock.get(_repo_info_url(), params={"cmisselector": "typeDefinition"}).mock(
            return_value=httpx.Response(200, json={"id": "t"})
        )
        uploader = CmisUploader(_make_config())
        uploader.get_type_definition("t")
        assert len(respx_mock.calls) == 2  # warmup + typeDefinition
        assert uploader._client.cookies.get("JSESSIONID") == "OLD"  # noqa: SLF001
        assert respx_mock.calls[0].request.headers["Authorization"] == _basic(
            "tester", "secret-not-real"
        )

        uploader.set_credentials("nuevo", "clave")
        uploader.get_type_definition("t")

        # Sesión fría: warmup NUEVO con el auth nuevo y sin la cookie vieja.
        assert len(respx_mock.calls) == 4
        warm = respx_mock.calls[2].request
        assert "cmisselector=repositoryInfo" in str(warm.url)
        assert warm.headers["Authorization"] == _basic("nuevo", "clave")
        assert "OLD" not in warm.headers.get("Cookie", "")
        assert respx_mock.calls[3].request.headers["Authorization"] == _basic("nuevo", "clave")

    @respx.mock
    def test_set_credentials_without_warmup_is_harmless(self, respx_mock: respx.MockRouter) -> None:
        _stub_warmup(respx_mock)
        uploader = CmisUploader(_make_config())
        uploader.set_credentials("nuevo", "clave")
        uploader.test_connection()
        assert len(respx_mock.calls) == 1
        assert respx_mock.calls[0].request.headers["Authorization"] == _basic("nuevo", "clave")
