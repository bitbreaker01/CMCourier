"""Tests del connection pool por-worker de :class:`As400NiarvilogStore` (095).

Pre-095 el store cacheaba una única conexión ``pyodbc`` compartida por
todos los worker threads de S5 — la porción AS400 se serializaba. 095
introduce conexiones thread-local: cada thread abre y cachea la suya.

El fake de ``pyodbc`` de acá crea una conexión NUEVA por cada
``connect()`` (a diferencia de ``test_as400_niarvilog.py``, que reusa
una sola) — así se puede observar cuántas conexiones se abrieron y
quién las usó.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from cmcourier.adapters.tracking import as400_niarvilog as niarvilog_module
from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore
from cmcourier.config.schema import As400ConnectionConfig

from .test_as400_niarvilog import _make_record

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fake de pyodbc que crea una conexión distinta por connect()
# ---------------------------------------------------------------------------


class _PoolFakeCursor:
    """Cursor que registra cada execute() y el thread que lo ejecutó."""

    def __init__(self) -> None:
        self.executions: list[tuple[str, list[Any]]] = []
        self.threads: set[str] = set()
        self.rowcount = 1  # default: el UPDATE de claim gana
        self.raise_queue: list[BaseException | None] = []
        self._columns: list[str] = []
        self._rows: list[list[Any]] = []

    @property
    def description(self) -> list[tuple[str, ...]]:
        return [(c,) for c in self._columns]

    def execute(self, sql: str, params: list[Any] | None = None) -> _PoolFakeCursor:
        self.executions.append((sql, list(params or [])))
        self.threads.add(threading.current_thread().name)
        if self.raise_queue:
            exc = self.raise_queue.pop(0)
            if exc is not None:
                raise exc
        return self

    def fetchall(self) -> list[list[Any]]:
        return []

    def close(self) -> None:
        pass


class _PoolFakeConn:
    def __init__(self) -> None:
        self.cursor_obj = _PoolFakeCursor()
        self.closed = False
        self.commits = 0

    def cursor(self) -> _PoolFakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class _PoolFakeModule:
    """Módulo pyodbc fake — una conexión nueva por cada connect()."""

    class Error(Exception):
        pass

    class IntegrityError(Error):
        pass

    class OperationalError(Error):
        pass

    def __init__(self) -> None:
        self.conns: list[_PoolFakeConn] = []
        self._lock = threading.Lock()

    def connect(self, cs: str) -> _PoolFakeConn:  # noqa: ARG002
        conn = _PoolFakeConn()
        with self._lock:
            self.conns.append(conn)
        return conn


def _make_pool_store(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[As400NiarvilogStore, _PoolFakeModule]:
    module = _PoolFakeModule()
    monkeypatch.setattr(niarvilog_module, "pyodbc", module)
    store = As400NiarvilogStore(
        connection=As400ConnectionConfig(host="10.0.0.1", database="RVILIB"),
        username="tester",
        password="secret",
        retry_attempts=2,
        retry_base_delay_s=0.001,
    )
    return store, module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_each_worker_thread_opens_its_own_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N threads ejecutando try_claim → N conexiones distintas (una por thread)."""
    store, module = _make_pool_store(monkeypatch)
    n_workers = 8
    # Barrier: cada thread bloquea hasta que los N llegaron — fuerza
    # concurrencia real (si no, ThreadPoolExecutor reusa un thread veloz).
    barrier = threading.Barrier(n_workers)

    def claim(i: int) -> bool:
        record, document, mapping, trigger = _make_record(txn=f"000000{i}")
        barrier.wait()
        return store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        results = list(pool.map(claim, range(n_workers)))

    assert all(results)
    # Una conexión por thread — no una sola compartida.
    assert len(module.conns) == n_workers
    # Cada conexión la tocó exactamente un thread → sin sharing cross-thread.
    for conn in module.conns:
        assert len(conn.cursor_obj.threads) == 1
    store.close()


def test_same_thread_reuses_its_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dos operaciones en el mismo thread reusan la conexión cacheada."""
    store, module = _make_pool_store(monkeypatch)
    for i in range(3):
        record, document, mapping, trigger = _make_record(txn=f"000000{i}")
        store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)
    assert len(module.conns) == 1


def test_close_closes_every_pooled_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() cierra TODAS las conexiones, sin importar qué thread la llame."""
    store, module = _make_pool_store(monkeypatch)
    n_workers = 5
    barrier = threading.Barrier(n_workers)

    def claim(i: int) -> None:
        record, document, mapping, trigger = _make_record(txn=f"000000{i}")
        barrier.wait()
        store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        list(pool.map(claim, range(n_workers)))

    assert len(module.conns) == n_workers
    store.close()  # llamado desde el thread principal
    assert all(conn.closed for conn in module.conns)


def test_retry_resets_only_the_calling_threads_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un OperationalError resetea solo la conexión del thread que reintenta."""
    store, module = _make_pool_store(monkeypatch)
    record, document, mapping, trigger = _make_record()

    # El primer execute del primer cursor falla transitoriamente; el
    # retry abre una conexión nueva y triunfa.
    def connect_with_first_failing(cs: str) -> _PoolFakeConn:  # noqa: ARG001
        conn = _PoolFakeConn()
        if not module.conns:
            conn.cursor_obj.raise_queue = [module.OperationalError("transient")]
        with module._lock:
            module.conns.append(conn)
        return conn

    monkeypatch.setattr(module, "connect", connect_with_first_failing)

    assert store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)
    # Dos conexiones: la primera (fallida, reseteada) y la del retry.
    assert len(module.conns) == 2
    assert module.conns[0].closed is True  # reseteada por el retry
    assert module.conns[1].closed is False  # la del retry, viva
    store.close()


def test_dead_worker_connections_are_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    """106: los ThreadPoolExecutor de S5 se reciclan por chunk. Las
    conexiones de los threads muertos del chunk anterior deben cerrarse
    cuando los threads del chunk nuevo abren las suyas — sin esto se
    acumulan cientos de jobs QZDASOINIT en el iSeries."""
    store, module = _make_pool_store(monkeypatch)

    def wave(n: int, offset: int) -> None:
        # Doble barrera: ningún thread de la ola muere hasta que todos
        # reclamaron — así la poda solo puede ocurrir ENTRE olas.
        start = threading.Barrier(n)
        done = threading.Barrier(n)

        def claim(i: int) -> None:
            record, document, mapping, trigger = _make_record(txn=f"00000{offset + i}")
            start.wait()
            store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)
            done.wait()

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    wave(4, 0)
    first_wave = list(module.conns)
    assert all(not c.closed for c in first_wave)

    wave(4, 4)

    assert all(c.closed for c in first_wave), (
        "las conexiones de threads muertos quedaron abiertas (fuga 106)"
    )
    assert all(not c.closed for c in module.conns[4:])
    store.close()
