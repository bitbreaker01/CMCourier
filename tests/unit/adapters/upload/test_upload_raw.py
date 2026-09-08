"""141 REQ-003 (E3) — ``upload_raw`` / ``delete_object`` con respuesta CRUDA.

Un solo POST, sin reintentos, sin re-auth: el 4xx/5xx se DEVUELVE como
:class:`RawResponse` en lugar de lanzarse. Sólo el error de transporte
sube como :class:`CMISServerError`, con el curl equivalente en el
contexto.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cmcourier.adapters.upload.cmis_uploader import CmisConfig, CmisUploader
from cmcourier.domain.exceptions import CMISServerError
from cmcourier.domain.models import StagedFile
from cmcourier.services.practice_upload import RawResponse

pytestmark = pytest.mark.unit

_PROPERTIES = {"cmcourier:BAC_CIF": "123456", "cmcourier:Short_Name": "TESTCLIENT01"}


def _uploader(handler, *, repo_id: str = "repo") -> CmisUploader:  # type: ignore[no-untyped-def]
    uploader = CmisUploader(
        CmisConfig(
            base_url="http://cm.test/cmis",
            repo_id=repo_id,
            username="admin",
            password="s3cr3t0",
            timeout_seconds=5.0,
            retry_max_attempts=3,
            retry_base_delay_s=0.01,
        )
    )
    uploader._client.close()  # noqa: SLF001 — reemplazamos el transporte real
    uploader._client = httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        auth=("admin", "s3cr3t0"),
    )
    return uploader


def _staged(tmp_path: Path, payload: bytes = b"%PDF-1.4 contenido de prueba") -> StagedFile:
    path = tmp_path / "PRUEBA-CN01.pdf"
    path.write_bytes(payload)
    return StagedFile(path=path, size_bytes=len(payload), page_count=1)


def _upload(uploader: CmisUploader, staged: StagedFile) -> RawResponse:
    return uploader.upload_raw(
        staged,
        folder_path="/cmcourier-staging/CN01",
        object_type_id="D:cmcourier:bacDoc",
        document_name="PRUEBA-CN01.pdf",
        mime_type="application/pdf",
        properties=_PROPERTIES,
    )


class TestUploadRawSuccess:
    def test_201_parses_object_id_and_keeps_full_body(self, tmp_path: Path) -> None:
        body = json.dumps(
            {
                "succinctProperties": {"cmis:objectId": "workspace://abc-123"},
                "relleno": "x" * 10_000,
            }
        )
        assert len(body) > 10_000
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            seen.append(request)
            return httpx.Response(201, text=body, headers={"X-Prueba": "ok"})

        response = _upload(_uploader(handler), _staged(tmp_path))

        assert isinstance(response, RawResponse)
        assert response.ok
        assert response.status_code == 201
        assert response.object_id == "workspace://abc-123"
        assert response.body == body  # COMPLETO — nada de truncar a 1024
        assert response.headers["x-prueba"] == "ok"
        assert response.elapsed_ms >= 0
        assert len(seen) == 1

    def test_posts_multipart_create_document_with_properties(self, tmp_path: Path) -> None:
        seen: list[bytes] = []
        urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.read())
            urls.append(str(request.url))
            return httpx.Response(201, text='{"succinctProperties": {"cmis:objectId": "id"}}')

        _upload(_uploader(handler), _staged(tmp_path))

        payload = seen[0].decode("utf-8", "replace")
        assert "createDocument" in payload
        assert "cmcourier:BAC_CIF" in payload
        assert "123456" in payload
        assert "D:cmcourier:bacDoc" in payload
        # 141 antagonista I7: la URL tiene que llevar el ``folder_path``
        # RESUELTO — la misma convención IBM CM que ``_service_url`` usa
        # para el resto de los métodos (``base/<repo_id>/root/<folder>``).
        assert urls[0] == "http://cm.test/cmis/repo/root/cmcourier-staging/CN01"

    def test_curl_masks_the_password(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            return httpx.Response(201, text="{}")

        response = _upload(_uploader(handler), _staged(tmp_path))

        assert "s3cr3t0" not in response.curl
        assert "-u admin:***" in response.curl
        assert "createDocument" in response.curl

    # -------------------------------------------------------------- I5
    # el curl hardcodeaba "-u admin:***" sin importar el username real de
    # la config — engañoso para el operador que copia el comando.

    def test_curl_uses_the_real_username_not_hardcoded_admin(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            return httpx.Response(201, text="{}")

        uploader = CmisUploader(
            CmisConfig(
                base_url="http://cm.test/cmis",
                repo_id="repo",
                username="operador",
                password="s3cr3t0",
                timeout_seconds=5.0,
            )
        )
        uploader._client.close()  # noqa: SLF001
        uploader._client = httpx.Client(  # noqa: SLF001
            transport=httpx.MockTransport(handler), auth=("operador", "s3cr3t0")
        )
        response = _upload(uploader, _staged(tmp_path))

        assert "-u operador:***" in response.curl
        assert "s3cr3t0" not in response.curl
        assert "-u admin:***" not in response.curl


class TestUploadRawFailure:
    def test_409_is_returned_not_raised_and_never_retried(self, tmp_path: Path) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            calls.append(1)
            return httpx.Response(409, text="conflict: ya existe")

        response = _upload(_uploader(handler), _staged(tmp_path))

        assert len(calls) == 1  # sin reintentos
        assert response.ok is False
        assert response.status_code == 409
        assert response.object_id is None
        assert response.body == "conflict: ya existe"

    def test_401_is_returned_without_reauth(self, tmp_path: Path) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            calls.append(1)
            return httpx.Response(401, text="unauthorized")

        response = _upload(_uploader(handler), _staged(tmp_path))

        assert len(calls) == 1  # sin re-warmup ni segundo intento
        assert response.status_code == 401
        assert response.ok is False

    def test_500_is_returned_without_backoff(self, tmp_path: Path) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            calls.append(1)
            return httpx.Response(500, text="boom")

        response = _upload(_uploader(handler), _staged(tmp_path))

        assert len(calls) == 1
        assert response.status_code == 500
        assert response.ok is False

    def test_transport_error_raises_with_curl_in_context(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no hay nadie del otro lado", request=request)

        with pytest.raises(CMISServerError) as excinfo:
            _upload(_uploader(handler), _staged(tmp_path))

        curl = excinfo.value.context["curl"]
        assert isinstance(curl, str)
        assert "-u admin:***" in curl
        assert "s3cr3t0" not in curl


class TestDeleteObject:
    def test_posts_cmisaction_delete_with_object_id(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            seen.append(request)
            return httpx.Response(200, text="{}")

        response = _uploader(handler).delete_object("abc")

        request = seen[0]
        payload = request.content.decode("utf-8", "replace")
        assert "cmisaction=delete" in payload
        assert "objectId=abc" in str(request.url) or "objectId=abc" in payload
        assert "allVersions=true" in payload
        assert str(request.url).startswith("http://cm.test/cmis/repo/root")
        assert response.ok
        assert response.status_code == 200

    def test_error_is_returned_not_raised(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            return httpx.Response(404, text="no existe")

        response = _uploader(handler).delete_object("abc")

        assert response.ok is False
        assert response.body == "no existe"

    def test_transport_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("caído", request=request)

        with pytest.raises(CMISServerError) as excinfo:
            _uploader(handler).delete_object("abc")

        assert "curl" in excinfo.value.context

    # -------------------------------------------------------------- M1
    # delete_object("") armaba igual el request contra root?objectId= — el
    # adapter tiene que fallar ANTES de tocar la red.

    def test_empty_object_id_raises_before_any_request(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, text="{}")

        with pytest.raises(ValueError, match="object_id"):
            _uploader(handler).delete_object("")
        assert calls == []

    def test_whitespace_object_id_raises_before_any_request(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, text="{}")

        with pytest.raises(ValueError, match="object_id"):
            _uploader(handler).delete_object("   ")
        assert calls == []

    # -------------------------------------------------------------- I5
    def test_delete_curl_uses_the_real_username(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            return httpx.Response(200, text="{}")

        uploader = CmisUploader(
            CmisConfig(
                base_url="http://cm.test/cmis",
                repo_id="repo",
                username="operador",
                password="s3cr3t0",
                timeout_seconds=5.0,
            )
        )
        uploader._client.close()  # noqa: SLF001
        uploader._client = httpx.Client(  # noqa: SLF001
            transport=httpx.MockTransport(handler), auth=("operador", "s3cr3t0")
        )
        response = uploader.delete_object("abc")

        assert "-u operador:***" in response.curl
        assert "s3cr3t0" not in response.curl


class TestRawResponse:
    def test_ok_only_for_2xx(self) -> None:
        assert RawResponse(200, "OK", {}, "", 1, "curl").ok
        assert RawResponse(299, "?", {}, "", 1, "curl").ok
        assert not RawResponse(302, "Found", {}, "", 1, "curl").ok
        assert not RawResponse(404, "Not Found", {}, "", 1, "curl").ok

    def test_object_id_from_properties_route(self) -> None:
        body = json.dumps({"properties": {"cmis:objectId": {"value": "por-properties"}}})
        assert RawResponse(201, "Created", {}, body, 1, "c").object_id == "por-properties"

    def test_object_id_none_when_body_is_not_json(self) -> None:
        assert RawResponse(201, "Created", {}, "<html>error</html>", 1, "c").object_id is None

    # -------------------------------------------------------------- B3
    # object_id NUNCA cae al fallback `data["id"]`: un cuerpo de error de
    # Alfresco trae un "id" que es la carpeta destino, no el documento.

    def test_id_only_route_no_longer_falls_back(self) -> None:
        body = json.dumps({"exception": "constraint", "id": "workspace://SpacesStore/FOLDER-ROOT"})
        assert RawResponse(201, "Created", {}, body, 1, "c").object_id is None

    def test_id_boolean_is_not_an_object_id(self) -> None:
        body = json.dumps({"id": True})
        assert RawResponse(201, "Created", {}, body, 1, "c").object_id is None

    def test_not_ok_status_never_parses_an_object_id(self) -> None:
        """Un 500 con succinctProperties válido igual devuelve None: sin
        ``ok`` no hay documento que borrar."""
        body = json.dumps({"succinctProperties": {"cmis:objectId": "workspace://obj-1"}})
        assert RawResponse(500, "Internal Server Error", {}, body, 1, "c").object_id is None

    def test_folder_base_type_is_not_a_document(self) -> None:
        body = json.dumps(
            {
                "succinctProperties": {
                    "cmis:objectId": "folder-999",
                    "cmis:baseTypeId": "cmis:folder",
                }
            }
        )
        assert RawResponse(201, "Created", {}, body, 1, "c").object_id is None

    def test_succinct_document_base_type_is_accepted(self) -> None:
        body = json.dumps(
            {"succinctProperties": {"cmis:objectId": "doc-1", "cmis:baseTypeId": "cmis:document"}}
        )
        assert RawResponse(201, "Created", {}, body, 1, "c").object_id == "doc-1"

    def test_properties_route_rejects_folder_base_type(self) -> None:
        body = json.dumps(
            {
                "properties": {
                    "cmis:objectId": {"value": "folder-1"},
                    "cmis:baseTypeId": {"value": "cmis:folder"},
                }
            }
        )
        assert RawResponse(201, "Created", {}, body, 1, "c").object_id is None
