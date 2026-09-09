"""Tests de :class:`As400Recovery` (099).

El recuperador es lógica de orquestación pura — stores y servicios se
mockean con :class:`MagicMock`.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.tracking.sqlite import UploadedRecord
from cmcourier.domain.exceptions import IDRViNotMappedError
from cmcourier.services.recovery import As400Recovery, RecoveryItem, SyncProgress

pytestmark = pytest.mark.unit


def _uploaded(txn: str = "0000001") -> UploadedRecord:
    return UploadedRecord(
        txn_num=txn,
        cm_object_id=f"cm-{txn}",
        shortname="CLIENT01",
        cif="123456",
        system_id="1",
        file_name="DOC.001",
        retry_count=0,
    )


def _document(*, index7: str = "CC03", image_type: str = "B") -> MagicMock:
    doc = MagicMock()
    doc.index7 = index7
    doc.image_type = image_type
    return doc


def _mapping(*, id_corto: str = "CN01", cmis_type: str = "MyType") -> MagicMock:
    m = MagicMock()
    m.id_corto = id_corto
    m.cmis_type = cmis_type
    return m


def _recovery(
    *,
    uploaded: list[UploadedRecord],
    as400: MagicMock,
    indexing: MagicMock,
    mapping: MagicMock,
) -> As400Recovery:
    sqlite = MagicMock()
    sqlite.uploaded_records.return_value = uploaded
    return As400Recovery(
        sqlite_store=sqlite,
        as400_store=as400,
        indexing_service=indexing,
        mapping_service=mapping,
    )


# ---------------------------------------------------------------------------
# Camino feliz + dry-run
# ---------------------------------------------------------------------------


class TestRecoverHappyPath:
    def test_missing_txn_is_recovered_with_apply(self) -> None:
        as400 = MagicMock()
        as400.read_states_by_txns.return_value = {}  # ausente en NIARVILOG
        indexing = MagicMock()
        indexing.find_documents_by_txns.return_value = {"0000001": _document()}
        mapping = MagicMock()
        mapping.get_mapping.return_value = _mapping()
        rec = _recovery(
            uploaded=[_uploaded("0000001")], as400=as400, indexing=indexing, mapping=mapping
        )

        result = rec.recover(apply=True)

        assert result.recovered == ["0000001"]
        assert result.unrecoverable == []
        as400.insert_recovered_row.assert_called_once()

    def test_recovered_row_carries_the_rederived_fields(self) -> None:
        as400 = MagicMock()
        as400.read_states_by_txns.return_value = {}
        indexing = MagicMock()
        indexing.find_documents_by_txns.return_value = {
            "0000001": _document(index7="FF17", image_type="O")
        }
        mapping = MagicMock()
        mapping.get_mapping.return_value = _mapping(id_corto="CN09", cmis_type="TipoX")
        rec = _recovery(
            uploaded=[_uploaded("0000001")], as400=as400, indexing=indexing, mapping=mapping
        )

        rec.recover(apply=True)

        kwargs = as400.insert_recovered_row.call_args.kwargs
        assert kwargs["docfrm"] == "FF17"  # re-derivado de RVABREP
        assert kwargs["imgtip"] == "O"
        assert kwargs["idnbac"] == "CN09"  # re-derivado del mapping
        assert kwargs["tipidn"] == "TipoX"
        assert kwargs["objidn"] == "cm-0000001"  # de SQLite
        # el mapping se busca por el index7 del documento
        mapping.get_mapping.assert_called_once_with("FF17")

    def test_dry_run_does_not_write_as400(self) -> None:
        as400 = MagicMock()
        as400.read_states_by_txns.return_value = {}
        indexing = MagicMock()
        indexing.find_documents_by_txns.return_value = {"0000001": _document()}
        mapping = MagicMock()
        mapping.get_mapping.return_value = _mapping()
        rec = _recovery(
            uploaded=[_uploaded("0000001")], as400=as400, indexing=indexing, mapping=mapping
        )

        result = rec.recover(apply=False)

        # El plan lista el txn como recuperable, pero no se escribió nada.
        assert result.recovered == ["0000001"]
        as400.insert_recovered_row.assert_not_called()


# ---------------------------------------------------------------------------
# Skips + no-recuperables
# ---------------------------------------------------------------------------


class TestRecoverSkipsAndFailures:
    def test_txn_already_in_as400_is_skipped(self) -> None:
        as400 = MagicMock()
        as400.read_states_by_txns.return_value = {"0000001": object()}  # ya existe
        rec = _recovery(
            uploaded=[_uploaded("0000001")],
            as400=as400,
            indexing=MagicMock(),
            mapping=MagicMock(),
        )

        result = rec.recover(apply=True)

        assert result.already_present == ["0000001"]
        assert result.recovered == []
        as400.insert_recovered_row.assert_not_called()

    def test_missing_rvabrep_row_is_unrecoverable(self) -> None:
        as400 = MagicMock()
        as400.read_states_by_txns.return_value = {}
        indexing = MagicMock()
        indexing.find_documents_by_txns.return_value = {}  # sin fila RVABREP
        rec = _recovery(
            uploaded=[_uploaded("0000001")],
            as400=as400,
            indexing=indexing,
            mapping=MagicMock(),
        )

        result = rec.recover(apply=True)

        assert result.recovered == []
        assert result.unrecoverable == [RecoveryItem("0000001", "rvabrep_row_not_found")]
        as400.insert_recovered_row.assert_not_called()

    def test_unmapped_id_rvi_is_unrecoverable(self) -> None:
        as400 = MagicMock()
        as400.read_states_by_txns.return_value = {}
        indexing = MagicMock()
        indexing.find_documents_by_txns.return_value = {"0000001": _document(index7="CC99")}
        mapping = MagicMock()
        mapping.get_mapping.side_effect = IDRViNotMappedError(id_rvi="CC99")
        rec = _recovery(
            uploaded=[_uploaded("0000001")], as400=as400, indexing=indexing, mapping=mapping
        )

        result = rec.recover(apply=True)

        assert len(result.unrecoverable) == 1
        assert result.unrecoverable[0].reason.startswith("id_rvi_not_mapped")
        as400.insert_recovered_row.assert_not_called()

    def test_one_failing_txn_does_not_abort_the_rest(self) -> None:
        # La lección del 098: un txn que explota no se lleva a los demás.
        as400 = MagicMock()
        as400.read_states_by_txns.return_value = {}
        indexing = MagicMock()
        indexing.find_documents_by_txns.return_value = {
            t: _document() for t in ("0000001", "0000002", "0000003")
        }
        mapping = MagicMock()
        mapping.get_mapping.return_value = _mapping()

        # El INSERT del 2do txn explota; el 1ro y el 3ro están OK. 144: los
        # INSERT corren en paralelo → se discrimina por txn, no por orden.
        def insert(**kwargs: object) -> None:
            if kwargs["trnnum"] == "0000002":
                raise RuntimeError("AS400 boom")

        as400.insert_recovered_row.side_effect = insert
        rec = _recovery(
            uploaded=[_uploaded("0000001"), _uploaded("0000002"), _uploaded("0000003")],
            as400=as400,
            indexing=indexing,
            mapping=mapping,
        )

        result = rec.recover(apply=True)

        assert set(result.recovered) == {"0000001", "0000003"}
        assert len(result.unrecoverable) == 1
        assert result.unrecoverable[0].txn_num == "0000002"


class TestBatchedExistenceCheck118:
    def test_single_batched_read_for_all_records(self) -> None:
        """118: N docs → UNA llamada batcheada, no N SELECTs."""
        as400 = MagicMock()
        txns = [f"{i:07d}" for i in range(50)]
        as400.read_states_by_txns.return_value = {t: object() for t in txns}
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns],
            as400=as400,
            indexing=MagicMock(),
            mapping=MagicMock(),
        )

        result = rec.recover(apply=True)

        assert len(result.already_present) == 50
        as400.read_states_by_txns.assert_called_once()
        as400.read_state_by_txn.assert_not_called()
        as400.insert_recovered_row.assert_not_called()


# ---------------------------------------------------------------------------
# 144 — RVABREP batcheado, INSERT en paralelo, progreso
# ---------------------------------------------------------------------------


def _missing_setup(n: int) -> tuple[list[str], MagicMock, MagicMock, MagicMock]:
    """N docs ausentes en NIARVILOG, todos con fila RVABREP y mapping."""
    txns = [f"{i:07d}" for i in range(n)]
    as400 = MagicMock()
    as400.read_states_by_txns.return_value = {}
    indexing = MagicMock()
    indexing.find_documents_by_txns.return_value = {t: _document() for t in txns}
    mapping = MagicMock()
    mapping.get_mapping.return_value = _mapping()
    return txns, as400, indexing, mapping


class TestBatchedRvabrepLookup144:
    def test_single_batched_rvabrep_read_for_all_missing(self) -> None:
        """144: N faltantes → UNA llamada a ``find_documents_by_txns`` con
        exactamente los txns faltantes; nunca ``find_document_by_txn``."""
        txns, as400, indexing, mapping = _missing_setup(120)
        # uno ya presente: no debe pedirse a RVABREP
        as400.read_states_by_txns.return_value = {txns[0]: object()}
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )

        result = rec.recover(apply=False)

        indexing.find_documents_by_txns.assert_called_once_with(txns[1:])
        indexing.find_document_by_txn.assert_not_called()
        assert result.recovered == txns[1:]
        assert result.already_present == [txns[0]]

    def test_nothing_missing_skips_rvabrep_and_insert(self) -> None:
        txns, as400, indexing, mapping = _missing_setup(3)
        as400.read_states_by_txns.return_value = {t: object() for t in txns}
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )

        result = rec.recover(apply=True)

        assert result.already_present == txns
        indexing.find_documents_by_txns.assert_not_called()
        as400.insert_recovered_row.assert_not_called()


class TestParallelInserts144:
    def test_all_inserts_happen_with_bounded_pool(self) -> None:
        """144: los INSERT corren en un pool acotado (``write_workers``):
        se ejecutan TODOS y nunca más de ``write_workers`` a la vez."""
        txns, as400, indexing, mapping = _missing_setup(40)
        lock = threading.Lock()
        active = 0
        peak = 0
        threads: set[int] = set()

        def insert(**kwargs: object) -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                threads.add(threading.get_ident())
            threading.Event().wait(0.005)
            with lock:
                active -= 1

        as400.insert_recovered_row.side_effect = insert
        sqlite = MagicMock()
        sqlite.uploaded_records.return_value = [_uploaded(t) for t in txns]
        rec = As400Recovery(
            sqlite_store=sqlite,
            as400_store=as400,
            indexing_service=indexing,
            mapping_service=mapping,
            write_workers=4,
        )

        result = rec.recover(apply=True)

        assert sorted(result.recovered) == txns
        assert result.unrecoverable == []
        assert as400.insert_recovered_row.call_count == 40
        assert 1 < peak <= 4  # paralelo, pero acotado
        assert len(threads) <= 4

    def test_default_write_workers_is_eight(self) -> None:
        rec = _recovery(uploaded=[], as400=MagicMock(), indexing=MagicMock(), mapping=MagicMock())
        assert rec._write_workers == 8

    def test_one_failing_insert_does_not_block_the_rest(self) -> None:
        txns, as400, indexing, mapping = _missing_setup(30)

        def insert(**kwargs: object) -> None:
            if kwargs["trnnum"] == txns[7]:
                raise RuntimeError("SQLSTATE 23505")

        as400.insert_recovered_row.side_effect = insert
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )

        result = rec.recover(apply=True)

        assert sorted(result.recovered) == [t for t in txns if t != txns[7]]
        assert result.unrecoverable == [RecoveryItem(txns[7], "error: SQLSTATE 23505")]
        assert as400.insert_recovered_row.call_count == 30

    def test_recovered_keeps_tracking_order(self) -> None:
        """El orden de ``recovered`` sigue al de SQLite aunque los INSERT
        terminen desordenados — el reporte es determinista."""
        txns, as400, indexing, mapping = _missing_setup(25)
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )

        result = rec.recover(apply=True)

        assert result.recovered == txns


class TestProgress144:
    def test_sync_progress_is_frozen(self) -> None:
        import dataclasses

        p = SyncProgress("insertando", 1, 2)
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.done = 5  # type: ignore[misc]

    def test_dry_run_emits_read_phases_only(self) -> None:
        txns, as400, indexing, mapping = _missing_setup(3)
        as400.read_states_by_txns.return_value = {txns[0]: object()}
        events: list[SyncProgress] = []
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )

        rec.recover(apply=False, on_progress=events.append)

        assert events == [
            SyncProgress("leyendo tracking", 0, 0),
            SyncProgress("consultando NIARVILOG", 0, 3),
            SyncProgress("consultando RVABREP", 0, 2),
        ]

    def test_apply_emits_insert_progress_every_50_and_at_end(self) -> None:
        txns, as400, indexing, mapping = _missing_setup(120)
        events: list[SyncProgress] = []
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )

        rec.recover(apply=True, on_progress=events.append)

        inserting = [e for e in events if e.phase == "insertando"]
        assert inserting == [
            SyncProgress("insertando", 0, 120),
            SyncProgress("insertando", 50, 120),
            SyncProgress("insertando", 100, 120),
            SyncProgress("insertando", 120, 120),
        ]
        assert [e.phase for e in events[:3]] == [
            "leyendo tracking",
            "consultando NIARVILOG",
            "consultando RVABREP",
        ]

    def test_exact_multiple_of_50_does_not_duplicate_final_event(self) -> None:
        txns, as400, indexing, mapping = _missing_setup(100)
        events: list[SyncProgress] = []
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )

        rec.recover(apply=True, on_progress=events.append)

        inserting = [e for e in events if e.phase == "insertando"]
        assert [e.done for e in inserting] == [0, 50, 100]

    def test_failed_inserts_still_count_as_done(self) -> None:
        txns, as400, indexing, mapping = _missing_setup(60)
        as400.insert_recovered_row.side_effect = RuntimeError("boom")
        events: list[SyncProgress] = []
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )

        result = rec.recover(apply=True, on_progress=events.append)

        assert len(result.unrecoverable) == 60
        assert events[-1] == SyncProgress("insertando", 60, 60)

    def test_raising_callback_does_not_abort_recover(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        txns, as400, indexing, mapping = _missing_setup(2)
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )

        def bad_callback(_: SyncProgress) -> None:
            raise ValueError("UI muerta")

        with caplog.at_level("ERROR", logger="cmcourier.services.recovery"):
            result = rec.recover(apply=True, on_progress=bad_callback)

        assert result.recovered == txns
        assert as400.insert_recovered_row.call_count == 2
        assert any("on_progress" in r.getMessage() for r in caplog.records)

    def test_no_callback_is_fine(self) -> None:
        txns, as400, indexing, mapping = _missing_setup(2)
        rec = _recovery(
            uploaded=[_uploaded(t) for t in txns], as400=as400, indexing=indexing, mapping=mapping
        )
        assert rec.recover(apply=True).recovered == txns
