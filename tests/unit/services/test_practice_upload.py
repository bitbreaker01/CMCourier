"""141 REQ-004 (E4) — el caso de uso puro del tiro de prueba.

Validación de los metadatos requeridos, traducción a ``CMISPropertyId``,
nombre del documento y el ciclo generar → subir → borrar el temporal.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from cmcourier.domain.models import CMMapping, StagedFile
from cmcourier.services.practice_upload import (
    PracticeDraft,
    RawResponse,
    build_properties,
    document_name,
    run_practice_upload,
    validate_values,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 3, 4, 9, 8, 7)
_OK = RawResponse(201, "Created", {}, '{"id": "obj-1"}', 12, "curl …")


def _mapping(*, with_ids: bool = True, folder: str | None = "/cm/CN01") -> CMMapping:
    ids = {"BAC_Nombre": "cmcourier:Nombre", "BAC_CIF": "cmcourier:CIF"}
    return CMMapping(
        clase_id="01.01.01.01.01",
        id_rvi="FB01",
        id_corto="CN01",
        clase_name="01.01.01.01.01",
        required_metadata_fields=("BAC_Nombre", "BAC_CIF"),
        cmis_type="D:cmcourier:bacDoc",
        cmis_folder=folder,
        cmis_property_ids=MappingProxyType(ids) if with_ids else None,
    )


class _FakeUploader:
    """Doble del puerto: registra la llamada y comprueba el temporal."""

    def __init__(self, *, boom: Exception | None = None) -> None:
        self.calls: list[tuple[StagedFile, str, str, str, str, dict[str, str]]] = []
        self.existed_during_upload: bool | None = None
        self._boom = boom

    def upload_raw(
        self,
        file: StagedFile,
        folder_path: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
    ) -> RawResponse:
        self.existed_during_upload = file.path.exists()
        self.calls.append(
            (file, folder_path, object_type_id, document_name, mime_type, dict(properties))
        )
        if self._boom is not None:
            raise self._boom
        return _OK

    def delete_object(self, object_id: str) -> RawResponse:  # pragma: no cover — no se usa acá
        raise NotImplementedError


class TestValidateValues:
    def test_empty_required_field(self) -> None:
        errors = validate_values(_mapping(), {"BAC_Nombre": "  ", "BAC_CIF": "123"})
        assert errors == {"BAC_Nombre": "requerido"}

    def test_missing_key_counts_as_empty(self) -> None:
        assert validate_values(_mapping(), {})["BAC_CIF"] == "requerido"

    def test_all_filled_is_clean(self) -> None:
        assert validate_values(_mapping(), {"BAC_Nombre": "Juan", "BAC_CIF": "123"}) == {}

    def test_field_without_cmis_property_id(self) -> None:
        mapping = CMMapping(
            clase_id="01",
            id_rvi="FB01",
            id_corto="CN01",
            clase_name="c",
            required_metadata_fields=("BAC_Nombre", "BAC_Huerfano"),
            cmis_property_ids=MappingProxyType({"BAC_Nombre": "cmcourier:Nombre"}),
        )
        errors = validate_values(mapping, {"BAC_Nombre": "Juan", "BAC_Huerfano": "x"})
        assert errors == {"BAC_Huerfano": "sin CMISPropertyId en el mapping"}

    def test_no_catalog_never_complains_about_property_ids(self) -> None:
        assert validate_values(_mapping(with_ids=False), {"BAC_Nombre": "a", "BAC_CIF": "b"}) == {}


class TestBuildProperties:
    def test_translates_to_cmis_property_ids(self) -> None:
        props = build_properties(_mapping(), {"BAC_Nombre": " Juan ", "BAC_CIF": "123"})
        assert props == {"cmcourier:Nombre": "Juan", "cmcourier:CIF": "123"}

    def test_without_catalog_keeps_the_friendly_name(self) -> None:
        props = build_properties(_mapping(with_ids=False), {"BAC_Nombre": "Juan", "BAC_CIF": "1"})
        assert props == {"BAC_Nombre": "Juan", "BAC_CIF": "1"}

    def test_only_required_fields_travel(self) -> None:
        props = build_properties(_mapping(), {"BAC_Nombre": "a", "BAC_CIF": "b", "Otro": "c"})
        assert "Otro" not in props and "c" not in props.values()


class TestDocumentName:
    def test_format(self) -> None:
        assert document_name("CN01", "pdf", _NOW) == "PRUEBA-CN01-20260304-090807.pdf"

    @pytest.mark.parametrize(("fmt", "ext"), [("tiff", ".tif"), ("jpeg", ".jpg"), ("png", ".png")])
    def test_extension_per_format(self, fmt: str, ext: str) -> None:
        assert document_name("CN01", fmt, _NOW).endswith(ext)  # type: ignore[arg-type]


class TestRunPracticeUpload:
    def _draft(self, **kw: object) -> PracticeDraft:
        base = {
            "cm_code": "CN01",
            "mapping": _mapping(),
            "values": {"BAC_Nombre": "Juan", "BAC_CIF": "123"},
            "fmt": "pdf",
            "size_bytes": 20_000,
        }
        base.update(kw)
        return PracticeDraft(**base)  # type: ignore[arg-type]

    def test_generates_uploads_and_cleans_up(self, tmp_path: Path) -> None:
        uploader = _FakeUploader()
        result = run_practice_upload(self._draft(), uploader, workdir=tmp_path, now=_NOW)

        assert uploader.existed_during_upload is True
        assert list(tmp_path.iterdir()) == []  # el temporal se borró
        assert result.name == "PRUEBA-CN01-20260304-090807.pdf"
        assert result.folder == "/cm/CN01"
        assert result.object_type == "D:cmcourier:bacDoc"
        assert result.size_bytes == 20_000
        assert result.size_note == ""
        assert result.response is _OK

    def test_uploader_receives_translated_properties(self, tmp_path: Path) -> None:
        uploader = _FakeUploader()
        run_practice_upload(self._draft(), uploader, workdir=tmp_path, now=_NOW)

        staged, folder, object_type, name, mime, properties = uploader.calls[0]
        assert folder == "/cm/CN01"
        assert object_type == "D:cmcourier:bacDoc"
        assert mime == "application/pdf"
        assert name == staged.path.name
        assert staged.page_count == 1
        assert staged.size_bytes == 20_000
        assert properties == {"cmcourier:Nombre": "Juan", "cmcourier:CIF": "123"}

    def test_tempfile_is_removed_even_when_upload_raises(self, tmp_path: Path) -> None:
        uploader = _FakeUploader(boom=RuntimeError("se cayó la red"))

        with pytest.raises(RuntimeError):
            run_practice_upload(self._draft(), uploader, workdir=tmp_path, now=_NOW)

        assert uploader.existed_during_upload is True
        assert list(tmp_path.iterdir()) == []

    def test_falls_back_to_derived_folder_and_type(self, tmp_path: Path) -> None:
        mapping = CMMapping(
            clase_id="01.01.01.01.01",
            id_rvi="FB01",
            id_corto="CN01",
            clase_name="c",
            required_metadata_fields=(),
        )
        draft = self._draft(mapping=mapping, values={})
        result = run_practice_upload(draft, _FakeUploader(), workdir=tmp_path, now=_NOW)

        assert result.folder == mapping.cm_folder
        assert result.object_type == mapping.cm_object_type

    def test_image_format_travels_with_its_mime(self, tmp_path: Path) -> None:
        uploader = _FakeUploader()
        run_practice_upload(
            self._draft(fmt="png", size_bytes=30_000), uploader, workdir=tmp_path, now=_NOW
        )
        assert uploader.calls[0][4] == "image/png"
        assert uploader.calls[0][3].endswith(".png")

    def test_creates_the_workdir_when_missing(self, tmp_path: Path) -> None:
        workdir = tmp_path / "no" / "existe"
        run_practice_upload(self._draft(), _FakeUploader(), workdir=workdir, now=_NOW)
        assert workdir.is_dir()
