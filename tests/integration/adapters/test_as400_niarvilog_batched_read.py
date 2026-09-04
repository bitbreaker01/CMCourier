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

    @property
    def description(self) -> list[tuple[str, ...]]:
        return [(c,) for c in _COLS]

    def execute(self, sql: str, params: list[Any] | None = None) -> _ReadFakeCursor:
        params = list(params or [])
        self._module.executed.append((sql, params))
        if sql.lstrip().upper().startswith("SELECT"):
            self._rows = [_row_for(str(p)) for p in params if str(p) in self._module.known_txns]
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
