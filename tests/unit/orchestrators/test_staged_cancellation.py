"""Tests de cancelación cooperativa en StagedPipeline (097).

Con el `cancel_token` prendido, los métodos per-doc saltean el trabajo
sin contar fallas — eso habilita el *drain* cuando el operador cancela
desde el TUI.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from cmcourier.domain.models import (
    ClientTrigger,
    CMMapping,
    ResolvedMetadata,
    RVABREPDocument,
    StagedFile,
)
from cmcourier.orchestrators.staged import StagedPipeline, _StageItem
from cmcourier.services.cancellation import CancellationToken

pytestmark = pytest.mark.unit


def _pipeline() -> StagedPipeline:
    return StagedPipeline(
        trigger_strategy=MagicMock(),
        indexing_service=MagicMock(),
        mapping_service=MagicMock(),
        metadata_service=MagicMock(),
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=MagicMock(),
        workers=1,
    )


def _doc() -> RVABREPDocument:
    return RVABREPDocument(
        system_code="1",
        txn_num="TXN_C",
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


def _mapping() -> CMMapping:
    return CMMapping(
        clase_id="CC03",
        id_rvi="CC03",
        id_corto="CC03",
        clase_name="ClaseTest",
        required_metadata_fields=(),
    )


def _full_item(tmp_path) -> _StageItem:
    """Item con mapping + metadata + staged_file — listo para _upload_one."""
    p = tmp_path / "out.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"),
        document=_doc(),
        mapping=_mapping(),
        metadata=ResolvedMetadata.from_dict({}),
        staged_file=StagedFile(path=p, size_bytes=p.stat().st_size, page_count=1),
    )


def test_cancel_token_property_exposes_the_token() -> None:
    pipeline = _pipeline()
    assert isinstance(pipeline.cancel_token, CancellationToken)
    assert pipeline.cancel_token.is_cancelled() is False


def test_upload_one_skips_when_cancelled(tmp_path) -> None:
    pipeline = _pipeline()
    item = _full_item(tmp_path)
    pipeline.cancel_token.cancel()

    outcome = pipeline._upload_one(item, "B1")

    assert outcome == "skipped"
    pipeline._uploader.upload.assert_not_called()  # no se subió nada


def test_s2_one_skips_when_cancelled() -> None:
    pipeline = _pipeline()
    item = _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"),
        document=_doc(),
    )
    pipeline.cancel_token.cancel()

    survivor, counted_failure = pipeline._s2_one(item, "B1", pipeline._metrics)

    assert survivor is None
    assert counted_failure is False  # cancelar NO cuenta como falla
    pipeline._mapping_service.get_mapping.assert_not_called()


def test_s3_one_skips_when_cancelled() -> None:
    pipeline = _pipeline()
    item = _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"),
        document=_doc(),
        mapping=_mapping(),
    )
    pipeline.cancel_token.cancel()

    survivor, counted_failure = pipeline._s3_one(item, "B1", pipeline._metrics)

    assert survivor is None
    assert counted_failure is False


def test_s4_one_skips_when_cancelled() -> None:
    pipeline = _pipeline()
    item = _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"),
        document=_doc(),
        mapping=_mapping(),
    )
    pipeline.cancel_token.cancel()

    survivor, counted_failure = pipeline._s4_one(item, "B1", pipeline._metrics)

    assert survivor is None
    assert counted_failure is False
    pipeline._assembler.assemble_traced.assert_not_called()


def test_not_cancelled_upload_one_is_unaffected(tmp_path) -> None:
    """Sin cancelar, _upload_one sigue su curso normal (sube el doc)."""
    pipeline = _pipeline()
    item = _full_item(tmp_path)
    pipeline._tracking_store.is_stage_done.return_value = False
    pipeline._uploader.upload.return_value = "cm-obj-1"

    outcome = pipeline._upload_one(item, "B1")

    assert outcome == "done"
    pipeline._uploader.upload.assert_called_once()
