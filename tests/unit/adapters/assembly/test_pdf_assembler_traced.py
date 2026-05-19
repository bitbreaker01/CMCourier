"""093 — Tests del ``assemble_traced`` con timings sub-stage."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from cmcourier.adapters.assembly.pdf_assembler import (
    AssemblerConfig,
    AssemblyTimings,
    PdfAssembler,
)
from cmcourier.domain.models import RVABREPDocument

pytestmark = pytest.mark.unit


_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
    b" /Resources <<>> /Contents 4 0 R >>\nendobj\n"
    b"4 0 obj\n<< /Length 44 >>\nstream\n"
    b"BT /F1 12 Tf 100 700 Td (Hi) Tj ET\nendstream\nendobj\n"
    b"xref\n0 5\n0000000000 65535 f \n"
    b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
)


def _make_doc(
    *,
    txn_num: str = "T001",
    image_path: str = "001/0001",
    file_name: str = "T001.PDF",
    image_type: str = "O",
    total_pages: int = 1,
) -> RVABREPDocument:
    return RVABREPDocument(
        system_code="1",
        txn_num=txn_num,
        index1="DOC",
        index2="123456",
        index3="",
        index4="",
        index5="",
        index6="",
        index7="FF17",
        image_type=image_type,
        image_path=image_path,
        file_name=file_name,
        creation_date=datetime(2026, 5, 19),
        last_view_date=None,
        total_pages=total_pages,
        delete_code="",
    )


def _setup_native_pdf_fixture(tmp_path: Path) -> tuple[PdfAssembler, RVABREPDocument]:
    src_root = tmp_path / "source"
    temp_dir = tmp_path / "temp"
    src_root.mkdir()
    temp_dir.mkdir()
    doc_dir = src_root / "001" / "0001"
    doc_dir.mkdir(parents=True)
    (doc_dir / "T001.PDF").write_bytes(_MINIMAL_PDF)
    assembler = PdfAssembler(AssemblerConfig(source_root=src_root, temp_dir=temp_dir))
    return assembler, _make_doc()


class TestNativePdfPath:
    def test_traced_returns_tuple_with_timings(self, tmp_path: Path) -> None:
        assembler, doc = _setup_native_pdf_fixture(tmp_path)
        staged, timings = assembler.assemble_traced(doc)
        assert staged.path.exists()
        assert isinstance(timings, AssemblyTimings)

    def test_native_pdf_path_kind(self, tmp_path: Path) -> None:
        assembler, doc = _setup_native_pdf_fixture(tmp_path)
        _, timings = assembler.assemble_traced(doc)
        assert timings.path_kind == "native_pdf"

    def test_native_pdf_relevant_fields_populated(self, tmp_path: Path) -> None:
        assembler, doc = _setup_native_pdf_fixture(tmp_path)
        _, timings = assembler.assemble_traced(doc)
        # Los campos del camino native_pdf deben tener valores > 0.
        assert timings.source_stat_ms > 0
        assert timings.copy_native_ms > 0
        assert timings.dst_stat_ms > 0

    def test_native_pdf_paged_fields_stay_zero(self, tmp_path: Path) -> None:
        assembler, doc = _setup_native_pdf_fixture(tmp_path)
        _, timings = assembler.assemble_traced(doc)
        # Los campos del camino paged NO deben tocarse en native_pdf.
        assert timings.discover_pages_ms == 0.0
        assert timings.encode_pdf_ms == 0.0


class TestAssembleWrapperPreservesAPI:
    def test_assemble_returns_only_staged_file(self, tmp_path: Path) -> None:
        """El método ``assemble`` (sin traced) sigue retornando solo
        ``StagedFile`` — backward-compat."""
        assembler, doc = _setup_native_pdf_fixture(tmp_path)
        result = assembler.assemble(doc)
        # Debe ser un StagedFile, NO una tupla.
        assert not isinstance(result, tuple)
        assert result.path.exists()


class TestAssemblyTimingsDefaults:
    def test_zero_defaults_for_unused_fields(self) -> None:
        t = AssemblyTimings(path_kind="native_pdf", copy_native_ms=15.0)
        assert t.copy_native_ms == 15.0
        assert t.source_stat_ms == 0.0
        assert t.discover_pages_ms == 0.0
        assert t.encode_pdf_ms == 0.0
        assert t.dst_stat_ms == 0.0
