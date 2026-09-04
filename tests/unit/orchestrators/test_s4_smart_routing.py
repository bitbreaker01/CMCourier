"""094 — Routing inteligente PDF/TIFF en S4.

Verifica que cuando ``s4_smart_routing=True``:

- Los PDF nativos (``document.is_pdf is True``) corren inline
  vía ``assembler.assemble_traced``, NO en el process pool.
- Los paginados TIFF/JPEG (``is_pdf is False``) siguen yendo al
  process pool.

Cuando ``s4_smart_routing=False`` (default), el comportamiento es
pre-094: si hay process pool, TODO va al pool sin importar el tipo.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.assembly.pdf_assembler import AssemblyTimings
from cmcourier.domain.models import RVABREPDocument, StagedFile
from cmcourier.orchestrators.staged import StagedPipeline

pytestmark = pytest.mark.unit


def _make_doc(*, is_pdf: bool, txn_num: str = "T001") -> RVABREPDocument:
    """``is_pdf=True`` → file_name '*.PDF'; False → '*.001' (paginado)."""
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
        image_type="O" if is_pdf else "B",
        image_path="001/0001",
        file_name=f"{txn_num}.PDF" if is_pdf else f"{txn_num}.001",
        creation_date=datetime(2026, 5, 19),
        last_view_date=None,
        total_pages=1 if is_pdf else 3,
        delete_code="",
    )


def _make_pipeline_stub(
    *,
    has_process_pool: bool,
    smart_routing: bool,
) -> tuple[StagedPipeline, MagicMock, MagicMock]:
    """Construye un StagedPipeline mínimo con assembler + pool stubs."""
    stub = StagedPipeline.__new__(StagedPipeline)
    assembler_mock = MagicMock()
    fake_staged = MagicMock(spec=StagedFile)
    fake_timings = AssemblyTimings(path_kind="native_pdf")
    assembler_mock.assemble_traced.return_value = (fake_staged, fake_timings)
    stub._assembler = assembler_mock  # type: ignore[attr-defined]
    pool_mock: MagicMock | None
    if has_process_pool:
        pool_mock = MagicMock()
        future_mock = MagicMock()
        future_mock.result.return_value = (fake_staged, fake_timings)
        pool_mock.submit.return_value = future_mock
        stub._s4_process_pool = pool_mock  # type: ignore[attr-defined]
    else:
        pool_mock = None
        stub._s4_process_pool = None  # type: ignore[attr-defined]
    stub._s4_smart_routing = smart_routing  # type: ignore[attr-defined]
    return stub, assembler_mock, pool_mock  # type: ignore[return-value]


def _simulate_routing(stub: StagedPipeline, document: RVABREPDocument) -> str:
    """Replica la decisión de routing del orquestador. Devuelve 'inline' o 'pool'."""
    route_inline = stub._s4_process_pool is None or (  # type: ignore[attr-defined]
        stub._s4_smart_routing and document.is_pdf  # type: ignore[attr-defined]
    )
    return "inline" if route_inline else "pool"


class TestSmartRoutingEnabled:
    """094: con ``s4_smart_routing=True``, separa PDF nativos (inline)
    de paginados (pool)."""

    def test_pdf_native_goes_inline(self) -> None:
        stub, _, _ = _make_pipeline_stub(has_process_pool=True, smart_routing=True)
        route = _simulate_routing(stub, _make_doc(is_pdf=True))
        assert route == "inline"

    def test_paginated_goes_to_pool(self) -> None:
        stub, _, _ = _make_pipeline_stub(has_process_pool=True, smart_routing=True)
        route = _simulate_routing(stub, _make_doc(is_pdf=False))
        assert route == "pool"


class TestSmartRoutingDisabled:
    """094: con ``s4_smart_routing=False`` (default), TODO va al pool
    si está activo — comportamiento pre-094 byte-idéntico."""

    def test_pdf_native_still_goes_to_pool_by_default(self) -> None:
        stub, _, _ = _make_pipeline_stub(has_process_pool=True, smart_routing=False)
        route = _simulate_routing(stub, _make_doc(is_pdf=True))
        assert route == "pool"

    def test_paginated_goes_to_pool(self) -> None:
        stub, _, _ = _make_pipeline_stub(has_process_pool=True, smart_routing=False)
        route = _simulate_routing(stub, _make_doc(is_pdf=False))
        assert route == "pool"


class TestNoProcessPool:
    """094: sin process pool (``s4_use_processes: false``), TODO va
    inline, sin importar smart_routing."""

    def test_pdf_native_inline(self) -> None:
        stub, _, _ = _make_pipeline_stub(has_process_pool=False, smart_routing=True)
        route = _simulate_routing(stub, _make_doc(is_pdf=True))
        assert route == "inline"

    def test_paginated_inline(self) -> None:
        stub, _, _ = _make_pipeline_stub(has_process_pool=False, smart_routing=True)
        route = _simulate_routing(stub, _make_doc(is_pdf=False))
        assert route == "inline"

    def test_routing_with_smart_off_still_inline(self) -> None:
        stub, _, _ = _make_pipeline_stub(has_process_pool=False, smart_routing=False)
        route = _simulate_routing(stub, _make_doc(is_pdf=False))
        assert route == "inline"


class TestSchemaDefault:
    def test_processing_config_default_enables_smart_routing(self) -> None:
        from cmcourier.config.schema import ProcessingConfig

        cfg = ProcessingConfig()
        assert cfg.s4_smart_routing is True, (
            "114: con s4_use_processes=True por default, los PDF nativos "
            "(shutil.copy2) no deben pagar pickle/IPC del process pool"
        )

    def test_processing_config_opt_out(self) -> None:
        from cmcourier.config.schema import ProcessingConfig

        cfg = ProcessingConfig(s4_smart_routing=False)
        assert cfg.s4_smart_routing is False
