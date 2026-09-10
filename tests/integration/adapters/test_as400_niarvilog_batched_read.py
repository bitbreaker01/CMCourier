"""Tests de la lectura batcheada de NIARVILOG (113).

Pre-113 el pre-flight hacía un ``SELECT ... WHERE TRNNUM = ?`` por txn
del batch — 1000 round-trips secuenciales al iSeries antes de arrancar
el pipeline. 113 agrega ``read_states_by_txns``: IN chunkeado de a
1000, un round-trip por chunk.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import pytest

from cmcourier.adapters.tracking import as400_niarvilog as niarvilog_module
from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore
from cmcourier.config.schema import As400ConnectionConfig

pytestmark = pytest.mark.integration

_COLS = [
    "SISCOD",
    "TRNNUM",
    "DOCFRM",
    "IMGARC",
    "IMGTIP",
    "CTECIF",
    "CTENUM",
    "STSCOD",
    "IDNBAC",
    "TIPIDN",
    "OBJIDN",
    "NUMREI",
    "PMRREI",
    "FINREI",
    "EERRMSG",
]


def _row_for(txn: str) -> list[Any]:
    now = datetime(2026, 9, 4, 10, 0, 0)
    return ["1", txn, "CC03", "F.001", "B", "CLI", 123, "O", "CN01", "T", "oid", 0, now, now, ""]


class _ReadFakeCursor:
    """Cursor que responde a los SELECT con filas para los txns conocidos."""

    def __init__(self, module: _ReadFakeModule) -> None:
        self._module = module
        self._rows: list[list[Any]] = []
        self.rowcount = module.write_rowcount

    @property
    def description(self) -> list[tuple[str, ...]]:
        return [(c,) for c in _COLS]

    def execute(self, sql: str, params: list[Any] | None = None) -> _ReadFakeCursor:
        params = list(params or [])
        self._module.executed.append((sql, params))
        upper = sql.lstrip().upper()
        if upper.startswith("SELECT"):
            self._rows = [_row_for(str(p)) for p in params if str(p) in self._module.known_txns]
        elif upper.startswith("INSERT") and self._module.raise_integrity_on_insert:
            raise self._module.IntegrityError("duplicate")
        return self

    def fetchall(self) -> list[list[Any]]:
        return self._rows

    def close(self) -> None:
        pass


class _ReadFakeConn:
    def __init__(self, module: _ReadFakeModule) -> None:
        self._module = module

    def cursor(self) -> _ReadFakeCursor:
        return _ReadFakeCursor(self._module)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class _ReadFakeModule:
    class Error(Exception):
        pass

    class IntegrityError(Error):
        pass

    class OperationalError(Error):
        pass

    def __init__(self, known_txns: set[str]) -> None:
        self.known_txns = known_txns
        self.executed: list[tuple[str, list[Any]]] = []
        self.write_rowcount = 1
        self.raise_integrity_on_insert = False
        self._lock = threading.Lock()

    def connect(self, cs: str) -> _ReadFakeConn:  # noqa: ARG002
        return _ReadFakeConn(self)


def _make_store(
    monkeypatch: pytest.MonkeyPatch, known_txns: set[str]
) -> tuple[As400NiarvilogStore, _ReadFakeModule]:
    module = _ReadFakeModule(known_txns)
    monkeypatch.setattr(niarvilog_module, "pyodbc", module)
    store = As400NiarvilogStore(
        connection=As400ConnectionConfig(host="10.0.0.1", database="RVILIB"),
        username="tester",
        password="secret",
        retry_attempts=2,
        retry_base_delay_s=0.001,
    )
    return store, module


class TestReadStatesByTxns:
    def test_2500_txns_issue_3_queries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # E1: ceil(2500/1000) = 3 sentencias IN, no 2500.
        txns = [f"{i:07d}" for i in range(2500)]
        store, module = _make_store(monkeypatch, set(txns))
        result = store.read_states_by_txns(txns)
        selects = [sql for sql, _ in module.executed if "SELECT" in sql]
        assert len(selects) == 3
        assert len(result) == 2500
        assert result["0000042"].stscod == "O"
        store.close()

    def test_missing_txns_absent_from_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, _module = _make_store(monkeypatch, {"0000001"})
        result = store.read_states_by_txns(["0000001", "0000002"])
        assert set(result) == {"0000001"}
        store.close()

    def test_empty_input_issues_no_queries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, module = _make_store(monkeypatch, set())
        assert store.read_states_by_txns([]) == {}
        assert module.executed == []
        store.close()


class TestTerminalWrites117:
    """117: propagación en un solo write guardado, sin paso por 'I'."""

    def _doc_pack(self):  # type: ignore[no-untyped-def]
        from tests.integration.adapters.test_as400_niarvilog import _make_record

        record, document, mapping, trigger = _make_record(txn="0000042")
        return record, document, mapping, trigger

    def test_update_terminal_true_when_row_updated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, module = _make_store(monkeypatch, set())
        _record, document, mapping, trigger = self._doc_pack()
        ok = store.update_terminal_if_new(
            document=document,
            mapping=mapping,
            trigger=trigger,
            stscod="O",
            cm_object_id="cm-42",
        )
        assert ok is True
        sql, params = module.executed[-1]
        assert "STSCOD = 'N'" in sql and "UPDATE" in sql
        assert "cm-42" in params
        store.close()

    def test_update_terminal_false_on_race(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # rowcount 0 → otro proceso tocó la fila entre read y write.
        store, module = _make_store(monkeypatch, set())
        module.write_rowcount = 0
        _record, document, mapping, trigger = self._doc_pack()
        assert (
            store.update_terminal_if_new(
                document=document, mapping=mapping, trigger=trigger, stscod="O"
            )
            is False
        )
        store.close()

    def test_insert_terminal_false_on_integrity_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, module = _make_store(monkeypatch, set())
        module.raise_integrity_on_insert = True
        record, document, mapping, trigger = self._doc_pack()
        assert (
            store.insert_terminal(
                # 147 REQ-004: CTECIF / CTENUM salen del record, no del trigger.
                record=record,
                document=document,
                mapping=mapping,
                trigger=trigger,
                stscod="O",
            )
            is False
        )
        store.close()

    def test_reconcile_pass_uses_one_read_plus_one_write_per_item(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E1: 3 items ausentes = 1 cleanup + 1 SELECT batcheado + 3
        INSERTs — pre-117 eran ~3 sentencias POR item."""
        from unittest.mock import MagicMock

        from cmcourier.services.reconciler import As400Reconciler, PendingSyncItem
        from tests.integration.adapters.test_as400_niarvilog import _make_record

        store, module = _make_store(monkeypatch, set())
        items = []
        for i in range(3):
            record, document, mapping, trigger = _make_record(txn=f"000010{i}")
            items.append(
                PendingSyncItem(
                    record=record,
                    document=document,
                    mapping=mapping,
                    trigger=trigger,
                    outcome="uploaded",
                    cm_object_id=f"cm-{i}",
                )
            )
        rec = As400Reconciler(sqlite_store=MagicMock(), as400_store=store)
        result = rec.run_pass(items)
        assert len(result.synced_to_as400) == 3
        selects = [s for s, _ in module.executed if s.lstrip().upper().startswith("SELECT")]
        inserts = [s for s, _ in module.executed if s.lstrip().upper().startswith("INSERT")]
        updates = [s for s, _ in module.executed if s.lstrip().upper().startswith("UPDATE")]
        assert len(selects) == 1, "la lectura debe ser UNA, batcheada"
        assert len(inserts) == 3
        assert len(updates) == 1, "solo el cleanup de stale — sin claims intermedios"
        store.close()
