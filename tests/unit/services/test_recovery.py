"""Tests de :class:`As400Recovery` (099).

El recuperador es lógica de orquestación pura — stores y servicios se
mockean con :class:`MagicMock`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.tracking.sqlite import UploadedRecord
from cmcourier.domain.exceptions import IDRViNotMappedError
from cmcourier.services.recovery import As400Recovery, RecoveryItem

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
        indexing.find_document_by_txn.return_value = _document()
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
        indexing.find_document_by_txn.return_value = _document(index7="FF17", image_type="O")
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
        indexing.find_document_by_txn.return_value = _document()
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
        indexing.find_document_by_txn.return_value = None  # sin fila RVABREP
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
        indexing.find_document_by_txn.return_value = _document(index7="CC99")
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
        indexing.find_document_by_txn.return_value = _document()
        mapping = MagicMock()
        mapping.get_mapping.return_value = _mapping()
        # El INSERT del 2do txn explota; el 1ro y el 3ro están OK.
        as400.insert_recovered_row.side_effect = [None, RuntimeError("AS400 boom"), None]
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
