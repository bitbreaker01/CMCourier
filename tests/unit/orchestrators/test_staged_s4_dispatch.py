"""Tests unitarios para el `dispatch` de S4 — directo vs `ProcessPool` (066)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.assembly.pdf_assembler import AssemblyTimings
from cmcourier.domain.models import (
    ClientTrigger,
    CMMapping,
    RVABREPDocument,
    StagedFile,
)
from cmcourier.orchestrators.staged import StagedPipeline, _StageItem

pytestmark = pytest.mark.unit


def _make_doc() -> RVABREPDocument:
    return RVABREPDocument(
        system_code="1",
        txn_num="TXN_S4",
        index1="1",
        index2="1",
        index3="",
        index4="",
        index5="",
        index6="",
        index7="CC03",
        image_type="O",
        image_path="x",
        file_name="DOC.pdf",
        creation_date=datetime(2025, 11, 17),  # noqa: DTZ001
        last_view_date=None,
        total_pages=1,
        delete_code="",
    )


def _make_item() -> _StageItem:
    return _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"),
        document=_make_doc(),
        mapping=CMMapping(
            clase_id="CC03",
            id_rvi="CC03",
            id_corto="CC03",
            clase_name="ClaseTest",
            required_metadata_fields=(),
        ),
    )


def _staged_file(tmp_path) -> StagedFile:
    p = tmp_path / "out.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return StagedFile(path=p, size_bytes=p.stat().st_size, page_count=1)


def _pipeline_with_pool(pool: object | None) -> StagedPipeline:
    return StagedPipeline(
        trigger_strategy=MagicMock(),
        indexing_service=MagicMock(),
        mapping_service=MagicMock(),
        metadata_service=MagicMock(),
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=MagicMock(),
        workers=1,
        s4_process_pool=pool,  # type: ignore[arg-type]
    )


class TestS4DispatchByMode:
    def test_no_pool_calls_assembler_directly(self, tmp_path) -> None:
        # 066+093: con ``s4_process_pool=None``, `_s4_one` debe llamar a
        # ``self._assembler.assemble_traced`` directamente (la variante
        # con timings sub-stage que introdujo 093).
        pipeline = _pipeline_with_pool(None)
        assembler = pipeline._assembler  # MagicMock
        assembler.assemble_traced.return_value = (
            _staged_file(tmp_path),
            AssemblyTimings(path_kind="native_pdf"),
        )
        # Hace que `tracking_store.mark_stage_done` sea no-op y
        # `is_stage_done` devuelva False.
        pipeline._tracking_store.is_stage_done.return_value = False

        item = _make_item()
        survivor, failed = pipeline._s4_one(item, batch_id="B1", rec=pipeline._metrics)

        assert survivor is item
        assert failed is False
        assembler.assemble_traced.assert_called_once_with(item.document)

    def test_pool_provided_dispatches_via_submit(self, tmp_path) -> None:
        # 066+093: con `pool`, `_s4_one` debe usar
        # ``pool.submit(_pool_assemble_traced, doc).result()`` — sin
        # llamar nunca al assembler directamente.
        staged = _staged_file(tmp_path)
        fake_future = MagicMock()
        fake_future.result.return_value = (staged, AssemblyTimings(path_kind="native_pdf"))
        fake_pool = MagicMock()
        fake_pool.submit.return_value = fake_future

        pipeline = _pipeline_with_pool(fake_pool)
        pipeline._tracking_store.is_stage_done.return_value = False

        item = _make_item()
        survivor, failed = pipeline._s4_one(item, batch_id="B1", rec=pipeline._metrics)

        assert survivor is item
        assert failed is False
        # El assembler directo NO debe haber sido llamado.
        pipeline._assembler.assemble_traced.assert_not_called()
        # El `pool` recibió el doc y esperamos `result()`.
        fake_pool.submit.assert_called_once()
        args, _kwargs = fake_pool.submit.call_args
        # El primer arg debe ser la función `_pool_assemble_traced` a
        # nivel de módulo, el segundo el documento.
        from cmcourier.adapters.assembly.pool import _pool_assemble_traced

        assert args[0] is _pool_assemble_traced
        assert args[1] is item.document
        fake_future.result.assert_called_once()

    def test_pool_propagates_assembler_failure(self) -> None:
        # 066: las fallas del proceso `worker` afloran como el mismo
        # tipo de excepción cuando ``Future.result()`` re-lanza, así
        # que el camino de manejo de errores queda igual.
        from cmcourier.domain.exceptions import PDFAssemblyFailedError

        fake_future = MagicMock()
        fake_future.result.side_effect = PDFAssemblyFailedError(txn_num="TXN_S4", reason="boom")
        fake_pool = MagicMock()
        fake_pool.submit.return_value = fake_future

        pipeline = _pipeline_with_pool(fake_pool)
        pipeline._tracking_store.is_stage_done.return_value = False

        item = _make_item()
        survivor, failed = pipeline._s4_one(item, batch_id="B1", rec=pipeline._metrics)

        assert survivor is None
        assert failed is True
        pipeline._tracking_store.mark_stage_failed.assert_called_once()
