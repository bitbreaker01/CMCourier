"""Tests de concurrencia de lectura del tracking store (107).

Pre-107 todas las lecturas pasaban por UNA conexión compartida
serializada por ``_reader_lock`` — con 50-100 worker threads de S5 más
el polling del TUI a 4 Hz, ese lock era un punto de serialización
global. 107 da a cada thread su propia conexión de lectura (WAL permite
lectores concurrentes con conexiones separadas) y agrega el índice
``idx_migration_log_batch`` que les saca el full scan a las queries
por batch.
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore
from cmcourier.domain.models import MigrationRecord, StageStatus

pytestmark = pytest.mark.integration


def _make_record(txn: str, batch_id: str) -> MigrationRecord:
    return MigrationRecord(
        trigger_shortname="SHORT1",
        trigger_cif="123",
        trigger_system_id="A",
        rvabrep_txn_num=txn,
        rvabrep_file_name=f"{txn}.001",
        batch_id=batch_id,
        status=StageStatus.S1_PENDING,
        created_at=datetime.now(),
    )


@pytest.fixture
def store(tmp_path: Path) -> SQLiteTrackingStore:
    s = SQLiteTrackingStore(tmp_path / "tracking.db")
    yield s
    s.close()


class TestBatchIdIndex:
    def test_index_exists_on_fresh_store(self, store: SQLiteTrackingStore, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(tmp_path / "tracking.db"))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_migration_log_batch",),
            ).fetchall()
        finally:
            conn.close()
        assert rows, "falta el índice idx_migration_log_batch (107)"

    def test_list_docs_query_plan_uses_index(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        # E2: sin el índice, el plan dice "SCAN migration_log".
        conn = sqlite3.connect(str(tmp_path / "tracking.db"))
        try:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT rvabrep_txn_num FROM migration_log WHERE batch_id = ?",
                ("b1",),
            ).fetchall()
        finally:
            conn.close()
        plan_text = " ".join(str(row) for row in plan)
        assert "idx_migration_log_batch" in plan_text, (
            f"la query por batch_id no usa el índice: {plan_text}"
        )

    def test_preexisting_db_acquires_index_on_reopen(self, tmp_path: Path) -> None:
        # E3: simula una DB creada por una versión pre-107 (sin índice).
        db = tmp_path / "old.db"
        store = SQLiteTrackingStore(db)
        store.close()
        conn = sqlite3.connect(str(db))
        conn.execute("DROP INDEX idx_migration_log_batch")
        conn.commit()
        conn.close()

        reopened = SQLiteTrackingStore(db)
        try:
            check = sqlite3.connect(str(db))
            rows = check.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_migration_log_batch",),
            ).fetchall()
            check.close()
            assert rows
        finally:
            reopened.close()


class TestConcurrentReads:
    def test_parallel_reads_while_writer_commits(self, store: SQLiteTrackingStore) -> None:
        """E1: 16 threads leyendo en paralelo mientras se encolan
        escrituras — sin lock global, sin errores, resultado consistente."""
        batch_id = store.start_batch(total_records=200)
        for i in range(200):
            record = _make_record(f"{i:07d}", batch_id)
            store.mark_stage_pending(record, StageStatus.S1_PENDING)
            store.mark_stage_done(f"{i:07d}", batch_id, StageStatus.S1_DONE)
        store.flush()

        n_threads = 16
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def read_loop(worker: int) -> int:
            barrier.wait()
            hits = 0
            try:
                for i in range(200):
                    txn = f"{i:07d}"
                    if store.is_stage_done(txn, batch_id, StageStatus.S1_DONE):
                        hits += 1
                    store.is_uploaded(txn)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            return hits

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            results = list(pool.map(read_loop, range(n_threads)))

        assert not errors, f"lecturas concurrentes fallaron: {errors[0]}"
        assert all(hits == 200 for hits in results)

    def test_each_reader_thread_gets_own_connection(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=1)
        n = 6
        barrier = threading.Barrier(n)
        done = threading.Barrier(n)

        def read(_: int) -> None:
            barrier.wait()
            store.is_uploaded("0000001")
            store.list_txn_nums_for_batch(batch_id)
            done.wait()

        threads = [threading.Thread(target=read, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # n threads lectores + posiblemente el main thread.
        assert store._read_pool.open_count >= n  # noqa: SLF001

    def test_read_after_flush_sees_writes_from_any_thread(self, store: SQLiteTrackingStore) -> None:
        """E4: paridad con el contrato actual — flush() garantiza
        visibilidad desde cualquier thread."""
        batch_id = store.start_batch(total_records=1)
        record = _make_record("0009999", batch_id)
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_done("0009999", batch_id, StageStatus.S1_DONE)
        store.flush()

        seen: list[bool] = []

        def check() -> None:
            seen.append(store.is_stage_done("0009999", batch_id, StageStatus.S1_DONE))

        t = threading.Thread(target=check)
        t.start()
        t.join()
        assert seen == [True]

    def test_dead_reader_threads_are_pruned(self, store: SQLiteTrackingStore) -> None:
        """E5: las conexiones de lectura de threads muertos se podan
        (los pools de S5 se reciclan por chunk)."""

        def wave(n: int) -> None:
            start = threading.Barrier(n)
            done = threading.Barrier(n)

            def read(_: int) -> None:
                start.wait()
                store.is_uploaded("0000001")
                done.wait()

            threads = [threading.Thread(target=read, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        wave(4)
        count_after_first = store._read_pool.open_count  # noqa: SLF001
        wave(4)
        # La segunda ola podó a la primera: el total registrado no crece
        # linealmente con el histórico de threads.
        assert store._read_pool.open_count <= count_after_first  # noqa: SLF001
