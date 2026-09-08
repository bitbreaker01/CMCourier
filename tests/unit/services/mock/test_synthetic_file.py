"""141 REQ-002 (E2) — archivo sintético multi-formato (pdf/tiff/jpeg/png).

Módulo puro: entra ``(fmt, size_bytes, marker)``, salen bytes válidos
del formato pedido con el tamaño aproximado y determinístico.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from cmcourier.services.mock.synthetic_file import (
    MAX_SIZE_BYTES,
    MIN_SIZE_BYTES,
    SyntheticFile,
    build_synthetic_file,
)

pytestmark = pytest.mark.unit

_TARGET = 300_000
_TOLERANCE = 45_000  # ±15 %
_PIL_FORMAT = {"tiff": "TIFF", "jpeg": "JPEG", "png": "PNG"}
_IMAGE_FORMATS = ("tiff", "jpeg", "png")


class TestPdf:
    def test_exact_size_and_header(self) -> None:
        out = build_synthetic_file("pdf", 200_000, "x")
        assert isinstance(out, SyntheticFile)
        assert len(out.content) == 200_000
        assert out.content.startswith(b"%PDF")
        assert out.mime_type == "application/pdf"
        assert out.extension == ".pdf"
        assert out.size_note == ""

    def test_marker_travels_in_the_content_stream(self) -> None:
        out = build_synthetic_file("pdf", 50_000, "PRUEBA-CN01")
        assert b"PRUEBA-CN01" in out.content


class TestImages:
    @pytest.mark.parametrize("fmt", _IMAGE_FORMATS)
    def test_pillow_reopens_with_the_right_format(self, fmt: str) -> None:
        out = build_synthetic_file(fmt, _TARGET, "m")
        with Image.open(BytesIO(out.content)) as img:
            assert img.format == _PIL_FORMAT[fmt]

    @pytest.mark.parametrize("fmt", _IMAGE_FORMATS)
    def test_size_within_tolerance(self, fmt: str) -> None:
        """141 antagonista I7: assert de tolerancia REAL — el ``or
        out.size_note != ""`` original nunca reprobaba (a 300 KB los tres
        formatos entran cómodos en la banda), así que la tolerancia
        jamás se ejercitaba de verdad."""
        out = build_synthetic_file(fmt, _TARGET, "m")
        assert out.size_note == ""
        assert abs(len(out.content) - _TARGET) <= _TOLERANCE

    @pytest.mark.parametrize("fmt", _IMAGE_FORMATS)
    def test_marker_is_actually_drawn_on_the_pixels(
        self, fmt: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """141 antagonista I7: ``TestDeterminism`` sólo prueba que el
        SEED (= marker) cambia los bytes — no que el texto se dibuje. Acá
        se neutraliza ``ImageDraw.text`` con el MISMO seed: si el marker
        realmente se pinta, los bytes tienen que diferir."""
        with_marker = build_synthetic_file(fmt, 20_000, "m").content

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", lambda *a, **k: None)
        without_marker = build_synthetic_file(fmt, 20_000, "m").content

        assert with_marker != without_marker

    @pytest.mark.parametrize(
        ("fmt", "mime", "ext"),
        [
            ("tiff", "image/tiff", ".tif"),
            ("jpeg", "image/jpeg", ".jpg"),
            ("png", "image/png", ".png"),
        ],
    )
    def test_mime_and_extension(self, fmt: str, mime: str, ext: str) -> None:
        out = build_synthetic_file(fmt, MIN_SIZE_BYTES * 4, "m")
        assert (out.mime_type, out.extension) == (mime, ext)


class TestDeterminism:
    @pytest.mark.parametrize("fmt", ["pdf", *_IMAGE_FORMATS])
    def test_same_marker_same_bytes(self, fmt: str) -> None:
        first = build_synthetic_file(fmt, 20_000, "igual")
        second = build_synthetic_file(fmt, 20_000, "igual")
        assert first.content == second.content

    def test_different_marker_different_bytes(self) -> None:
        a = build_synthetic_file("png", 20_000, "uno")
        b = build_synthetic_file("png", 20_000, "dos")
        assert a.content != b.content


class TestBounds:
    @pytest.mark.parametrize("size", [500, MIN_SIZE_BYTES - 1, 60 * 1024 * 1024])
    def test_out_of_range_raises(self, size: int) -> None:
        with pytest.raises(ValueError, match="size_bytes"):
            build_synthetic_file("pdf", size, "x")

    def test_bounds_are_1kb_and_50mb(self) -> None:
        assert MIN_SIZE_BYTES == 1024
        assert MAX_SIZE_BYTES == 50 * 1024 * 1024

    def test_unknown_format_raises(self) -> None:
        with pytest.raises(ValueError, match="formato"):
            build_synthetic_file("gif", 20_000, "x")  # type: ignore[arg-type]
