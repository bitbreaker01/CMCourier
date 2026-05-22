"""Tests del proveedor de contenido sintético on-the-fly (102)."""

from __future__ import annotations

import time
from io import BytesIO

import pytest
from PyPDF2 import PdfReader

from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.services.mock.synthetic_content import (
    DEFAULT_SIZE_MIX,
    SizeBand,
    SizeMix,
    SyntheticPdfProvider,
    build_synthetic_pdf,
)

pytestmark = pytest.mark.unit


class TestBuildSyntheticPdf:
    """REQ-001: PDF de una página, estructuralmente válido."""

    def test_starts_with_pdf_header_and_ends_with_eof(self) -> None:
        pdf = build_synthetic_pdf(50_000, "TAB3K7Q")
        assert pdf.startswith(b"%PDF-")
        assert pdf.rstrip().endswith(b"%%EOF")

    def test_is_a_valid_single_page_pdf(self) -> None:
        pdf = build_synthetic_pdf(120_000, "TAB3K7Q")
        reader = PdfReader(BytesIO(pdf))
        assert len(reader.pages) == 1

    def test_total_size_is_close_to_requested(self) -> None:
        pdf = build_synthetic_pdf(200_000, "TAB3K7Q")
        # El overhead estructural del PDF es chico y fijo; el tamaño total
        # debe quedar muy cerca del pedido.
        assert 200_000 <= len(pdf) <= 200_000 + 2_048

    def test_embeds_the_txn_marker(self) -> None:
        # REQ-006: contenido no byte-idéntico — el txn viaja adentro.
        pdf = build_synthetic_pdf(50_000, "TXN-UNIQUE-123")
        assert b"TXN-UNIQUE-123" in pdf

    def test_different_markers_yield_different_bytes(self) -> None:
        a = build_synthetic_pdf(50_000, "TXN-A")
        b = build_synthetic_pdf(50_000, "TXN-B")
        assert a != b


class TestSizeMix:
    def test_rejects_empty_mix(self) -> None:
        with pytest.raises(ConfigurationError, match="at least one"):
            SizeMix(bands=())

    def test_rejects_all_zero_weights(self) -> None:
        with pytest.raises(ConfigurationError, match="weights cannot all be zero"):
            SizeMix(bands=(SizeBand("z", 0.0, 10, 20),))

    def test_rejects_inverted_band(self) -> None:
        with pytest.raises(ConfigurationError, match="min_bytes"):
            SizeBand("bad", 1.0, 100, 10)

    def test_distribution_is_roughly_respected(self) -> None:
        mix = SizeMix(
            bands=(
                SizeBand("small", 50.0, 1_000, 1_000),
                SizeBand("large", 50.0, 300_000, 300_000),
            )
        )
        provider = SyntheticPdfProvider(size_mix=mix, seed=1)
        small = sum(1 for i in range(1000) if len(provider.generate(f"T{i}")) < 100_000)
        # 50/50 ± margen amplio para no ser flaky.
        assert 400 <= small <= 600


class TestSyntheticPdfProvider:
    """REQ-003: tamaño determinístico por txn_num."""

    def test_same_txn_yields_identical_bytes(self) -> None:
        p1 = SyntheticPdfProvider(seed=42)
        p2 = SyntheticPdfProvider(seed=42)
        assert p1.generate("TAB3K7Q") == p2.generate("TAB3K7Q")

    def test_different_seed_changes_output(self) -> None:
        a = SyntheticPdfProvider(seed=1).generate("TAB3K7Q")
        b = SyntheticPdfProvider(seed=2).generate("TAB3K7Q")
        assert a != b

    def test_generates_valid_pdf(self) -> None:
        pdf = SyntheticPdfProvider(seed=7).generate("TAB3K7Q")
        assert len(PdfReader(BytesIO(pdf)).pages) == 1

    def test_mix_spans_size_classes(self) -> None:
        mix = SizeMix(
            bands=(
                SizeBand("tiny", 1.0, 5_000, 5_000),
                SizeBand("big", 1.0, 3_000_000, 3_000_000),
            )
        )
        provider = SyntheticPdfProvider(size_mix=mix, seed=3)
        sizes = {len(provider.generate(f"T{i}")) for i in range(50)}
        assert max(sizes) - min(sizes) > 1_000_000

    def test_default_mix_is_usable(self) -> None:
        pdf = SyntheticPdfProvider(size_mix=DEFAULT_SIZE_MIX, seed=0).generate("T1")
        assert pdf.startswith(b"%PDF-")

    def test_provider_is_picklable(self) -> None:
        # El provider cruza a subprocesos vía el process pool de S4.
        import pickle

        provider = SyntheticPdfProvider(size_mix=DEFAULT_SIZE_MIX, seed=9)
        restored: SyntheticPdfProvider = pickle.loads(pickle.dumps(provider))
        assert restored.generate("T1") == provider.generate("T1")


class TestPerformance:
    """REQ-004: la generación no es el cuello de botella."""

    def test_large_pdf_generates_fast(self) -> None:
        start = time.perf_counter()
        build_synthetic_pdf(20_000_000, "TAB3K7Q")
        # Un PDF de 20 MB: sólo relleno de buffer. Holgadísimo techo.
        assert time.perf_counter() - start < 0.5

    def test_many_small_docs_generate_fast(self) -> None:
        mix = SizeMix(bands=(SizeBand("small", 1.0, 40_000, 80_000),))
        provider = SyntheticPdfProvider(size_mix=mix, seed=0)
        start = time.perf_counter()
        for i in range(500):
            provider.generate(f"T{i}")
        assert time.perf_counter() - start < 1.0
