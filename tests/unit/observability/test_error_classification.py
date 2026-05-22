"""Tests de ``classify_failure`` (104, REQ-002)."""

from __future__ import annotations

import httpx
import pytest

from cmcourier.domain.exceptions import (
    CMISClientError,
    CMISServerError,
    PDFAssemblyFailedError,
    RetriesExhaustedError,
)
from cmcourier.observability.error_classification import classify_failure

pytestmark = pytest.mark.unit


def _retries_exhausted_from(cause: BaseException) -> RetriesExhaustedError:
    """Construye un RetriesExhaustedError encadenado a *cause*, como lo hace
    el uploader con ``raise ... from last_exc``."""
    exc = RetriesExhaustedError(txn_num="T1", attempts=3)
    exc.__cause__ = cause
    return exc


class TestClassifyDirectExceptions:
    def test_client_error_4xx(self) -> None:
        assert classify_failure(CMISClientError(status_code=400)) == ("http_4xx", 400)

    def test_client_error_409(self) -> None:
        # Un 409 sin recuperar que llega como excepción cuenta como 4xx.
        assert classify_failure(CMISClientError(status_code=409)) == ("http_4xx", 409)

    def test_server_error_5xx(self) -> None:
        assert classify_failure(CMISServerError(status_code=503)) == ("http_5xx", 503)

    def test_server_error_500(self) -> None:
        assert classify_failure(CMISServerError(status_code=500)) == ("http_5xx", 500)


class TestClassifyUnwrapsRetriesExhausted:
    """REQ-002: la causa raíz se encuentra desenvolviendo ``__cause__``."""

    def test_retries_exhausted_over_5xx(self) -> None:
        exc = _retries_exhausted_from(CMISServerError(status_code=502))
        assert classify_failure(exc) == ("http_5xx", 502)

    def test_retries_exhausted_over_read_timeout(self) -> None:
        exc = _retries_exhausted_from(httpx.ReadTimeout("slow"))
        assert classify_failure(exc) == ("timeout", None)

    def test_retries_exhausted_over_connect_timeout(self) -> None:
        exc = _retries_exhausted_from(httpx.ConnectTimeout("slow"))
        assert classify_failure(exc) == ("timeout", None)

    def test_retries_exhausted_over_connect_error(self) -> None:
        exc = _retries_exhausted_from(httpx.ConnectError("refused"))
        assert classify_failure(exc) == ("transport", None)

    def test_retries_exhausted_without_cause_is_app_error(self) -> None:
        assert classify_failure(RetriesExhaustedError(txn_num="T1", attempts=3)) == (
            "app_error",
            None,
        )


class TestClassifyFallbacks:
    def test_bare_timeout_exception(self) -> None:
        assert classify_failure(httpx.ReadTimeout("slow")) == ("timeout", None)

    def test_bare_transport_error(self) -> None:
        assert classify_failure(httpx.RemoteProtocolError("boom")) == ("transport", None)

    def test_unknown_exception_is_app_error(self) -> None:
        assert classify_failure(ValueError("???")) == ("app_error", None)

    def test_pipeline_stage_error_is_app_error(self) -> None:
        exc = PDFAssemblyFailedError(txn_num="T1", reason="corrupt")
        assert classify_failure(exc) == ("app_error", None)
