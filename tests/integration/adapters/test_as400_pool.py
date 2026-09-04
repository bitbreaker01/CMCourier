"""Tests del pool de conexiones thread-local de :class:`As400DataSource` (106).

Pre-106 el adapter cacheaba UNA conexión pyodbc en ``self._conn`` sin
sincronización — pyodbc declara ``threadsafety = 1`` (las conexiones no
son compartibles entre threads), así que con ``prep_workers > 1`` los
cursores concurrentes de S1/S3 caían sobre la misma conexión.

El fake replica el de ``test_as400_niarvilog_pool.py``: una conexión
nueva por cada ``connect()`` para poder contar cuántas se abrieron y
qué thread tocó cada una.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from cmcourier.adapters.sources import as400 as as400_module
from cmcourier.adapters.sources.as400 import As400DataSource

pytestmark = pytest.mark.integration


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @property
    def description(self) -> list[tuple[str, ...]]:
        return [("TRNNUM",)]

    def execute(self, sql: str, params: list[Any] | None = None) -> _FakeCursor:
        self._conn.executions.append((sql, list(params or [])))
        self._conn.threads.add(threading.current_thread().name)
        return self

    def fetchall(self) -> list[list[Any]]:
        return [["0001"]]

    def fetchmany(self, size: int) -> list[list[Any]]:
        if self._conn.stream_drained:
            return []
        self._conn.stream_drained = True
        return [["0001"]]

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self) -> None:
        self.executions: list[tuple[str, list[Any]]] = []
        self.threads: set[str] = set()
        self.closed = False
        self.stream_drained = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class _FakeModule:
    class Error(Exception):
        pass

    def __init__(self) -> None:
        self.conns: list[_FakeConn] = []
        self._lock = threading.Lock()

    def connect(self, cs: str) -> _FakeConn:  # noqa: ARG002
        conn = _FakeConn()
        with self._lock:
            self.conns.append(conn)
        return conn


def _make_source(monkeypatch: pytest.MonkeyPatch) -> tuple[As400DataSource, _FakeModule]:
    module = _FakeModule()
    monkeypatch.setattr(as400_module, "pyodbc", module)
    source = As400DataSource(
        host="10.0.0.1",
        port=446,
        database="RVILIB",
        driver="IBM i Access ODBC Driver",
        username="tester",
        password="secret",
        table="RVILIB.RVABREP",
    )
    return source, module


def test_each_thread_opens_its_own_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """E1: N threads concurrentes → N conexiones, una por thread."""
    source, module = _make_source(monkeypatch)
    n_workers = 8
    barrier = threading.Barrier(n_workers)

    def query(i: int) -> list[dict[str, Any]]:
        barrier.wait()
        return source.get_by_fields({"TRNNUM": f"000{i}"})

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        results = list(pool.map(query, range(n_workers)))

    assert all(rows == [{"TRNNUM": "0001"}] for rows in results)
    assert len(module.conns) == n_workers
    for conn in module.conns:
        assert len(conn.threads) == 1, "una conexión fue compartida entre threads"
    source.close()


def test_same_thread_reuses_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    source, module = _make_source(monkeypatch)
    for i in range(3):
        source.get_by_fields({"TRNNUM": f"000{i}"})
    assert len(module.conns) == 1
    source.close()


def test_close_closes_all_thread_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """E3: close() desde el thread principal cierra las de todos los threads."""
    source, module = _make_source(monkeypatch)
    n_workers = 5
    barrier = threading.Barrier(n_workers)

    def query(i: int) -> None:
        barrier.wait()
        source.get_by_fields({"TRNNUM": f"000{i}"})

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        list(pool.map(query, range(n_workers)))

    assert len(module.conns) == n_workers
    source.close()
    assert all(conn.closed for conn in module.conns)


def test_dead_worker_connections_are_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2: los prep_workers se reciclan por chunk — las conexiones de la
    ola anterior deben cerrarse cuando la nueva ola conecta."""
    source, module = _make_source(monkeypatch)

    def wave(n: int) -> None:
        # Doble barrera: ningún thread de la ola muere hasta que todos
        # consultaron — así la poda solo puede ocurrir ENTRE olas.
        start = threading.Barrier(n)
        done = threading.Barrier(n)

        def query(i: int) -> None:
            start.wait()
            source.get_by_fields({"TRNNUM": f"000{i}"})
            done.wait()

        threads = [threading.Thread(target=query, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    wave(4)
    first_wave = list(module.conns)
    wave(4)

    assert all(c.closed for c in first_wave), (
        "las conexiones de threads muertos quedaron abiertas (fuga 106)"
    )
    assert all(not c.closed for c in module.conns[4:])
    source.close()


def test_query_stream_works_with_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    source, module = _make_source(monkeypatch)
    rows = list(source.get_all())
    assert rows == [{"TRNNUM": "0001"}]
    source.close()
