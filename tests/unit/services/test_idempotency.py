"""Tests unitarios para :class:`IdempotencyCoordinator` (034 fase 3).

El coordinador compone ``SQLiteTrackingStore`` (siempre presente)
con un ``As400NiarvilogStore`` opcional (solo cuando
``tracking.as400_sync.enabled=true``). Cuando el store de AS400 es
``None``, el comportamiento es byte-idéntico al pre-034 — el
coordinador simplemente delega a SQLite.

Los tests usan ``unittest.mock`` en vez de adaptadores reales
porque el coordinador es lógica pura de `dispatch`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.tracking.as400_niarvilog import NiarvilogRow
from cmcourier.domain.models import (
    CMMapping,
    MigrationRecord,
    ReasonCode,
    RVABREPDocument,
    StageStatus,
    TriggerRecord,
)
from cmcourier.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyCoordinator,
    SyncReport,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _record(
    txn: str = "0000001",
    *,
    siscod: str = "1",
    cm_object_id: str | None = None,
) -> tuple[MigrationRecord, RVABREPDocument, CMMapping, TriggerRecord]:
    trigger = TriggerRecord(shortname="TESTCLIENT01", cif="123456", system_id=siscod)
    document = RVABREPDocument(
        system_code=siscod,
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
        status=StageStatus.S5_PENDING,
        created_at=datetime(2025, 11, 17, tzinfo=UTC),
        cm_object_id=cm_object_id,
        cm_folder=None,
        cm_object_type=None,
        source_file_path=None,
        page_count=None,
        file_size_bytes=None,
    )
    return record, document, mapping, trigger


def _niarvilog_row(
    *,
    txn: str = "0000001",
    stscod: str = "N",
    objidn: str = "",
) -> NiarvilogRow:
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
# Camino deshabilitado (store AS400 es None)
# ---------------------------------------------------------------------------


class TestCoordinatorAs400Disabled:
    def test_is_uploaded_delegates_to_sqlite(self) -> None:
        sqlite = MagicMock()
        sqlite.is_uploaded.return_value = True
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=None)
        assert coord.is_uploaded("TXN_001") is True
        sqlite.is_uploaded.assert_called_once_with("TXN_001")

    def test_try_claim_always_true_without_as400(self) -> None:
        sqlite = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=None)
        record, document, mapping, trigger = _record()
        assert (
            coord.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)
            is True
        )

    def test_mark_uploaded_writes_only_to_sqlite(self) -> None:
        sqlite = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=None)
        record, document, mapping, trigger = _record()
        coord.mark_uploaded(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            cm_object_id="cmis-abc",
        )
        # 047: el coordinador reenvía `cm_object_id` al store SQLite
        # para que `migration_log.cm_object_id` se pueble en la fila
        # S5_DONE.
        sqlite.mark_stage_done.assert_called_once_with(
            "0000001", "B1", StageStatus.S5_DONE, cm_object_id="cmis-abc"
        )

    def test_mark_failed_writes_only_to_sqlite(self) -> None:
        sqlite = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=None)
        record, document, mapping, trigger = _record()
        coord.mark_failed(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            stage=StageStatus.S5_FAILED,
            error="CMIS 500",
        )
        sqlite.mark_stage_failed.assert_called_once_with(
            "0000001", "B1", StageStatus.S5_FAILED, "CMIS 500", reason_code=None
        )

    def test_preflight_sync_is_noop(self) -> None:
        sqlite = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=None)
        report = coord.preflight_sync(batch_scope=set())
        assert isinstance(report, SyncReport)
        assert report.imported_from_as400 == []
        assert report.conflicts == []
        assert report.stale_cleaned == 0


# ---------------------------------------------------------------------------
# Camino habilitado (store AS400 presente)
# ---------------------------------------------------------------------------


class TestCoordinatorAs400Enabled:
    def test_is_uploaded_uses_as400_status(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.read_state.return_value = _niarvilog_row(stscod="O")
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        # `is_uploaded` solo toma un `txn_num`; tiene que conocer la
        # PK. La firma del coordinador requiere el record completo
        # para AS400.
        record, document, mapping, trigger = _record()
        assert coord.is_uploaded_record(document=document, trigger=trigger) is True
        sqlite.is_uploaded.assert_not_called()

    def test_is_uploaded_falls_back_when_row_absent(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.read_state.return_value = None
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        record, document, mapping, trigger = _record()
        # Fila ausente en AS400 → no está uploadeado (sin importar SQLite).
        assert coord.is_uploaded_record(document=document, trigger=trigger) is False

    def test_try_claim_delegates_to_as400(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.try_claim.return_value = True
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        record, document, mapping, trigger = _record()
        result = coord.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)
        assert result is True
        as400.try_claim.assert_called_once()

    def test_try_claim_false_when_as400_says_no(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.try_claim.return_value = False
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        record, document, mapping, trigger = _record()
        assert (
            coord.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)
            is False
        )

    def test_mark_uploaded_dual_writes(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        record, document, mapping, trigger = _record()
        coord.mark_uploaded(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            cm_object_id="cmis-abc",
        )
        sqlite.mark_stage_done.assert_called_once_with(
            "0000001", "B1", StageStatus.S5_DONE, cm_object_id="cmis-abc"
        )
        as400.mark_uploaded.assert_called_once()

    def test_mark_failed_dual_writes(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        record, document, mapping, trigger = _record()
        coord.mark_failed(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            stage=StageStatus.S5_FAILED,
            error="boom",
        )
        sqlite.mark_stage_failed.assert_called_once_with(
            "0000001", "B1", StageStatus.S5_FAILED, "boom", reason_code=None
        )
        as400.mark_failed.assert_called_once()

    def test_reason_code_goes_to_sqlite_only(self) -> None:
        """148 REQ-004: el censo vive en ``migration_log``. NIARVILOG tiene
        su propio ``STSCOD`` y no es la tabla que el censo lee."""
        sqlite = MagicMock()
        as400 = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        record, document, mapping, trigger = _record()
        coord.mark_failed(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            stage=StageStatus.S5_FAILED,
            error="boom",
            reason_code=ReasonCode.CM_ERROR_5XX,
        )
        assert sqlite.mark_stage_failed.call_args.kwargs["reason_code"] is ReasonCode.CM_ERROR_5XX
        assert "reason_code" not in as400.mark_failed.call_args.kwargs


# ---------------------------------------------------------------------------
# `preflight_sync`
# ---------------------------------------------------------------------------


class TestPreflightSync:
    def test_imports_completed_from_as400(self) -> None:
        """AS400 tiene `STSCOD='O'` para un doc que SQLite no conoce → importa."""
        sqlite = MagicMock()
        sqlite.is_uploaded.return_value = False  # SQLite no tiene record
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.side_effect = lambda txns: {
            txn: _niarvilog_row(txn=txn, stscod="O", objidn="cmis-xyz") for txn in txns
        }
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        report = coord.preflight_sync(batch_scope={"0000001"})
        assert "0000001" in report.imported_from_as400
        assert report.conflicts == []

    def test_detects_conflict_when_sqlite_done_but_as400_new(self) -> None:
        """SQLite dice S5_DONE, AS400 dice `STSCOD='N'` → conflicto."""
        sqlite = MagicMock()
        sqlite.is_uploaded.return_value = True  # SQLite dice done
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {"0000001": _niarvilog_row(stscod="N")}
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        report = coord.preflight_sync(batch_scope={"0000001"})
        assert "0000001" in report.conflicts

    def test_runs_stale_cleanup_first(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 7
        as400.read_states_by_txns.return_value = {}
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        report = coord.preflight_sync(batch_scope=set())
        assert report.stale_cleaned == 7
        as400.cleanup_stale_in_progress.assert_called_once()

    def test_single_batched_read_for_the_whole_scope(self) -> None:
        """113: un solo read_states_by_txns para todo el scope."""
        sqlite = MagicMock()
        sqlite.is_uploaded.return_value = False
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {}
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        coord.preflight_sync(batch_scope={f"{i:07d}" for i in range(500)})
        as400.read_states_by_txns.assert_called_once()
        as400.read_state_by_txn.assert_not_called()

    def test_raises_when_conflicts_exist(self) -> None:
        sqlite = MagicMock()
        sqlite.is_uploaded.return_value = True
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {"0000001": _niarvilog_row(stscod="N")}
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400)
        with pytest.raises(IdempotencyConflictError) as ei:
            coord.preflight_sync(batch_scope={"0000001"}, raise_on_conflict=True)
        assert "0000001" in str(ei.value)


# ---------------------------------------------------------------------------
# 096 — modo de sincronización periódico
# ---------------------------------------------------------------------------


class TestCoordinatorPeriodicMode:
    """En mode=periodic, S5 NO toca AS400 — solo escribe SQLite. La
    reconciliación queda a cargo del As400Reconciler de fondo."""

    def test_try_claim_does_not_touch_as400(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400, mode="periodic")
        record, document, mapping, trigger = _record()
        assert (
            coord.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)
            is True
        )
        as400.try_claim.assert_not_called()

    def test_mark_uploaded_writes_sqlite_only(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400, mode="periodic")
        record, document, mapping, trigger = _record()
        coord.mark_uploaded(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            cm_object_id="cm-xyz",
        )
        sqlite.mark_stage_done.assert_called_once()
        as400.mark_uploaded.assert_not_called()

    def test_mark_failed_writes_sqlite_only(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400, mode="periodic")
        record, document, mapping, trigger = _record()
        coord.mark_failed(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            stage=StageStatus.S5_FAILED,
            error="boom",
        )
        sqlite.mark_stage_failed.assert_called_once()
        as400.mark_failed.assert_not_called()

    def test_preflight_sync_is_noop_and_never_raises(self) -> None:
        # periodic tolera conflictos: el preflight no debe abortar el pipeline.
        sqlite = MagicMock()
        sqlite.is_uploaded.return_value = True
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_state_by_txn.return_value = _niarvilog_row(stscod="N")
        coord = IdempotencyCoordinator(sqlite_store=sqlite, as400_store=as400, mode="periodic")
        report = coord.preflight_sync(batch_scope={"0000001"}, raise_on_conflict=True)
        assert report.conflicts == []
        as400.read_state_by_txn.assert_not_called()


class TestCoordinatorPeriodicBuffer:
    """En periodic con un PendingSyncBuffer, S5 encola el contexto del doc."""

    def test_mark_uploaded_appends_uploaded_item_to_buffer(self) -> None:
        from cmcourier.services.reconciler import PendingSyncBuffer

        sqlite = MagicMock()
        buffer = PendingSyncBuffer()
        coord = IdempotencyCoordinator(
            sqlite_store=sqlite,
            as400_store=MagicMock(),
            mode="periodic",
            pending_buffer=buffer,
        )
        record, document, mapping, trigger = _record()
        coord.mark_uploaded(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            cm_object_id="cm-xyz",
        )
        items = buffer.drain()
        assert len(items) == 1
        assert items[0].outcome == "uploaded"
        assert items[0].cm_object_id == "cm-xyz"

    def test_mark_failed_appends_failed_item_to_buffer(self) -> None:
        from cmcourier.services.reconciler import PendingSyncBuffer

        sqlite = MagicMock()
        buffer = PendingSyncBuffer()
        coord = IdempotencyCoordinator(
            sqlite_store=sqlite,
            as400_store=MagicMock(),
            mode="periodic",
            pending_buffer=buffer,
        )
        record, document, mapping, trigger = _record()
        coord.mark_failed(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            stage=StageStatus.S5_FAILED,
            error="boom",
        )
        items = buffer.drain()
        assert len(items) == 1
        assert items[0].outcome == "failed"
        assert items[0].error == "boom"
