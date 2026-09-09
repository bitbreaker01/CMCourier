"""Tests del As400Reconciler + PendingSyncBuffer + PeriodicReconciler (096).

El reconciliador es lógica de `dispatch` pura — los dos stores se
mockean con :class:`MagicMock`. ``read_state`` devuelve un
:class:`NiarvilogRow` o ``None`` según el escenario.
"""

from __future__ import annotations

import threading
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


def _boom() -> bool:
    raise RuntimeError("AS400 boom")


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
        as400.read_states_by_txns.return_value = {}  # fila ausente
        as400.insert_terminal.return_value = True
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001")])

        assert result.synced_to_as400 == ["0000001"]
        assert result.conflicts == []
        # 117: un solo write directo al estado terminal — sin claim previo.
        as400.insert_terminal.assert_called_once()
        assert as400.insert_terminal.call_args.kwargs["stscod"] == "O"
        as400.update_terminal_if_new.assert_not_called()

    def test_failed_item_is_marked_failed(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {}
        as400.insert_terminal.return_value = True
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass(
            [_item(txn="0000001", outcome="failed", cm_object_id=None, error="boom")]
        )

        assert result.synced_to_as400 == ["0000001"]
        assert as400.insert_terminal.call_args.kwargs["stscod"] == "F"
        assert as400.insert_terminal.call_args.kwargs["error"] == "boom"

    def test_consistent_row_is_skipped(self) -> None:
        # AS400 ya tiene 'O' con NUESTRO object id → nada que sincronizar.
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {"0000001": _row(stscod="O", objidn="cm-aaa")}
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001", cm_object_id="cm-aaa")])

        assert result.synced_to_as400 == []
        assert result.conflicts == []
        as400.insert_terminal.assert_not_called()
        as400.update_terminal_if_new.assert_not_called()


# ---------------------------------------------------------------------------
# As400Reconciler — conflictos
# ---------------------------------------------------------------------------


class TestReconcilerConflicts:
    def test_divergent_object_id_is_a_conflict(self) -> None:
        # AS400 dice 'O' pero con OTRO object id → lo subió otro sistema.
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {"0000001": _row(stscod="O", objidn="cm-OTHER")}
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001", cm_object_id="cm-aaa")])

        assert result.synced_to_as400 == []
        assert len(result.conflicts) == 1
        c = result.conflicts[0]
        assert c.txn_num == "0000001"
        assert c.as400_objidn == "cm-OTHER"
        assert c.local_object_id == "cm-aaa"
        as400.update_terminal_if_new.assert_not_called()  # nunca pisamos un conflicto

    def test_in_progress_row_is_a_conflict(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {"0000001": _row(stscod="I", objidn="")}
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001")])

        assert len(result.conflicts) == 1
        assert result.conflicts[0].as400_stscod == "I"

    def test_lost_claim_race_is_a_conflict(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {}
        as400.insert_terminal.return_value = False  # otro proceso ganó la race
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
        as400.read_states_by_txns.return_value = {
            "0009999": _row(txn="0009999", stscod="O", objidn="cm-zzz")
        }
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
    as400.read_states_by_txns.return_value = {"0000001": _row(stscod="O", objidn="cm-OTHER")}
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


# ---------------------------------------------------------------------------
# 098 — resiliencia: ningún ítem drenado se pierde
# ---------------------------------------------------------------------------


class TestReconcilerResilience:
    """098: una falla per-ítem NO debe abortar el batch ni perder ítems."""

    def test_one_failing_item_does_not_abort_the_batch(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {}
        # El 2do ítem explota en el write; el 1ro y el 3ro están OK. 144:
        # el write es paralelo, así que la falla se ata al txn, no al
        # orden de llamada.
        as400.insert_terminal.side_effect = lambda **kw: (
            _boom() if kw["document"].txn_num == "0000002" else True
        )
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        items = [_item(txn="0000001"), _item(txn="0000002"), _item(txn="0000003")]
        result = rec.run_pass(items)

        # Los dos sanos se sincronizaron — la falla del 2do no los arrastró.
        assert set(result.synced_to_as400) == {"0000001", "0000003"}
        assert result.failed == 1
        # El que falló queda para reintento, no se pierde.
        assert len(result.requeued) == 1
        assert result.requeued[0].document.txn_num == "0000002"

    def test_run_pass_never_raises_even_if_everything_fails(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.side_effect = RuntimeError("AS400 down")
        as400.read_states_by_txns.side_effect = RuntimeError("AS400 down")
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        # No debe levantar — todos los ítems van a requeued.
        result = rec.run_pass([_item(txn="0000001"), _item(txn="0000002")])
        assert result.failed == 2
        assert len(result.requeued) == 2

    def test_cleanup_stale_failure_does_not_abort_pass(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.side_effect = RuntimeError("stale boom")
        as400.read_states_by_txns.return_value = {}
        as400.insert_terminal.return_value = True
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        result = rec.run_pass([_item(txn="0000001")])
        # El fallo de cleanup no impide procesar los ítems.
        assert result.synced_to_as400 == ["0000001"]

    def test_stop_event_halts_pass_and_requeues_remainder(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.return_value = {}
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        stop = threading.Event()
        stop.set()  # ya seteado → corta antes del primer ítem
        items = [_item(txn="0000001"), _item(txn="0000002")]
        result = rec.run_pass(items, stop_event=stop)

        assert result.synced_to_as400 == []
        assert len(result.requeued) == 2  # todo vuelve al buffer

    def test_run_one_rebuffers_failed_items(self) -> None:
        sqlite = MagicMock()
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        as400.read_states_by_txns.side_effect = RuntimeError("AS400 boom")
        rec = As400Reconciler(sqlite_store=sqlite, as400_store=as400)

        buf = PendingSyncBuffer()
        buf.append(_item(txn="0000001"))
        periodic = PeriodicReconciler(reconciler=rec, buffer=buf, interval_s=999)

        periodic.run_one()
        # El ítem que falló volvió al buffer — no se perdió.
        assert len(buf) == 1


# ---------------------------------------------------------------------------
# 144 — pasada en paralelo + progreso
# ---------------------------------------------------------------------------


def _as400_ok() -> MagicMock:
    as400 = MagicMock()
    as400.cleanup_stale_in_progress.return_value = 0
    as400.read_states_by_txns.return_value = {}
    as400.insert_terminal.return_value = True
    return as400


class TestReconcilerParallelPass144:
    """144: ``run_pass`` propaga con un pool acotado (``write_workers``)."""

    def test_all_items_propagated_with_bounded_concurrency(self) -> None:
        import time

        as400 = _as400_ok()
        lock = threading.Lock()
        in_flight = 0
        peak = 0

        def _insert(**kw: object) -> bool:
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.005)
            with lock:
                in_flight -= 1
            return True

        as400.insert_terminal.side_effect = _insert
        rec = As400Reconciler(sqlite_store=MagicMock(), as400_store=as400, write_workers=4)

        items = [_item(txn=f"{i:07d}") for i in range(1, 21)]
        result = rec.run_pass(items)

        assert sorted(result.synced_to_as400) == [f"{i:07d}" for i in range(1, 21)]
        assert result.failed == 0 and result.requeued == []
        assert peak <= 4, f"el pool superó write_workers: {peak}"
        assert peak >= 2, "los writes no se solaparon — la pasada sigue siendo serial"

    def test_one_failing_item_is_requeued_and_the_rest_synced(self) -> None:
        as400 = _as400_ok()
        as400.insert_terminal.side_effect = lambda **kw: (
            _boom() if kw["document"].txn_num == "0000005" else True
        )
        rec = As400Reconciler(sqlite_store=MagicMock(), as400_store=as400, write_workers=3)

        items = [_item(txn=f"{i:07d}") for i in range(1, 11)]
        result = rec.run_pass(items)

        assert result.failed == 1
        assert [it.document.txn_num for it in result.requeued] == ["0000005"]
        assert len(result.synced_to_as400) == 9
        assert "0000005" not in result.synced_to_as400

    def test_stop_event_mid_pass_requeues_unsubmitted_items(self) -> None:
        as400 = _as400_ok()
        stop = threading.Event()

        def _insert_and_stop(**kw: object) -> bool:
            stop.set()  # se pide parar mientras el 1er write está en vuelo
            return True

        as400.insert_terminal.side_effect = _insert_and_stop
        rec = As400Reconciler(sqlite_store=MagicMock(), as400_store=as400, write_workers=1)

        items = [_item(txn="0000001"), _item(txn="0000002"), _item(txn="0000003")]
        result = rec.run_pass(items, stop_event=stop)

        # El ya despachado termina; los no despachados vuelven al buffer.
        assert result.synced_to_as400 == ["0000001"]
        assert [it.document.txn_num for it in result.requeued] == ["0000002", "0000003"]
        assert result.failed == 0

    def test_conflicts_still_detected_in_parallel(self) -> None:
        as400 = _as400_ok()
        as400.read_states_by_txns.return_value = {
            "0000002": _row(txn="0000002", stscod="O", objidn="cm-OTHER"),
        }
        # El 3ro pierde la race del write.
        as400.insert_terminal.side_effect = lambda **kw: kw["document"].txn_num != "0000003"
        rec = As400Reconciler(sqlite_store=MagicMock(), as400_store=as400, write_workers=4)

        result = rec.run_pass([_item(txn=f"{i:07d}") for i in range(1, 4)])

        assert result.synced_to_as400 == ["0000001"]
        assert sorted(c.txn_num for c in result.conflicts) == ["0000002", "0000003"]
        assert result.failed == 0

    def test_progress_events_start_every_50_and_end(self) -> None:
        from cmcourier.services.recovery import SyncProgress

        as400 = _as400_ok()
        rec = As400Reconciler(sqlite_store=MagicMock(), as400_store=as400, write_workers=4)
        events: list[SyncProgress] = []
        calling_thread = threading.get_ident()
        emitting_threads: set[int] = set()

        def _on_progress(ev: SyncProgress) -> None:
            emitting_threads.add(threading.get_ident())
            events.append(ev)

        items = [_item(txn=f"{i:07d}") for i in range(1, 121)]
        rec.run_pass(items, on_progress=_on_progress)

        assert [(e.done, e.total) for e in events] == [(0, 120), (50, 120), (100, 120), (120, 120)]
        assert {e.phase for e in events} == {"sincronizando AS400"}
        # Nunca desde los hilos del pool — el callback toca UI.
        assert emitting_threads == {calling_thread}

    def test_progress_exact_multiple_does_not_emit_twice_at_end(self) -> None:
        as400 = _as400_ok()
        rec = As400Reconciler(sqlite_store=MagicMock(), as400_store=as400, write_workers=2)
        seen: list[tuple[int, int]] = []
        rec.run_pass(
            [_item(txn=f"{i:07d}") for i in range(1, 51)],
            on_progress=lambda ev: seen.append((ev.done, ev.total)),
        )
        assert seen == [(0, 50), (50, 50)]

    def test_progress_with_no_items_emits_zero_over_zero(self) -> None:
        as400 = _as400_ok()
        rec = As400Reconciler(sqlite_store=MagicMock(), as400_store=as400)
        seen: list[tuple[int, int]] = []
        rec.run_pass([], on_progress=lambda ev: seen.append((ev.done, ev.total)))
        assert seen == [(0, 0)]

    def test_raising_progress_callback_does_not_abort_pass(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        as400 = _as400_ok()
        rec = As400Reconciler(sqlite_store=MagicMock(), as400_store=as400)

        def _broken(ev: object) -> None:
            raise RuntimeError("widget muerto")

        with caplog.at_level("ERROR", logger="cmcourier.services"):
            result = rec.run_pass([_item(txn="0000001")], on_progress=_broken)
        assert result.synced_to_as400 == ["0000001"]
        assert any("on_progress" in r.getMessage() for r in caplog.records)


class TestPeriodicReconcilerProgress144:
    def test_run_one_forwards_on_progress(self) -> None:
        reconciler = MagicMock()
        periodic = PeriodicReconciler(
            reconciler=reconciler, buffer=PendingSyncBuffer(), interval_s=999
        )
        cb = MagicMock()
        periodic.run_one(on_progress=cb)
        assert reconciler.run_pass.call_args.kwargs["on_progress"] is cb

    def test_stop_forwards_on_progress_to_the_final_pass(self) -> None:
        reconciler = MagicMock()
        periodic = PeriodicReconciler(
            reconciler=reconciler, buffer=PendingSyncBuffer(), interval_s=999
        )
        cb = MagicMock()
        periodic.stop(join_timeout_s=1.0, on_progress=cb)
        kwargs = reconciler.run_pass.call_args.kwargs
        assert kwargs["on_progress"] is cb
        assert kwargs["stop_event"] is None  # la pasada final procesa TODO

    def test_daemon_pass_does_not_forward_progress(self) -> None:
        # Sólo la pasada FINAL reporta al monitor; el daemon corre mudo.
        reconciler = MagicMock()
        periodic = PeriodicReconciler(
            reconciler=reconciler, buffer=PendingSyncBuffer(), interval_s=999
        )
        periodic.run_one(stop_event=threading.Event())
        assert reconciler.run_pass.call_args.kwargs["on_progress"] is None


class TestStopReconcilerVisibly144:
    """144: el helper compartido por los tres orquestadores."""

    def test_sets_progress_on_pool_stats_and_clears_at_the_end(self) -> None:
        from cmcourier.services.reconciler import stop_reconciler_visibly
        from cmcourier.services.recovery import SyncProgress
        from cmcourier.services.worker_pool_stats import WorkerPoolStats

        stats = WorkerPoolStats()
        seen: list[tuple[str, int, int] | None] = []

        class _Recon:
            def stop(self, *, on_progress=None):  # noqa: ANN001, ANN202
                c = stats.snapshot().closing
                seen.append((c.label, c.done, c.total) if c else None)
                on_progress(SyncProgress("sincronizando AS400", 50, 120))
                c = stats.snapshot().closing
                seen.append((c.label, c.done, c.total) if c else None)
                return "final-result"

        result = stop_reconciler_visibly(_Recon(), stats)  # type: ignore[arg-type]

        assert result == "final-result"
        assert seen == [("sincronizando AS400", 0, 0), ("sincronizando AS400", 50, 120)]
        assert stats.snapshot().closing is None

    def test_clears_closing_even_if_stop_raises(self) -> None:
        from cmcourier.services.reconciler import stop_reconciler_visibly
        from cmcourier.services.worker_pool_stats import WorkerPoolStats

        stats = WorkerPoolStats()
        recon = MagicMock()
        recon.stop.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            stop_reconciler_visibly(recon, stats)
        assert stats.snapshot().closing is None

    def test_without_pool_stats_just_stops(self) -> None:
        from cmcourier.services.reconciler import stop_reconciler_visibly

        recon = MagicMock()
        recon.stop.return_value = "r"
        assert stop_reconciler_visibly(recon, None) == "r"
        recon.stop.assert_called_once_with()
