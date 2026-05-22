"""Tests del modo de contenido sintético del PdfAssembler (102, REQ-001/007/008)."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path

import pytest
from PyPDF2 import PdfReader

from cmcourier.adapters.assembly.pdf_assembler import AssemblerConfig, PdfAssembler
from cmcourier.domain.exceptions import PDFAssemblyFailedError
from cmcourier.domain.models import RVABREPDocument
from cmcourier.services.mock.synthetic_content import (
    SizeBand,
    SizeMix,
    SyntheticPdfProvider,
)

pytestmark = pytest.mark.unit

_SMALL_MIX = SizeMix(bands=(SizeBand("small", 1.0, 40_000, 80_000),))


def _make_doc(*, txn_num: str = "T001", image_type: str = "O") -> RVABREPDocument:
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
        image_path="001/0001",
        file_name=f"{txn_num}.PDF" if image_type == "O" else f"{txn_num}.001",
        creation_date=datetime(2026, 5, 22),
        last_view_date=None,
        total_pages=1,
        delete_code="",
    )


def _synthetic_assembler(tmp_path: Path) -> PdfAssembler:
    # source_root apunta a un dir que ni existe — el modo sintético no lo
    # toca. ``PdfAssembler.__init__`` crea el temp_dir (con parents).
    return PdfAssembler(
        AssemblerConfig(
            source_root=tmp_path,
            temp_dir=tmp_path / "staging",
            synthetic_provider=SyntheticPdfProvider(size_mix=_SMALL_MIX, seed=1),
        )
    )


def test_synthetic_mode_generates_without_a_source_file(tmp_path: Path) -> None:
    """REQ-001/005: el PDF se genera on-the-fly; no se lee ningún archivo fuente."""
    assembler = _synthetic_assembler(tmp_path)
    staged = assembler.assemble(_make_doc())
    assert staged.path.is_file()
    assert staged.size_bytes > 0


def test_synthetic_output_is_a_valid_single_page_pdf(tmp_path: Path) -> None:
    assembler = _synthetic_assembler(tmp_path)
    staged = assembler.assemble(_make_doc())
    reader = PdfReader(BytesIO(staged.path.read_bytes()))
    assert len(reader.pages) == 1


def test_synthetic_content_is_deterministic(tmp_path: Path) -> None:
    """REQ-003: misma txn → mismo contenido."""
    first = _synthetic_assembler(tmp_path / "a")
    second = _synthetic_assembler(tmp_path / "b")
    bytes_a = first.assemble(_make_doc(txn_num="TXNZ")).path.read_bytes()
    bytes_b = second.assemble(_make_doc(txn_num="TXNZ")).path.read_bytes()
    assert bytes_a == bytes_b


def test_synthetic_mode_rejects_non_pdf_row(tmp_path: Path) -> None:
    """REQ-008: una fila TIFF/JPEG con modo sintético activo es error claro."""
    assembler = _synthetic_assembler(tmp_path)
    with pytest.raises(PDFAssemblyFailedError, match="pdf:100"):
        assembler.assemble(_make_doc(txn_num="T002", image_type="B"))


def test_path_kind_is_synthetic(tmp_path: Path) -> None:
    assembler = _synthetic_assembler(tmp_path)
    _, timings = assembler.assemble_traced(_make_doc())
    assert timings.path_kind == "synthetic_pdf"
