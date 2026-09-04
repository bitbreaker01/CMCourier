"""Tests unitarios de :class:`ThreadLocalConnectionPool` (106).

El pool encapsula el patrón de conexiones ODBC thread-local (095) y le
agrega la poda de conexiones cuyos threads ya murieron — el fix de la
fuga de conexiones que acumulaba jobs QZDASOINIT en el iSeries cuando
los ThreadPoolExecutor de S5 se reciclaban por chunk.

El pool no conoce pyodbc: recibe un factory y objetos con ``close()``.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cmcourier.adapters.connection_pool import ThreadLocalConnectionPool

pytestmark = pytest.mark.unit


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False
        self.thread_name = threading.current_thread().name

    def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self) -> None:
        self.conns: list[_FakeConn] = []
        self._lock = threading.Lock()

    def __call__(self) -> _FakeConn:
        conn = _FakeConn()
        with self._lock:
            self.conns.append(conn)
        return conn


def _run_in_fresh_threads(pool: ThreadLocalConnectionPool, n: int) -> None:
    """Fuerza n threads NUEVOS (no reciclados) que hacen acquire().

    Doble barrera: ningún thread muere hasta que TODOS adquirieron —
    sin eso, un thread rápido muere antes de que el último adquiera y
    la poda (correcta) del pool le cierra la conexión en plena ola.
    """
    start = threading.Barrier(n)
    done = threading.Barrier(n)

    def work() -> None:
        start.wait()
        pool.acquire()
        done.wait()

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class TestThreadLocalCaching:
    def test_same_thread_reuses_connection(self) -> None:
        factory = _Factory()
        pool = ThreadLocalConnectionPool(factory)
        c1 = pool.acquire()
        c2 = pool.acquire()
        assert c1 is c2
        assert len(factory.conns) == 1

    def test_each_thread_gets_its_own_connection(self) -> None:
        factory = _Factory()
        pool = ThreadLocalConnectionPool(factory)
        n = 6
        barrier = threading.Barrier(n)
        acquired: list[_FakeConn] = []
        lock = threading.Lock()

        def work() -> None:
            barrier.wait()
            conn = pool.acquire()
            with lock:
                acquired.append(conn)

        with ThreadPoolExecutor(max_workers=n) as tp:
            for _ in range(n):
                tp.submit(work)

        assert len(factory.conns) == n
        assert len({id(c) for c in acquired}) == n


class TestDeadThreadPruning:
    def test_new_connection_prunes_dead_threads(self) -> None:
        # E2: la primera "ola" de threads conecta y muere; cuando la
        # segunda ola conecta, las conexiones de la primera se cierran.
        factory = _Factory()
        pool = ThreadLocalConnectionPool(factory)

        _run_in_fresh_threads(pool, 4)
        first_wave = list(factory.conns)
        assert all(not c.closed for c in first_wave), "aún no hay poda que hacer"

        _run_in_fresh_threads(pool, 4)

        assert all(c.closed for c in first_wave), (
            "las conexiones de threads muertos deben cerrarse cuando un thread nuevo abre la suya"
        )
        second_wave = factory.conns[4:]
        assert all(not c.closed for c in second_wave)
        assert pool.open_count == 4

    def test_live_thread_connections_survive_pruning(self) -> None:
        factory = _Factory()
        pool = ThreadLocalConnectionPool(factory)
        main_conn = pool.acquire()  # el thread principal sigue vivo

        _run_in_fresh_threads(pool, 3)
        _run_in_fresh_threads(pool, 3)  # dispara la poda de la ola anterior

        assert main_conn.closed is False
        assert main_conn is pool.acquire()


class TestResetAndClose:
    def test_reset_current_closes_only_own_connection(self) -> None:
        factory = _Factory()
        pool = ThreadLocalConnectionPool(factory)
        other_conns: list[_FakeConn] = []
        # El otro thread queda VIVO durante el reset — si muriera, el
        # acquire del main thread lo podaría (comportamiento correcto)
        # y el test dejaría de medir lo que quiere medir.
        connected = threading.Event()
        release = threading.Event()

        def work() -> None:
            other_conns.append(pool.acquire())
            connected.set()
            release.wait(timeout=10)

        t = threading.Thread(target=work)
        t.start()
        connected.wait(timeout=10)

        mine = pool.acquire()
        pool.reset_current()
        assert mine.closed is True
        # reset no toca la del otro thread.
        assert other_conns[0].closed is False
        # el próximo acquire del mismo thread abre una nueva.
        fresh = pool.acquire()
        assert fresh is not mine
        release.set()
        t.join()

    def test_reset_current_without_connection_is_noop(self) -> None:
        pool = ThreadLocalConnectionPool(_Factory())
        pool.reset_current()  # no explota

    def test_close_all_closes_everything_from_any_thread(self) -> None:
        factory = _Factory()
        pool = ThreadLocalConnectionPool(factory)
        pool.acquire()
        _run_in_fresh_threads(pool, 3)

        pool.close_all()
        assert all(c.closed for c in factory.conns)
        assert pool.open_count == 0

    def test_close_errors_are_swallowed(self) -> None:
        class _ExplodingConn(_FakeConn):
            def close(self) -> None:
                super().close()
                raise RuntimeError("boom")

        pool = ThreadLocalConnectionPool(_ExplodingConn)
        pool.acquire()
        pool.close_all()  # no propaga

    def test_factory_errors_propagate_and_register_nothing(self) -> None:
        class _BoomError(Exception):
            pass

        def factory() -> _FakeConn:
            raise _BoomError()

        pool = ThreadLocalConnectionPool(factory)
        with pytest.raises(_BoomError):
            pool.acquire()
        assert pool.open_count == 0
