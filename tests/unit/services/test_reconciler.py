"""Tests del As400Reconciler + PendingSyncBuffer + PeriodicReconciler (096).

El reconciliador es lógica de `dispatch` pura — los dos stores se
mockean con :class:`MagicMock`. ``read_state`` devuelve un
:class:`NiarvilogRow` o ``None`` según el escenario.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.tracking.as400_niarvilog import NiarvilogRow
from cmcourier.domain.models import (
    CMMapping,
    MigrationRecord,
    RVABREPDocument,
    StageStatus,
    TriggerRecord,
)
from cmcourier.services.reconciler import (
    As400Reconciler,
    PendingSyncBuffer,
    PendingSyncItem,
    PeriodicReconciler,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item(
    *,
    txn: str = "0000001",
    outcome: str = "uploaded",
    cm_object_id: str | None = "cm-aaa",
    error: str | None = None,
) -> PendingSyncItem:
    trigger = TriggerRecord(shortname="TESTCLIENT01", cif="123456", system_id="1")
    document = RVABREPDocument(
        system_code="1",
        txn_num=txn,
        index1="",
        index2="123456",
        index3="",
        index4="",
        index5="",
        index6="",
        index7="CC03",
        image_type="B",
        image_path="paged_tiff/PROD/2025/11/17",
        file_name="DAAAH9X4.001",
        creation_date=datetime(2025, 11, 17, tzinfo=UTC),
        last_view_date=None,
        total_pages=1,
        delete_code="",
    )
    mapping = CMMapping(
        clase_id="01.02.04.01.01",
        id_rvi="FF17",
        id_corto="CN01",
        clase_name="Autorizacion SMS",
        required_metadata_fields=(),
        cmis_type="MyType",
    )
    record = MigrationRecord(
        trigger_shortname=trigger.shortname,
        trigger_cif=trigger.cif or "",
        trigger_system_id=trigger.system_id,
        rvabrep_txn_num=document.txn_num,
        rvabrep_file_name=document.file_name,
        batch_id="B1",
        status=StageStatus.S5_DONE,
        created_at=datetime(2025, 11, 17, tzinfo=UTC),
        cm_object_id=cm_object_id,
        cm_folder=None,
        cm_object_type=None,
        source_file_path=None,
        page_count=None,
        file_size_bytes=None,
    )
    return PendingSyncItem(
        record=record,
        document=document,
        mapping=mapping,
        trigger=trigger,
        outcome=outcome,  # type: ignore[arg-type]
        cm_object_id=cm_object_id,
        error=error,
    )


def _row(*, txn: str = "0000001", stscod: str = "O", objidn: str = "cm-aaa") -> NiarvilogRow:
    now = datetime(2025, 11, 17, 10, 0, 0)
    return NiarvilogRow(
        siscod="1",
        trnnum=txn,
        docfrm="CC03",
        imgarc="DAAAH9X4.001",
        imgtip="B",
        ctecif="TESTCLIENT01",
        ctenum=123456,
        stscod=stscod,
        idnbac="CN01",
        tipidn="MyType",
        objidn=objidn,
        numrei=0,
        pmrrei=now,
        finrei=now,
        eerrmsg="",
    )


# ---------------------------------------------------------------------------
# PendingSyncBuffer
# ---------------------------------------------------------------------------


class TestPendingSyncBuffer:
    def test_append_and_drain(self) -> None:
        buf = PendingSyncBuffer()
        buf.append(_item(txn="0000001"))
        buf.append(_item(txn="0000002"))
        assert len(buf) == 2
        drained = buf.drain()
        assert len(drained) == 2
        assert len(buf) == 0  # drain vacía

    def test_drain_empty_returns_empty_list(self) -> None:
        assert PendingSyncBuffer().drain() == []


# ---------------------------------------------------------------------------
# As400Reconciler — dirección synced_to_as400
# ---------------------------------------------------------------------------


class TestReconcilerPushToAs400:
    def test_absent_row_is_claimed_and_marked_uploaded(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_state.return_value = None  # fila ausente
        as400.try_claim.return_value = True
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001")])

        assert result.synced_to_as400 == ["0000001"]
        assert result.conflicts == []
        as400.try_claim.assert_called_once()
        as400.mark_uploaded.assert_called_once()

    def test_failed_item_is_marked_failed(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_state.return_value = None
        as400.try_claim.return_value = True
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass(
            [_item(txn="0000001", outcome="failed", cm_object_id=None, error="boom")]
        )

        assert result.synced_to_as400 == ["0000001"]
        as400.mark_failed.assert_called_once()
        as400.mark_uploaded.assert_not_called()

    def test_consistent_row_is_skipped(self) -> None:
        # AS400 ya tiene 'O' con NUESTRO object id → nada que sincronizar.
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_state.return_value = _row(stscod="O", objidn="cm-aaa")
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001", cm_object_id="cm-aaa")])

        assert result.synced_to_as400 == []
        assert result.conflicts == []
        as400.try_claim.assert_not_called()


# ---------------------------------------------------------------------------
# As400Reconciler — conflictos
# ---------------------------------------------------------------------------


class TestReconcilerConflicts:
    def test_divergent_object_id_is_a_conflict(self) -> None:
        # AS400 dice 'O' pero con OTRO object id → lo subió otro sistema.
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_state.return_value = _row(stscod="O", objidn="cm-OTHER")
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001", cm_object_id="cm-aaa")])

        assert result.synced_to_as400 == []
        assert len(result.conflicts) == 1
        c = result.conflicts[0]
        assert c.txn_num == "0000001"
        assert c.as400_objidn == "cm-OTHER"
        assert c.local_object_id == "cm-aaa"
        as400.mark_uploaded.assert_not_called()  # nunca pisamos un conflicto

    def test_in_progress_row_is_a_conflict(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_state.return_value = _row(stscod="I", objidn="")
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001")])

        assert len(result.conflicts) == 1
        assert result.conflicts[0].as400_stscod == "I"

    def test_lost_claim_race_is_a_conflict(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_state.return_value = None
        as400.try_claim.return_value = False  # otro proceso ganó la race
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001")])

        assert result.synced_to_as400 == []
        assert len(result.conflicts) == 1


# ---------------------------------------------------------------------------
# As400Reconciler — dirección synced_to_local
# ---------------------------------------------------------------------------


class TestReconcilerImportToLocal:
    def test_foreign_upload_is_imported_to_sqlite(self) -> None:
        sqlite = MagicMock()
        sqlite.is_uploaded.return_value = False  # local no lo conoce
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_state_by_txn.return_value = _row(txn="0009999", stscod="O", objidn="cm-zzz")
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([], import_scope={"0009999"})

        assert result.synced_to_local == ["0009999"]
        sqlite.record_external_upload.assert_called_once()

    def test_already_local_txn_is_not_imported(self) -> None:
        sqlite = MagicMock()
        sqlite.is_uploaded.return_value = True  # local ya lo tiene
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([], import_scope={"0009999"})

        assert result.synced_to_local == []
        sqlite.record_external_upload.assert_not_called()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_pass_emits_reconcile_log(caplog: pytest.LogCaptureFixture) -> None:
    sqlite = MagicMock()
    as400 = MagicMock()
    as400.cleanup_stale_in_progress.return_value = 0
    as400.read_state.return_value = _row(stscod="O", objidn="cm-OTHER")
    rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

    with caplog.at_level("INFO", logger="cmcourier.metrics.reconcile"):
        rec.run_pass([_item(txn="0000001", cm_object_id="cm-aaa")])

    messages = [r.message for r in caplog.records]
    assert "reconcile_pass" in messages
    assert "reconcile_conflict" in messages


# ---------------------------------------------------------------------------
# PeriodicReconciler
# ---------------------------------------------------------------------------


class TestPeriodicReconciler:
    def test_run_one_drains_buffer_and_reconciles(self) -> None:
        buf = PendingSyncBuffer()
        buf.append(_item(txn="0000001"))
        reconciler = MagicMock()
        periodic = PeriodicReconciler(reconciler=reconciler, buffer=buf, interval_s=999)
        periodic.run_one()
        # El buffer se drenó y se le pasó el item a run_pass.
        assert len(buf) == 0
        reconciler.run_pass.assert_called_once()
        passed_items = reconciler.run_pass.call_args.args[0]
        assert len(passed_items) == 1

    def test_run_one_swallows_reconciler_errors(self) -> None:
        # Un AS400 caído no debe tumbar el pipeline.
        reconciler = MagicMock()
        reconciler.run_pass.side_effect = RuntimeError("AS400 down")
        periodic = PeriodicReconciler(
            reconciler=reconciler, buffer=PendingSyncBuffer(), interval_s=999
        )
        periodic.run_one()  # no levanta

    def test_stop_runs_a_final_pass(self) -> None:
        buf = PendingSyncBuffer()
        reconciler = MagicMock()
        periodic = PeriodicReconciler(reconciler=reconciler, buffer=buf, interval_s=999)
        periodic.start()
        buf.append(_item(txn="0000001"))
        periodic.stop(join_timeout_s=2.0)
        # La pasada final corre aunque el intervalo (999s) nunca disparó.
        reconciler.run_pass.assert_called_once()

    def test_import_scope_provider_is_used(self) -> None:
        reconciler = MagicMock()
        periodic = PeriodicReconciler(
            reconciler=reconciler,
            buffer=PendingSyncBuffer(),
            interval_s=999,
            import_scope_provider=lambda: {"0000007"},
        )
        periodic.run_one()
        assert reconciler.run_pass.call_args.kwargs["import_scope"] == {"0000007"}
