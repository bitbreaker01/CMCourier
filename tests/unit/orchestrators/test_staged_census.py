"""148 REQ-004 — el censo: TODO camino escribe su fila con su razón.

El rastreo de la spec encontró 19 caminos por los que un documento del
origen termina sin subirse. Dos dejaban una razón legible por máquina y
**ocho no escribían absolutamente nada**. Esta suite es el candado de
esos ocho más el de las fallas que ya existían pero colapsaban en
``Sn_FAILED`` con texto libre.

Un ``MagicMock`` como tracking store a propósito: lo que se está
probando es QUÉ le pide el orquestador al puerto (status + reason_code +
txn), no cómo lo persiste SQLite — eso tiene su propia suite de
integración.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.domain.exceptions import (
    CMISClientError,
    CMISServerError,
    IdentityResolutionError,
    IDRViNotMappedError,
    IndexingError,
    PDFAssemblyFailedError,
    RetriesExhaustedError,
    RVABREPNotFoundError,
    SourceFailedError,
    SourceFileMissingError,
)
from cmcourier.domain.models import (
    ClientTrigger,
    CMMapping,
    ExcludedTrigger,
    MigrationRecord,
    ReasonBucket,
    ReasonCode,
    ResolvedMetadata,
    RVABREPDocument,
    RvabrepRowTrigger,
    StagedFile,
    StageStatus,
)
from cmcourier.orchestrators.staged import StagedPipeline, _StageItem
from cmcourier.services.indexing import EnrichOutcome

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pipeline() -> StagedPipeline:
    pipeline = StagedPipeline(
        trigger_strategy=MagicMock(),
        indexing_service=MagicMock(),
        mapping_service=MagicMock(),
        metadata_service=MagicMock(),
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=MagicMock(),
        workers=1,
    )
    pipeline._tracking_store.is_stage_done.return_value = False
    pipeline._tracking_store.is_uploaded.return_value = False
    pipeline._tracking_store.list_docs_for_batch.return_value = []
    return pipeline


def _doc(txn: str = "TXN1", id_rvi: str = "CC03") -> RVABREPDocument:
    return RVABREPDocument(
        system_code="1",
        txn_num=txn,
        index1="SN",
        index2="123456",
        index3="",
        index4="",
        index5="",
        index6="",
        index7=id_rvi,
        image_type="O",
        image_path="x",
        file_name=f"{txn}.pdf",
        creation_date=datetime(2025, 11, 17),  # noqa: DTZ001
        last_view_date=None,
        total_pages=1,
        delete_code="",
    )


def _client_trigger() -> ClientTrigger:
    return ClientTrigger(shortname="SN", cif="123456", system_id="1")


def _mapping() -> CMMapping:
    return CMMapping(
        clase_id="CC03",
        id_rvi="CC03",
        id_corto="CC03",
        clase_name="ClaseTest",
        required_metadata_fields=(),
    )


def _item(pipeline: StagedPipeline, **over: Any) -> _StageItem:
    base: dict[str, Any] = {"trigger": _client_trigger(), "document": _doc()}
    base.update(over)
    return _StageItem(**base)


def _terminal_calls(pipeline: StagedPipeline) -> list[tuple[Any, ...]]:
    """``(txn, batch_id, status, message, reason_code)`` de cada escritura terminal."""
    out: list[tuple[Any, ...]] = []
    for name in ("mark_stage_terminal", "mark_stage_failed"):
        for call in getattr(pipeline._tracking_store, name).call_args_list:
            out.append((*call.args, call.kwargs.get("reason_code")))
    return out


def _reasons_by_txn(pipeline: StagedPipeline) -> dict[str, ReasonCode | None]:
    return {c[0]: c[-1] for c in _terminal_calls(pipeline)}


def _statuses_by_txn(pipeline: StagedPipeline) -> dict[str, StageStatus]:
    return {c[0]: c[2] for c in _terminal_calls(pipeline)}


def _pending_records(pipeline: StagedPipeline) -> list[MigrationRecord]:
    return [c.args[0] for c in pipeline._tracking_store.mark_stage_pending.call_args_list]


# ---------------------------------------------------------------------------
# S0/S1 — los caminos mudos del escaneo
# ---------------------------------------------------------------------------


class TestS0S1Census148:
    def test_excluded_trigger_from_s0_is_recorded_and_never_reaches_s2(self) -> None:
        pipeline = _pipeline()
        excluded = ExcludedTrigger(
            reason_code=ReasonCode.EXCLUDED_BY_FILTER,
            txn_num="TXN_X",
            id_rvi="AA01",
            file_name="x.pdf",
            shortname="SN",
            cif="123456",
            system_id="1",
        )

        items, _skipped, filtered = pipeline._stage_s0_s1([excluded], "B1", None)

        assert items == []  # no fluye a S2
        assert filtered == 1
        assert _reasons_by_txn(pipeline)["TXN_X"] is ReasonCode.EXCLUDED_BY_FILTER
        assert _statuses_by_txn(pipeline)["TXN_X"] is StageStatus.S1_FILTERED
        pipeline._indexing_service.enrich_census.assert_not_called()

    def test_excluded_row_carries_id_rvi_so_the_census_can_group_by_code(self) -> None:
        pipeline = _pipeline()
        excluded = ExcludedTrigger(
            reason_code=ReasonCode.SOURCE_ROW_INCOMPLETE, txn_num="TXN_Y", id_rvi="BB02"
        )

        pipeline._stage_s0_s1([excluded], "B1", None)

        assert [r.id_rvi for r in _pending_records(pipeline)] == ["BB02"]

    def test_rvabrep_not_found_writes_source_row_not_found(self) -> None:
        pipeline = _pipeline()
        pipeline._indexing_service.enrich_census.side_effect = RVABREPNotFoundError(
            shortname="SN", system_id="1"
        )

        pipeline._stage_s0_s1([_client_trigger()], "B1", None)

        assert list(_reasons_by_txn(pipeline).values()) == [ReasonCode.SOURCE_ROW_NOT_FOUND]

    def test_indexing_error_writes_indexing_failed(self) -> None:
        pipeline = _pipeline()
        pipeline._indexing_service.enrich_census.side_effect = IndexingError("boom")

        pipeline._stage_s0_s1([_client_trigger()], "B1", None)

        assert list(_reasons_by_txn(pipeline).values()) == [ReasonCode.INDEXING_FAILED]

    def test_deleted_rows_get_one_row_each_not_one_collapsed(self) -> None:
        """La colisión de la clave sintética ``FILTERED__{sn}__{sys}``.

        N documentos borrados del mismo cliente colapsaban en 1 fila
        contra el índice único ``(rvabrep_txn_num, batch_id)`` con
        ``INSERT OR IGNORE``.
        """
        pipeline = _pipeline()
        deleted = tuple(
            ExcludedTrigger(
                reason_code=ReasonCode.DELETED_AT_SOURCE,
                txn_num=txn,
                id_rvi="FF17",
                shortname="SN",
                system_id="1",
            )
            for txn in ("TXN_D1", "TXN_D2", "TXN_D3")
        )
        pipeline._indexing_service.enrich_census.return_value = EnrichOutcome(
            documents=(), excluded=deleted
        )

        _items, _skipped, filtered = pipeline._stage_s0_s1([_client_trigger()], "B1", None)

        assert filtered == 3
        assert {r.rvabrep_txn_num for r in _pending_records(pipeline)} == {
            "TXN_D1",
            "TXN_D2",
            "TXN_D3",
        }
        assert set(_reasons_by_txn(pipeline).values()) == {ReasonCode.DELETED_AT_SOURCE}

    def test_cross_batch_skip_gets_already_uploaded(self) -> None:
        pipeline = _pipeline()
        pipeline._tracking_store.is_uploaded.return_value = True
        pipeline._indexing_service.enrich_census.return_value = EnrichOutcome(
            documents=(_doc("TXN_S"),), excluded=()
        )

        _items, skipped, _filtered = pipeline._stage_s0_s1([_client_trigger()], "B1", None)

        assert skipped == 1
        assert _reasons_by_txn(pipeline)["TXN_S"] is ReasonCode.ALREADY_UPLOADED
        assert _statuses_by_txn(pipeline)["TXN_S"] is StageStatus.S1_SKIPPED

    def test_out_of_resume_scope_gets_its_reason(self) -> None:
        pipeline = _pipeline()
        pipeline._indexing_service.enrich_census.return_value = EnrichOutcome(
            documents=(_doc("TXN_OOS"),), excluded=()
        )

        items, _skipped, _filtered = pipeline._stage_s0_s1(
            [_client_trigger()], "B1", resume_scope={"OTHER"}
        )

        assert items == []
        assert _reasons_by_txn(pipeline)["TXN_OOS"] is ReasonCode.OUT_OF_SCOPE_RESUME

    def test_cancellation_at_s1_records_the_trigger_in_hand(self) -> None:
        pipeline = _pipeline()
        pipeline.cancel_token.cancel()

        pipeline._stage_s0_s1([_client_trigger()], "B1", None)

        assert list(_reasons_by_txn(pipeline).values()) == [ReasonCode.CANCELLED]

    def test_denominator_counts_every_source_document(self) -> None:
        """REQ-005: el total contra el que todo lo demás tiene que sumar."""
        pipeline = _pipeline()
        pipeline._indexing_service.enrich_census.return_value = EnrichOutcome(
            documents=(_doc("TXN_A"), _doc("TXN_B")),
            excluded=(ExcludedTrigger(reason_code=ReasonCode.DELETED_AT_SOURCE, txn_num="TXN_C"),),
        )
        excluded_at_s0 = ExcludedTrigger(reason_code=ReasonCode.EXCLUDED_BY_FILTER, txn_num="TXN_D")

        pipeline._stage_s0_s1([_client_trigger(), excluded_at_s0], "B1", None)

        total = sum(
            c.args[1] for c in pipeline._tracking_store.increment_source_total.call_args_list
        )
        assert total == 4  # 2 vivos + 1 borrado + 1 excluido por filtro

    def test_a_document_already_in_the_batch_is_not_counted_twice(self) -> None:
        pipeline = _pipeline()
        pipeline._tracking_store.is_stage_done.return_value = True
        pipeline._indexing_service.enrich_census.return_value = EnrichOutcome(
            documents=(_doc("TXN_A"),), excluded=()
        )

        pipeline._stage_s0_s1([_client_trigger()], "B1", None)

        pipeline._tracking_store.increment_source_total.assert_not_called()

    def test_row_trigger_records_real_txn_never_a_synthetic_key(self) -> None:
        pipeline = _pipeline()
        pipeline._indexing_service.txn_num_of.return_value = "TXN_REAL"
        pipeline._indexing_service.enrich_census.side_effect = RVABREPNotFoundError(
            shortname="SN", system_id="1"
        )
        trigger = RvabrepRowTrigger(row={"ABABCD": "SN", "ABAACD": "1"})

        pipeline._stage_s0_s1([trigger], "B1", None)

        assert "TXN_REAL" in _reasons_by_txn(pipeline)


# ---------------------------------------------------------------------------
# S2..S5 — las fallas que ya existían, ahora con código
# ---------------------------------------------------------------------------


class TestStageReasons148:
    def test_id_rvi_not_mapped_is_code_not_mapped(self) -> None:
        pipeline = _pipeline()
        pipeline._mapping_service.get_mapping.side_effect = IDRViNotMappedError(id_rvi="CC03")
        pipeline._mapping_service.missing_from_manifest.return_value = False

        pipeline._s2_one(_item(pipeline), "B1", pipeline._metrics)

        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.CODE_NOT_MAPPED

    def test_identity_unresolved_is_distinguishable_from_code_not_mapped(self) -> None:
        """Hoy las dos colapsan en ``S2_FAILED`` con texto libre; el balde
        es el mismo pero la acción del operador NO."""
        pipeline = _pipeline()
        pipeline._mapping_service.get_mapping.side_effect = IdentityResolutionError(
            slot="cif", field_name="BAC_CIF", reason="sin resultado", chain=()
        )

        pipeline._s2_one(_item(pipeline), "B1", pipeline._metrics)

        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.IDENTITY_UNRESOLVED

    def test_code_whose_idcm_is_not_in_the_manifest_is_type_not_in_manifest(self) -> None:
        pipeline = _pipeline()
        pipeline._mapping_service.get_mapping.side_effect = IDRViNotMappedError(id_rvi="CC03")
        pipeline._mapping_service.missing_from_manifest.return_value = True

        pipeline._s2_one(_item(pipeline), "B1", pipeline._metrics)

        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.TYPE_NOT_IN_MANIFEST

    def test_s3_failure_is_metadata_unresolved(self) -> None:
        pipeline = _pipeline()
        pipeline._metadata_service.resolve.side_effect = SourceFailedError(
            field_name="BAC_CIF", source="as400"
        )

        pipeline._s3_one(_item(pipeline, mapping=_mapping()), "B1", pipeline._metrics)

        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.METADATA_UNRESOLVED

    def test_s4_missing_file_is_source_file_missing(self) -> None:
        pipeline = _pipeline()
        pipeline._assembler.assemble_traced.side_effect = SourceFileMissingError(
            file_path="/x/y.001"
        )

        pipeline._s4_one(_item(pipeline, mapping=_mapping()), "B1", pipeline._metrics)

        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.SOURCE_FILE_MISSING

    def test_s4_broken_pdf_is_assembly_failed(self) -> None:
        pipeline = _pipeline()
        pipeline._assembler.assemble_traced.side_effect = PDFAssemblyFailedError(
            txn_num="TXN1", reason="roto"
        )

        pipeline._s4_one(_item(pipeline, mapping=_mapping()), "B1", pipeline._metrics)

        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.ASSEMBLY_FAILED

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (CMISClientError(status_code=409), ReasonCode.CM_REJECTED_4XX),
            (CMISServerError(status_code=503), ReasonCode.CM_ERROR_5XX),
        ],
    )
    def test_cm_reasons_come_from_classify_failure(
        self, tmp_path: Any, exc: Exception, expected: ReasonCode
    ) -> None:
        """104 ya produce exactamente este enum y sólo alimentaba métricas:
        se conecta a la base, no se reinventa."""
        pipeline = _pipeline()
        item = _uploadable(tmp_path)
        pipeline._uploader.upload.side_effect = exc

        assert pipeline._upload_one(item, "B1") == "failed"
        assert _reasons_by_txn(pipeline)["TXN1"] is expected

    def test_timeout_unwrapped_from_retries_exhausted_is_cm_timeout(self, tmp_path: Any) -> None:
        pipeline = _pipeline()
        item = _uploadable(tmp_path)

        # ``classify_failure`` mira el NOMBRE de la clase para no acoplarse
        # a httpx; el doble se llama igual que la excepción real.
        class ConnectTimeout(Exception):  # noqa: N818 — espeja el nombre de httpx
            pass

        exhausted = RetriesExhaustedError(txn_num="TXN1", attempts=3)
        exhausted.__cause__ = ConnectTimeout()
        pipeline._uploader.upload.side_effect = exhausted

        assert pipeline._upload_one(item, "B1") == "failed"
        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.CM_TIMEOUT

    def test_lost_claim_ends_terminal_with_claim_lost(self, tmp_path: Any) -> None:
        pipeline = _pipeline()
        item = _uploadable(tmp_path)
        pipeline._coordinator = MagicMock()
        pipeline._coordinator.try_claim.return_value = False

        assert pipeline._upload_one(item, "B1") == "skipped"
        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.CLAIM_LOST
        assert _statuses_by_txn(pipeline)["TXN1"] is StageStatus.S5_FAILED

    @pytest.mark.parametrize("stage", ["_s2_one", "_s3_one", "_s4_one"])
    def test_cancellation_writes_a_terminal_row(self, stage: str) -> None:
        pipeline = _pipeline()
        pipeline.cancel_token.cancel()

        getattr(pipeline, stage)(_item(pipeline, mapping=_mapping()), "B1", pipeline._metrics)

        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.CANCELLED

    def test_cancelled_upload_writes_a_terminal_row(self, tmp_path: Any) -> None:
        pipeline = _pipeline()
        item = _uploadable(tmp_path)
        pipeline.cancel_token.cancel()

        assert pipeline._upload_one(item, "B1") == "skipped"
        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.CANCELLED


class TestCrashCensus148:
    """Los ``except BaseException`` de streaming: el bug de los ~200 uploads.

    La excepción no-CMIS se contaba en el tally y no persistía nada, y
    como ``mark_stage_pending`` es ``INSERT OR IGNORE`` el documento
    quedaba registrado como ``S4_DONE`` — subido a los ojos del reporte,
    nunca subido de verdad.
    """

    def test_upload_crash_ends_terminal_not_stranded_in_pending(self, tmp_path: Any) -> None:
        pipeline = _pipeline()
        item = _uploadable(tmp_path)

        pipeline.record_upload_crash(item, "B1", MemoryError("no contemplado"))

        assert _reasons_by_txn(pipeline)["TXN1"] is ReasonCode.CRASHED
        assert _statuses_by_txn(pipeline)["TXN1"] is StageStatus.S5_FAILED

    def test_prep_crash_after_s1_writes_the_reason_without_recounting(self) -> None:
        pipeline = _pipeline()
        pipeline._tracking_store.is_stage_done.return_value = True  # S1 ya lo contó
        pipeline._indexing_service.txn_num_of.return_value = "TXN_P"
        trigger = RvabrepRowTrigger(row={"ABABCD": "SN", "ABAACD": "1"})

        pipeline.record_prep_crash(trigger, "B1", MemoryError("no contemplado"))

        assert _reasons_by_txn(pipeline)["TXN_P"] is ReasonCode.CRASHED
        pipeline._tracking_store.increment_source_total.assert_not_called()

    def test_prep_crash_before_s1_counts_the_document(self) -> None:
        """Si reventó antes de que S1 lo contara, el denominador lo suma
        acá — si no, el censo nunca cerraría."""
        pipeline = _pipeline()
        pipeline._indexing_service.txn_num_of.return_value = "TXN_P"
        trigger = RvabrepRowTrigger(row={"ABABCD": "SN", "ABAACD": "1"})

        pipeline.record_prep_crash(trigger, "B1", MemoryError("no contemplado"))

        pipeline._tracking_store.increment_source_total.assert_called_once_with("B1", 1)

    def test_a_failing_tracking_store_never_kills_the_worker(self, tmp_path: Any) -> None:
        pipeline = _pipeline()
        item = _uploadable(tmp_path)
        pipeline._tracking_store.mark_stage_terminal.side_effect = RuntimeError("caído")

        pipeline.record_upload_crash(item, "B1", MemoryError("no contemplado"))


def _uploadable(tmp_path: Any) -> _StageItem:
    p = tmp_path / "out.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return _StageItem(
        trigger=_client_trigger(),
        document=_doc(),
        mapping=_mapping(),
        metadata=ResolvedMetadata.from_dict({}),
        staged_file=StagedFile(path=p, size_bytes=p.stat().st_size, page_count=1),
    )


# ---------------------------------------------------------------------------
# id_rvi — se llena SIEMPRE, no sólo en las exclusiones
# ---------------------------------------------------------------------------


class TestIdRviAlwaysPersisted148:
    def test_build_record_carries_id_rvi(self) -> None:
        pipeline = _pipeline()
        item = _item(pipeline)

        record = pipeline._build_record(item, "B1", StageStatus.S1_PENDING)

        assert record.id_rvi == "CC03"

    def test_a_document_that_uploads_has_no_reason(self, tmp_path: Any) -> None:
        """WP1 avisó: una razón sobre un doc que después sube lo hace contar
        en el censo Y en los subidos."""
        pipeline = _pipeline()
        item = _uploadable(tmp_path)
        pipeline._uploader.upload.return_value = "cm-1"

        assert pipeline._upload_one(item, "B1") == "done"
        assert _terminal_calls(pipeline) == []


# ---------------------------------------------------------------------------
# Los baldes, sobre los códigos que WP2 efectivamente escribe
# ---------------------------------------------------------------------------


def test_every_reason_wp2_writes_has_a_bucket() -> None:
    written = {
        ReasonCode.EXCLUDED_BY_FILTER,
        ReasonCode.SOURCE_ROW_INCOMPLETE,
        ReasonCode.DELETED_AT_SOURCE,
        ReasonCode.ALREADY_UPLOADED,
        ReasonCode.OUT_OF_SCOPE_RESUME,
        ReasonCode.SOURCE_ROW_NOT_FOUND,
        ReasonCode.INDEXING_FAILED,
        ReasonCode.CODE_NOT_MAPPED,
        ReasonCode.TYPE_NOT_IN_MANIFEST,
        ReasonCode.IDENTITY_UNRESOLVED,
        ReasonCode.METADATA_UNRESOLVED,
        ReasonCode.SOURCE_FILE_MISSING,
        ReasonCode.ASSEMBLY_FAILED,
        ReasonCode.CM_REJECTED_4XX,
        ReasonCode.CM_ERROR_5XX,
        ReasonCode.CM_TIMEOUT,
        ReasonCode.CM_TRANSPORT,
        ReasonCode.CLAIM_LOST,
        ReasonCode.CRASHED,
        ReasonCode.CANCELLED,
        # 150 REQ-004: lo escribe S2, después de resolver la identidad.
        ReasonCode.CLIENT_NOT_ACTIVE,
    }
    assert written == set(ReasonCode)
    assert all(isinstance(c.bucket, ReasonBucket) for c in written)
