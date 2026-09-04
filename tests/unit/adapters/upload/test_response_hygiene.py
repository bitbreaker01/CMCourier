"""Tests de la higiene de respuestas del uploader (120).

* P11: ``resp.text`` se decodificaba incondicionalmente antes de chequear
  el status — trabajo tirado en cada 200.
* P12: el lookup de 409 pedía 5000 hijos con TODAS sus propiedades; ahora
  pide solo ``cmis:name`` y ``cmis:objectId`` en formato succinct.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, PropertyMock

import pytest

from cmcourier.adapters.upload.cmis_uploader import CmisConfig, CmisUploader
from cmcourier.domain.exceptions import CMISServerError

pytestmark = pytest.mark.unit


def _make_uploader() -> CmisUploader:
    return CmisUploader(
        CmisConfig(
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
    )


def _response(status_code: int, json_data: Any, *, text: str = "{}") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    text_mock = PropertyMock(return_value=text)
    type(resp).text = text_mock
    resp._text_mock = text_mock
    return resp


class TestLazyBodyDecode:
    def test_200_never_decodes_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """E1: el path feliz no toca resp.text."""
        ok = _response(200, {"id": "cmis:document"})
        uploader = _make_uploader()
        uploader._warm = True  # noqa: SLF001 — saltea el warmup para aislar el GET
        monkeypatch.setattr("httpx.Client.get", lambda self, url, **kw: ok)
        result = uploader.get_type_definition("cmis:document")

        assert result == {"id": "cmis:document"}
        ok._text_mock.assert_not_called()

    def test_500_raises_with_truncated_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """E2: el error sí decodifica y trunca."""
        boom = _response(500, {}, text="x" * 5000)
        uploader = _make_uploader()
        uploader._warm = True  # noqa: SLF001
        monkeypatch.setattr("httpx.Client.get", lambda self, url, **kw: boom)

        with pytest.raises(CMISServerError) as ei:
            uploader.get_type_definition("cmis:document")
        assert len(ei.value.context["response_body"]) <= 1024 + 32


class TestLightweight409Lookup:
    def _capture_lookup(
        self, monkeypatch: pytest.MonkeyPatch, json_data: Any
    ) -> tuple[CmisUploader, dict[str, Any]]:
        captured: dict[str, Any] = {}

        def fake_get(self: Any, url: str, **kwargs: Any) -> Any:
            captured["url"] = url
            captured.update(kwargs)
            return _response(200, json_data)

        monkeypatch.setattr("httpx.Client.get", fake_get)
        uploader = _make_uploader()
        uploader._warm = True  # noqa: SLF001
        return uploader, captured

    def test_lookup_requests_only_needed_properties(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """E3: la request lleva filter + succinct."""
        uploader, captured = self._capture_lookup(monkeypatch, {"objects": []})
        uploader._lookup_existing_object_id("http://cm.test/folder", "doc.pdf")  # noqa: SLF001

        params = captured["params"]
        assert params["succinct"] == "true"
        assert set(params["filter"].split(",")) == {"cmis:name", "cmis:objectId"}
        assert params["cmisselector"] == "children"

    def test_lookup_resolves_id_from_succinct_properties(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E4: parse del formato succinct."""
        payload = {
            "objects": [
                {
                    "object": {
                        "succinctProperties": {
                            "cmis:name": "otro.pdf",
                            "cmis:objectId": "id-otro",
                        }
                    }
                },
                {
                    "object": {
                        "succinctProperties": {
                            "cmis:name": "doc.pdf",
                            "cmis:objectId": "id-buscado",
                        }
                    }
                },
            ]
        }
        uploader, _ = self._capture_lookup(monkeypatch, payload)
        found = uploader._lookup_existing_object_id("http://cm.test/folder", "doc.pdf")  # noqa: SLF001
        assert found == "id-buscado"
