"""Tests de integración para :class:`SQLiteTrackingStore`.

Pega contra un archivo SQLite real (``tmp_path`` por test) así ejercitamos
el modo WAL, los PRAGMAs, la cola del writer async, y el schema todo de
una. Sin `mockear` la base — Principio VI de la Constitución.

Los escenarios de aceptación de ``specs/007-sqlite-tracking-store/spec.md``
§4 mapean 1:1 contra los tests nombrados acá abajo.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from cmcourier.adapters.tracking import SQLiteTrackingStore
from cmcourier.domain.exceptions import TrackingError
from cmcourier.domain.models import MigrationRecord, ReasonBucket, ReasonCode, StageStatus

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(batch_id: str, txn_num: str, **overrides: object) -> MigrationRecord:
    """Construye un :class:`MigrationRecord` sintético para los tests.

    Todos los campos identificatorios son placeholders, así un `grep` de
    `PII` sobre el archivo del test no devuelve nada real.
    """
    defaults: dict[str, object] = {
        "trigger_shortname": "TESTUSER001",
        "trigger_cif": "000000",
        "trigger_system_id": "1",
        "rvabrep_txn_num": txn_num,
        "rvabrep_file_name": "TESTFILE.001",
        "batch_id": batch_id,
        "status": StageStatus.S1_PENDING,
        "created_at": datetime(2026, 1, 1, 0, 0),
    }
    defaults.update(overrides)
    return MigrationRecord(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> SQLiteTrackingStore:
    """Abre un store nuevo respaldado por ``tmp_path/tracking.db``.

    El `fixture` a propósito NO llama a ``close()`` en el teardown porque
    algunos tests hacen `assert` sobre la semántica de ``close()``. Los
    tests que necesitan un teardown limpio llaman ``close()`` ellos
    mismos.
    """
    return SQLiteTrackingStore(tmp_path / "tracking.db")


# ---------------------------------------------------------------------------
# Grupo 1 — Schema (aceptación §4.1, REQ-014..018)
# ---------------------------------------------------------------------------


class TestSchema:
    def test_init_creates_both_tables(self, store: SQLiteTrackingStore, tmp_path: Path) -> None:
        # Lee el schema con una conexión paralela así no metemos los dedos
        # en los internos del store.
        conn = sqlite3.connect(tmp_path / "tracking.db")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()
        store.close()
        assert "migration_log" in tables
        assert "migration_batch" in tables

    def test_init_creates_required_indexes(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        conn = sqlite3.connect(tmp_path / "tracking.db")
        idx = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        conn.close()
        store.close()
        # Los dos índices que la spec exige por nombre (REQ-016, REQ-017).
        assert "idx_migration_log_txn_batch" in idx
        assert "idx_migration_log_uploaded" in idx

    def test_init_is_idempotent_on_existing_db(self, tmp_path: Path) -> None:
        # Abrir la misma DB dos veces NO debe levantar (CREATE TABLE IF NOT EXISTS).
        s1 = SQLiteTrackingStore(tmp_path / "tracking.db")
        s1.close()
        s2 = SQLiteTrackingStore(tmp_path / "tracking.db")
        s2.close()  # si esto levanta, el bootstrap del schema no es idempotente


# ---------------------------------------------------------------------------
# Grupo 2 — Ciclo de vida del `batch` (aceptación §4.2, §4.3, REQ-019..021)
# ---------------------------------------------------------------------------


class TestBatchLifecycle:
    def test_start_batch_returns_non_empty_string(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=10)
        store.close()
        assert isinstance(batch_id, str)
        assert len(batch_id) > 0

    def test_start_batch_persists_synchronously(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        # No hace falta flush() — start_batch tiene que ser síncrono según REQ-019.
        batch_id = store.start_batch(total_records=42)
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT batch_id, total_records FROM migration_batch WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        conn.close()
        store.close()
        assert row is not None
        assert row[0] == batch_id
        assert row[1] == 42

    def test_start_batch_returns_unique_ids(self, store: SQLiteTrackingStore) -> None:
        ids = {store.start_batch(total_records=1) for _ in range(10)}
        store.close()
        assert len(ids) == 10  # sin colisiones

    def test_complete_batch_sets_completed_at(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        batch_id = store.start_batch(total_records=5)
        store.complete_batch(batch_id)
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT completed_at FROM migration_batch WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        conn.close()
        store.close()
        assert row is not None
        assert row[0] is not None  # string de timestamp ISO escrito


# ---------------------------------------------------------------------------
# Grupo 3 — Máquina de estados por `stage` (aceptación §4.4..§4.7, REQ-022..024)
# ---------------------------------------------------------------------------


class TestPerStageState:
    def test_mark_stage_pending_inserts_row(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN001")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT status FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("TXN001", batch_id),
        ).fetchone()
        conn.close()
        store.close()
        assert row is not None
        assert row[0] == "S1_PENDING"

    def test_mark_stage_pending_idempotent_within_batch(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        # Llamar a pending DOS veces para el mismo (txn, batch) no debe duplicar.
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN002")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        count = conn.execute(
            "SELECT COUNT(*) FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("TXN002", batch_id),
        ).fetchone()[0]
        conn.close()
        store.close()
        assert count == 1

    def test_mark_stage_done_transitions_row(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN003")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_done("TXN003", batch_id, StageStatus.S1_DONE)
        store.flush()
        assert store.is_stage_done("TXN003", batch_id, StageStatus.S1_DONE) is True
        store.close()

    def test_mark_stage_done_persists_cm_object_id(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        """047: la transición S5_DONE lleva el `objectId` de `cmis` así la
        DB de tracking puede responder "¿cuál es el `objectId` del doc X?"
        sin caminar los hijos contra el servidor `cmis` (§L.3 del checklist)."""
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN_OID")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_done(
            "TXN_OID", batch_id, StageStatus.S5_DONE, cm_object_id="cm-workspace://abc-123"
        )
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT status, cm_object_id FROM migration_log "
            "WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("TXN_OID", batch_id),
        ).fetchone()
        conn.close()
        store.close()
        assert row is not None
        assert row[0] == "S5_DONE"
        assert row[1] == "cm-workspace://abc-123"

    def test_mark_stage_done_without_oid_leaves_column(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        """047: el camino S1..S4 no pasa cm_object_id — el UPDATE NO debe
        tocar la columna, así un valor seteado antes sobrevive. Lo seteamos
        vía una llamada S5_DONE, después un S1_DONE colado sobre la misma
        fila no lo debe borrar."""
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN_KEEP")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_done("TXN_KEEP", batch_id, StageStatus.S5_DONE, cm_object_id="cm-keepme")
        # Una transición posterior sin el kwarg (la forma S1..S4) tiene
        # que dejar cm_object_id intacto.
        store.mark_stage_done("TXN_KEEP", batch_id, StageStatus.S1_DONE)
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT cm_object_id FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("TXN_KEEP", batch_id),
        ).fetchone()
        conn.close()
        store.close()
        assert row is not None
        assert row[0] == "cm-keepme"

    def test_mark_stage_failed_stores_error_and_bumps_retry(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN004")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_failed("TXN004", batch_id, StageStatus.S1_FAILED, "connection lost")
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT status, error_message, retry_count "
            "FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("TXN004", batch_id),
        ).fetchone()
        conn.close()
        store.close()
        assert row is not None
        assert row[0] == "S1_FAILED"
        assert row[1] == "connection lost"
        assert row[2] == 1

    def test_mark_stage_failed_increments_retry_each_call(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN005")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_failed("TXN005", batch_id, StageStatus.S1_FAILED, "boom 1")
        store.mark_stage_failed("TXN005", batch_id, StageStatus.S1_FAILED, "boom 2")
        store.mark_stage_failed("TXN005", batch_id, StageStatus.S1_FAILED, "boom 3")
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        retry = conn.execute(
            "SELECT retry_count FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("TXN005", batch_id),
        ).fetchone()[0]
        conn.close()
        store.close()
        assert retry == 3

    def test_mark_stage_terminal_writes_filtered_with_reason(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        # 062: transición terminal para S1_FILTERED (filas de origen con delete-code).
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "FILTERED__SHORTNAME__1")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_terminal(
            "FILTERED__SHORTNAME__1",
            batch_id,
            StageStatus.S1_FILTERED,
            "deleted_at_source; deleted_count=3",
        )
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT status, error_message, retry_count, completed_at "
            "FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("FILTERED__SHORTNAME__1", batch_id),
        ).fetchone()
        conn.close()
        store.close()
        assert row[0] == "S1_FILTERED"
        assert "deleted_at_source" in row[1]
        assert "deleted_count=3" in row[1]
        # Contrato 062: `filtered` NO es una falla → retry_count sin cambios.
        assert row[2] == 0
        assert row[3] is not None  # completed_at seteado

    def test_mark_stage_terminal_writes_skipped_with_reason(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        # 062: transición terminal para S1_SKIPPED (idempotencia cross-batch).
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN_SKIPPED_001")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_terminal(
            "TXN_SKIPPED_001",
            batch_id,
            StageStatus.S1_SKIPPED,
            "cross_batch_uploaded",
        )
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT status, error_message, retry_count "
            "FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("TXN_SKIPPED_001", batch_id),
        ).fetchone()
        conn.close()
        store.close()
        assert row[0] == "S1_SKIPPED"
        assert row[1] == "cross_batch_uploaded"
        assert row[2] == 0  # no es falla → retry no se incrementa

    def test_mark_stage_terminal_rejects_non_terminal_status(
        self, store: SQLiteTrackingStore
    ) -> None:
        # El validador solo acepta FAILED / FILTERED / SKIPPED.
        with pytest.raises(ValueError, match="terminal"):
            store.mark_stage_terminal("x", "b", StageStatus.S1_DONE, "ignored")
        with pytest.raises(ValueError, match="terminal"):
            store.mark_stage_terminal("x", "b", StageStatus.S1_PENDING, "ignored")
        store.close()

    def test_record_staged_file_metadata_updates_existing_row(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        # 058: el INSERT-OR-IGNORE de S1 deja source_file_path /
        # page_count / file_size_bytes en NULL (item.staged_file es None
        # en S1). Después de que S4 anda, record_staged_file_metadata los
        # UPDATEa. Sin este fix la pestaña DETAIL muestra "—" para el size.
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN_SIZE")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.record_staged_file_metadata(
            "TXN_SIZE",
            batch_id,
            source_file_path="/tmp/staged/TXN_SIZE.pdf",
            page_count=3,
            file_size_bytes=8192,
        )
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT source_file_path, page_count, file_size_bytes "
            "FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("TXN_SIZE", batch_id),
        ).fetchone()
        conn.close()
        store.close()
        assert row == ("/tmp/staged/TXN_SIZE.pdf", 3, 8192)

    def test_record_staged_file_metadata_is_idempotent(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN_IDEM")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        for _ in range(3):
            store.record_staged_file_metadata(
                "TXN_IDEM",
                batch_id,
                source_file_path="/tmp/x.pdf",
                page_count=1,
                file_size_bytes=1024,
            )
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT source_file_path, page_count, file_size_bytes "
            "FROM migration_log WHERE rvabrep_txn_num = ? AND batch_id = ?",
            ("TXN_IDEM", batch_id),
        ).fetchone()
        conn.close()
        store.close()
        assert row == ("/tmp/x.pdf", 1, 1024)


# ---------------------------------------------------------------------------
# Grupo 4 — Queries (aceptación §4.8, §4.9, REQ-025..026)
# ---------------------------------------------------------------------------


class TestQueries:
    def test_is_uploaded_false_when_no_s5_done(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN010")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_done("TXN010", batch_id, StageStatus.S1_DONE)
        store.flush()
        assert store.is_uploaded("TXN010") is False
        store.close()

    def test_is_uploaded_true_when_s5_done(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN011", status=StageStatus.S5_PENDING)
        store.mark_stage_pending(record, StageStatus.S5_PENDING)
        store.mark_stage_done("TXN011", batch_id, StageStatus.S5_DONE)
        store.flush()
        assert store.is_uploaded("TXN011") is True
        store.close()

    def test_is_uploaded_finds_prior_batch(self, store: SQLiteTrackingStore) -> None:
        # Ancla de idempotencia cross-batch — REQ-026.
        old_batch = store.start_batch(total_records=1)
        record = _make_record(old_batch, "TXN012", status=StageStatus.S5_PENDING)
        store.mark_stage_pending(record, StageStatus.S5_PENDING)
        store.mark_stage_done("TXN012", old_batch, StageStatus.S5_DONE)
        store.complete_batch(old_batch)
        store.flush()
        # `Batch` nuevo, mismo `txn` — se tiene que detectar como ya subido.
        new_batch = store.start_batch(total_records=1)
        assert store.is_uploaded("TXN012") is True
        assert new_batch != old_batch
        store.close()

    def test_is_stage_done_false_in_different_batch(self, store: SQLiteTrackingStore) -> None:
        b1 = store.start_batch(total_records=1)
        record = _make_record(b1, "TXN013")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.mark_stage_done("TXN013", b1, StageStatus.S1_DONE)
        store.flush()
        b2 = store.start_batch(total_records=1)
        assert store.is_stage_done("TXN013", b2, StageStatus.S1_DONE) is False
        store.close()

    def test_is_stage_done_rejects_non_done_stage(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=1)
        with pytest.raises(ValueError):
            store.is_stage_done("TXN014", batch_id, StageStatus.S1_PENDING)
        with pytest.raises(ValueError):
            store.is_stage_done("TXN014", batch_id, StageStatus.S1_FAILED)
        store.close()


# ---------------------------------------------------------------------------
# Grupo 5 — Ciclo de vida (aceptación §4.10..§4.12, REQ-027..030)
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_flush_drains_queued_writes(self, store: SQLiteTrackingStore, tmp_path: Path) -> None:
        # Sin flush(), una conexión externa puede no ver las escrituras encoladas.
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN020")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.flush()  # drena
        conn = sqlite3.connect(tmp_path / "tracking.db")
        count = conn.execute(
            "SELECT COUNT(*) FROM migration_log WHERE rvabrep_txn_num = ?",
            ("TXN020",),
        ).fetchone()[0]
        conn.close()
        store.close()
        assert count == 1

    def test_close_is_idempotent(self, store: SQLiteTrackingStore) -> None:
        # Llamar a close() dos veces no debe levantar.
        store.close()
        store.close()

    def test_close_drains_pending_writes(self, tmp_path: Path) -> None:
        # Sin flush() explícito entre mark_stage_pending y close() —
        # close() tiene que drenar así la fila queda visible después.
        store = SQLiteTrackingStore(tmp_path / "tracking.db")
        batch_id = store.start_batch(total_records=1)
        record = _make_record(batch_id, "TXN021")
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.close()
        # Conexión paralela DESPUÉS del close — la fila tiene que estar.
        conn = sqlite3.connect(tmp_path / "tracking.db")
        count = conn.execute(
            "SELECT COUNT(*) FROM migration_log WHERE rvabrep_txn_num = ?",
            ("TXN021",),
        ).fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# Grupo 6 — Envoltura de errores (REQ-031)
# ---------------------------------------------------------------------------


class TestErrorWrapping:
    def test_init_on_unwritable_path_raises_tracking_error(self, tmp_path: Path) -> None:
        # Un path adentro de un directorio inexistente dispara sqlite3.OperationalError
        # que el adapter debe envolver en TrackingError.
        bogus = tmp_path / "does" / "not" / "exist" / "tracking.db"
        with pytest.raises(TrackingError):
            SQLiteTrackingStore(bogus)

    def test_is_uploaded_wraps_sqlite_error(self, store: SQLiteTrackingStore) -> None:
        # 107: cerramos la conexión de lectura del thread actual así la
        # query posterior levanta (la conexión cerrada queda cacheada en
        # el pool thread-local hasta el reset).
        store._read_pool.acquire().close()  # type: ignore[reportPrivateUsage]
        with pytest.raises(TrackingError):
            store.is_uploaded("TXN-DEAD")
        # Resetea el flag _closed así el ruido del teardown queda silenciado.
        store._closed = True  # type: ignore[reportPrivateUsage]

    def test_is_stage_done_wraps_sqlite_error(self, store: SQLiteTrackingStore) -> None:
        store._read_pool.acquire().close()  # type: ignore[reportPrivateUsage]
        with pytest.raises(TrackingError):
            store.is_stage_done("TXN-DEAD", "batch", StageStatus.S1_DONE)
        store._closed = True  # type: ignore[reportPrivateUsage]

    def test_start_batch_wraps_sqlite_error(self, store: SQLiteTrackingStore) -> None:
        store._sync_conn.close()  # type: ignore[reportPrivateUsage]
        with pytest.raises(TrackingError):
            store.start_batch(total_records=1)
        store._closed = True  # type: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Grupo 7 — Flush del writer cuando se llega al cap de 500 filas
# ---------------------------------------------------------------------------


class TestBatchFlushCap:
    def test_writer_handles_more_than_500_queued_writes(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        # Encola 600 llamadas a mark_stage_pending así el writer pega contra el
        # cap del `batch` de 500 filas al menos una vez y rueda a un segundo `batch`.
        batch_id = store.start_batch(total_records=600)
        for i in range(600):
            record = _make_record(batch_id, f"BULK{i:04d}")
            store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        count = conn.execute(
            "SELECT COUNT(*) FROM migration_log WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0]
        conn.close()
        store.close()
        assert count == 600


# ---------------------------------------------------------------------------
# Grupo 8 — list_txn_nums_for_batch (enmienda al port en 011)
# ---------------------------------------------------------------------------


class TestListTxnNumsForBatch:
    def test_returns_distinct_txns_for_batch(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=3)
        for txn in ("TXN_A", "TXN_B", "TXN_C"):
            store.mark_stage_pending(_make_record(batch_id, txn), StageStatus.S1_PENDING)
        store.flush()
        result = store.list_txn_nums_for_batch(batch_id)
        store.close()
        assert result == {"TXN_A", "TXN_B", "TXN_C"}

    def test_unknown_batch_returns_empty_set(self, store: SQLiteTrackingStore) -> None:
        result = store.list_txn_nums_for_batch("does-not-exist")
        store.close()
        assert result == set()


# ---------------------------------------------------------------------------
# Grupo 9 — Métodos orientados al operador (021)
# ---------------------------------------------------------------------------


class TestListBatches:
    def test_empty_store_returns_empty_list(self, store: SQLiteTrackingStore) -> None:
        result = store.list_batches()
        store.close()
        assert result == []

    def test_lists_batches_descending(self, store: SQLiteTrackingStore) -> None:
        batch_a = store.start_batch(total_records=10)
        batch_b = store.start_batch(total_records=20)
        store.complete_batch(batch_a)
        store.flush()
        result = store.list_batches()
        store.close()
        ids_in_order = [b.batch_id for b in result]
        # Los dos `batches` están; el más nuevo (b) primero.
        assert set(ids_in_order) == {batch_a, batch_b}
        assert ids_in_order[0] == batch_b

    def test_filter_in_progress(self, store: SQLiteTrackingStore) -> None:
        batch_a = store.start_batch(total_records=10)
        batch_b = store.start_batch(total_records=20)
        store.complete_batch(batch_a)
        store.flush()
        result = store.list_batches(status="in_progress")
        store.close()
        ids = [b.batch_id for b in result]
        assert ids == [batch_b]

    def test_filter_completed(self, store: SQLiteTrackingStore) -> None:
        batch_a = store.start_batch(total_records=10)
        store.start_batch(total_records=20)  # queda en in_progress
        store.complete_batch(batch_a)
        store.flush()
        result = store.list_batches(status="completed")
        store.close()
        ids = [b.batch_id for b in result]
        assert ids == [batch_a]


class TestGetBatchDetails:
    def test_unknown_batch_returns_none(self, store: SQLiteTrackingStore) -> None:
        result = store.get_batch_details("ghost-123")
        store.close()
        assert result is None

    def test_returns_per_stage_counts(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=3)
        store.mark_stage_pending(_make_record(batch_id, "TXN_A"), StageStatus.S1_PENDING)
        store.mark_stage_done("TXN_A", batch_id, StageStatus.S1_DONE)
        store.mark_stage_pending(_make_record(batch_id, "TXN_B"), StageStatus.S2_PENDING)
        store.mark_stage_failed("TXN_B", batch_id, StageStatus.S2_FAILED, "mapping not found")
        store.flush()
        result = store.get_batch_details(batch_id)
        store.close()
        assert result is not None
        assert result.info.batch_id == batch_id
        assert result.stage_counts["S1"]["DONE"] == 1
        assert result.stage_counts["S2"]["FAILED"] == 1
        # Forma predecible: todos los stages S0..S5 presentes.
        for stage in ("S0", "S1", "S2", "S3", "S4", "S5"):
            assert set(result.stage_counts[stage].keys()) == {"DONE", "FAILED", "PENDING"}
        # El registro fallado aparece con su mensaje de error.
        assert len(result.failed_records) == 1
        assert result.failed_records[0].txn_num == "TXN_B"
        assert result.failed_records[0].status == "S2_FAILED"
        assert result.failed_records[0].error_message == "mapping not found"


class TestListDocsForBatch052:
    """052: detalle por-doc para el `drill-down` por-chunk del TUI."""

    def test_unknown_batch_returns_empty_list(self, store: SQLiteTrackingStore) -> None:
        result = store.list_docs_for_batch("ghost-456")
        store.close()
        assert result == []

    def test_returns_one_docdetail_per_row_with_status_and_reason(
        self, store: SQLiteTrackingStore
    ) -> None:
        batch_id = store.start_batch(total_records=2)
        store.mark_stage_pending(
            _make_record(batch_id, "TXN_OK", rvabrep_file_name="OK.001"),
            StageStatus.S5_PENDING,
        )
        store.mark_stage_done("TXN_OK", batch_id, StageStatus.S5_DONE)
        store.mark_stage_pending(
            _make_record(batch_id, "TXN_BAD", rvabrep_file_name="BAD.001"),
            StageStatus.S5_PENDING,
        )
        store.mark_stage_failed("TXN_BAD", batch_id, StageStatus.S5_FAILED, "cmis 500")
        store.flush()
        docs = store.list_docs_for_batch(batch_id)
        store.close()

        assert [d.txn_num for d in docs] == ["TXN_BAD", "TXN_OK"]  # ordenados por txn_num
        by_txn = {d.txn_num: d for d in docs}
        assert by_txn["TXN_OK"].status == "S5_DONE"
        assert by_txn["TXN_OK"].error_message == ""
        assert by_txn["TXN_OK"].file_name == "OK.001"
        assert by_txn["TXN_BAD"].status == "S5_FAILED"
        assert by_txn["TXN_BAD"].error_message == "cmis 500"


class TestRetryFailed:
    def test_no_failures_returns_zero(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(_make_record(batch_id, "TXN_A"), StageStatus.S1_PENDING)
        store.flush()
        result = store.retry_failed(batch_id)
        store.close()
        assert result == 0

    def test_resets_all_failed_to_pending(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=2)
        store.mark_stage_pending(_make_record(batch_id, "TXN_A"), StageStatus.S2_PENDING)
        store.mark_stage_failed("TXN_A", batch_id, StageStatus.S2_FAILED, "boom")
        store.mark_stage_pending(_make_record(batch_id, "TXN_B"), StageStatus.S5_PENDING)
        store.mark_stage_failed("TXN_B", batch_id, StageStatus.S5_FAILED, "cmis 500")
        store.flush()
        reset = store.retry_failed(batch_id)
        details = store.get_batch_details(batch_id)
        store.close()
        assert reset == 2
        assert details is not None
        assert details.stage_counts["S2"]["PENDING"] == 1
        assert details.stage_counts["S5"]["PENDING"] == 1
        assert details.failed_records == ()

    def test_resets_only_specified_stage(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=2)
        store.mark_stage_pending(_make_record(batch_id, "TXN_A"), StageStatus.S2_PENDING)
        store.mark_stage_failed("TXN_A", batch_id, StageStatus.S2_FAILED, "boom")
        store.mark_stage_pending(_make_record(batch_id, "TXN_B"), StageStatus.S5_PENDING)
        store.mark_stage_failed("TXN_B", batch_id, StageStatus.S5_FAILED, "cmis 500")
        store.flush()
        reset = store.retry_failed(batch_id, stage=StageStatus.S5_FAILED)
        details = store.get_batch_details(batch_id)
        store.close()
        assert reset == 1
        assert details is not None
        # S5 reseteado, S2 sigue fallado.
        assert details.stage_counts["S5"]["PENDING"] == 1
        assert details.stage_counts["S2"]["FAILED"] == 1

    def test_rejects_non_failed_stage(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=1)
        with pytest.raises(TrackingError):
            store.retry_failed(batch_id, stage=StageStatus.S5_DONE)
        store.close()


# ---------------------------------------------------------------------------
# 099 — uploaded_records (entrada de la recuperación AS400)
# ---------------------------------------------------------------------------


class TestUploadedRecords:
    def test_returns_only_s5_done_with_fields(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=2)
        # Un doc subido (S5_DONE) y uno que solo llegó a S1.
        done = _make_record(batch_id, "TXN_UP1", status=StageStatus.S5_PENDING)
        store.mark_stage_pending(done, StageStatus.S5_PENDING)
        store.mark_stage_done("TXN_UP1", batch_id, StageStatus.S5_DONE, cm_object_id="cm-1")
        pending = _make_record(batch_id, "TXN_UP2")
        store.mark_stage_pending(pending, StageStatus.S1_PENDING)
        store.mark_stage_done("TXN_UP2", batch_id, StageStatus.S1_DONE)
        store.flush()

        records = store.uploaded_records()

        assert [r.txn_num for r in records] == ["TXN_UP1"]  # solo el S5_DONE
        r = records[0]
        assert r.cm_object_id == "cm-1"
        assert r.system_id == "1"
        assert r.file_name == "TESTFILE.001"
        store.close()

    def test_filters_by_batch_id(self, store: SQLiteTrackingStore) -> None:
        b1 = store.start_batch(total_records=1)
        b2 = store.start_batch(total_records=1)
        for batch, txn in ((b1, "TXN_B1"), (b2, "TXN_B2")):
            rec = _make_record(batch, txn, status=StageStatus.S5_PENDING)
            store.mark_stage_pending(rec, StageStatus.S5_PENDING)
            store.mark_stage_done(txn, batch, StageStatus.S5_DONE, cm_object_id=f"cm-{txn}")
        store.flush()

        assert [r.txn_num for r in store.uploaded_records(batch_id=b1)] == ["TXN_B1"]
        assert {r.txn_num for r in store.uploaded_records()} == {"TXN_B1", "TXN_B2"}
        store.close()


# ---------------------------------------------------------------------------
# 148 — Censo del origen: reason_code + id_rvi, denominador real, pivot completo
# ---------------------------------------------------------------------------


_LEGACY_MIGRATION_LOG = """
CREATE TABLE migration_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_shortname   TEXT    NOT NULL,
    trigger_cif         TEXT    NOT NULL,
    trigger_system_id   TEXT    NOT NULL,
    rvabrep_txn_num     TEXT    NOT NULL,
    rvabrep_file_name   TEXT    NOT NULL,
    batch_id            TEXT    NOT NULL,
    status              TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    cm_object_id        TEXT,
    cm_folder           TEXT,
    cm_object_type      TEXT,
    error_message       TEXT,
    source_file_path    TEXT,
    page_count          INTEGER,
    file_size_bytes     INTEGER,
    started_at          TEXT,
    completed_at        TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0
)
"""

_LEGACY_ROW = """
INSERT INTO migration_log (
    trigger_shortname, trigger_cif, trigger_system_id, rvabrep_txn_num,
    rvabrep_file_name, batch_id, status, created_at
) VALUES ('OLDUSER', '000000', '1', 'TXN_LEGACY', 'OLD.001',
          'batch-legacy', 'S5_DONE', '2026-01-01T00:00:00')
"""


def _log_columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(migration_log)")}
    conn.close()
    return cols


class TestCensusSchema148:
    """REQ-004: dos columnas nuevas, con migración aditiva in-place."""

    def test_fresh_db_has_both_columns(self, store: SQLiteTrackingStore, tmp_path: Path) -> None:
        store.close()
        cols = _log_columns(tmp_path / "tracking.db")
        assert "reason_code" in cols
        assert "id_rvi" in cols

    def test_legacy_db_upgrades_in_place_and_stays_readable(self, tmp_path: Path) -> None:
        """Una base creada SIN las columnas se actualiza al abrirla, y la fila
        vieja sigue ahí y se sigue leyendo."""
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute(_LEGACY_MIGRATION_LOG)
        conn.execute(_LEGACY_ROW)
        conn.commit()
        conn.close()

        store = SQLiteTrackingStore(db)
        docs = store.list_docs_for_batch("batch-legacy")
        store.close()

        assert {"reason_code", "id_rvi"} <= _log_columns(db)
        assert [d.txn_num for d in docs] == ["TXN_LEGACY"]
        assert docs[0].status == "S5_DONE"
        assert docs[0].reason_code == ""  # columna recién agregada ⇒ NULL ⇒ ""
        assert docs[0].id_rvi == ""

    def test_upgrade_is_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "twice.db"
        SQLiteTrackingStore(db).close()
        SQLiteTrackingStore(db).close()  # si re-agregara la columna, ALTER TABLE explota
        assert {"reason_code", "id_rvi"} <= _log_columns(db)


class TestCensusColumnsRoundTrip148:
    """REQ-004: todo camino de escritura puede dejar su razón y su código RVI."""

    def _row(self, tmp_path: Path, txn: str) -> tuple[str | None, str | None]:
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT reason_code, id_rvi FROM migration_log WHERE rvabrep_txn_num = ?",
            (txn,),
        ).fetchone()
        conn.close()
        return (row[0], row[1])

    def test_mark_stage_pending_persists_both(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        batch_id = store.start_batch(total_records=1)
        record = _make_record(
            batch_id,
            "TXN_CENSUS_1",
            reason_code=ReasonCode.EXCLUDED_BY_FILTER,
            id_rvi="0042",
        )
        store.mark_stage_pending(record, StageStatus.S1_PENDING)
        store.flush()
        store.close()
        assert self._row(tmp_path, "TXN_CENSUS_1") == ("EXCLUDED_BY_FILTER", "0042")

    def test_normal_upload_leaves_reason_code_null(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        """El que subió bien no tiene nada que explicar — pero sí tiene id_rvi."""
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(
            _make_record(batch_id, "TXN_CENSUS_OK", id_rvi="0007"),
            StageStatus.S5_PENDING,
        )
        store.mark_stage_done("TXN_CENSUS_OK", batch_id, StageStatus.S5_DONE, cm_object_id="cm-x")
        store.flush()
        store.close()
        assert self._row(tmp_path, "TXN_CENSUS_OK") == (None, "0007")

    def test_mark_stage_failed_records_reason_code(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(_make_record(batch_id, "TXN_CENSUS_F"), StageStatus.S2_PENDING)
        store.mark_stage_failed(
            "TXN_CENSUS_F",
            batch_id,
            StageStatus.S2_FAILED,
            "IDRVI 0099 sin fila en MapeoRVI_CM.csv",
            reason_code=ReasonCode.CODE_NOT_MAPPED,
        )
        store.flush()
        store.close()
        assert self._row(tmp_path, "TXN_CENSUS_F")[0] == "CODE_NOT_MAPPED"

    def test_mark_stage_failed_without_reason_leaves_column_untouched(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        """Los callers pre-148 no pisan una razón ya escrita."""
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(
            _make_record(batch_id, "TXN_CENSUS_K", reason_code=ReasonCode.CRASHED),
            StageStatus.S2_PENDING,
        )
        store.mark_stage_failed("TXN_CENSUS_K", batch_id, StageStatus.S2_FAILED, "boom")
        store.flush()
        store.close()
        assert self._row(tmp_path, "TXN_CENSUS_K")[0] == "CRASHED"

    def test_mark_stage_terminal_records_reason_code(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(_make_record(batch_id, "TXN_CENSUS_T"), StageStatus.S1_PENDING)
        store.mark_stage_terminal(
            "TXN_CENSUS_T",
            batch_id,
            StageStatus.S1_FILTERED,
            "deleted_at_source",
            reason_code=ReasonCode.DELETED_AT_SOURCE,
        )
        store.flush()
        conn = sqlite3.connect(tmp_path / "tracking.db")
        row = conn.execute(
            "SELECT reason_code, retry_count FROM migration_log WHERE rvabrep_txn_num = ?",
            ("TXN_CENSUS_T",),
        ).fetchone()
        conn.close()
        store.close()
        assert row[0] == "DELETED_AT_SOURCE"
        assert row[1] == 0  # sigue sin ser una falla

    def test_record_external_upload_accepts_id_rvi(
        self, store: SQLiteTrackingStore, tmp_path: Path
    ) -> None:
        store.record_external_upload(
            txn_num="TXN_EXT_148",
            file_name="EXT.001",
            shortname="EXTUSER",
            cif="000000",
            system_id="1",
            cm_object_id="cm-ext",
            id_rvi="0055",
        )
        store.flush()
        store.close()
        assert self._row(tmp_path, "TXN_EXT_148") == (None, "0055")


class TestCensusReadProjections148:
    """REQ-004: las proyecciones de lectura cargan los campos nuevos."""

    def test_list_docs_for_batch_carries_reason_and_id_rvi(
        self, store: SQLiteTrackingStore
    ) -> None:
        batch_id = store.start_batch(total_records=2)
        store.mark_stage_pending(
            _make_record(batch_id, "TXN_P1", id_rvi="0001"), StageStatus.S5_PENDING
        )
        store.mark_stage_done("TXN_P1", batch_id, StageStatus.S5_DONE)
        store.mark_stage_pending(
            _make_record(batch_id, "TXN_P2", id_rvi="0002"), StageStatus.S1_PENDING
        )
        store.mark_stage_terminal(
            "TXN_P2",
            batch_id,
            StageStatus.S1_FILTERED,
            "excluido por filtro",
            reason_code=ReasonCode.EXCLUDED_BY_FILTER,
        )
        store.flush()
        docs = {d.txn_num: d for d in store.list_docs_for_batch(batch_id)}
        store.close()
        assert docs["TXN_P1"].reason_code == ""
        assert docs["TXN_P1"].id_rvi == "0001"
        assert docs["TXN_P2"].reason_code == "EXCLUDED_BY_FILTER"
        assert docs["TXN_P2"].id_rvi == "0002"

    def test_failed_records_carry_reason_code(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(_make_record(batch_id, "TXN_FR"), StageStatus.S5_PENDING)
        store.mark_stage_failed(
            "TXN_FR",
            batch_id,
            StageStatus.S5_FAILED,
            "cmis 500",
            reason_code=ReasonCode.CM_ERROR_5XX,
        )
        store.flush()
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        assert details.failed_records[0].reason_code == "CM_ERROR_5XX"

    def test_reason_counts_group_by_bucket_reason_and_id_rvi(
        self, store: SQLiteTrackingStore
    ) -> None:
        """El desglose balde → reason_code → id_rvi que pide REQ-006."""
        batch_id = store.start_batch(total_records=4)
        plan = (
            ("TXN_C1", "0010", ReasonCode.EXCLUDED_BY_FILTER),
            ("TXN_C2", "0010", ReasonCode.EXCLUDED_BY_FILTER),
            ("TXN_C3", "0020", ReasonCode.EXCLUDED_BY_FILTER),
            ("TXN_C4", "0030", ReasonCode.CODE_NOT_MAPPED),
        )
        for txn, id_rvi, reason in plan:
            store.mark_stage_pending(
                _make_record(batch_id, txn, id_rvi=id_rvi, reason_code=reason),
                StageStatus.S1_PENDING,
            )
        # Un doc que subió bien NO entra en el censo de razones.
        store.mark_stage_pending(
            _make_record(batch_id, "TXN_C5", id_rvi="0010"), StageStatus.S5_PENDING
        )
        store.mark_stage_done("TXN_C5", batch_id, StageStatus.S5_DONE)
        store.flush()
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        got = {(r.bucket, r.reason_code, r.id_rvi): r.count for r in details.reason_counts}
        assert got == {
            ("EXCLUIDO", "EXCLUDED_BY_FILTER", "0010"): 2,
            ("EXCLUIDO", "EXCLUDED_BY_FILTER", "0020"): 1,
            ("BLOQUEADO", "CODE_NOT_MAPPED", "0030"): 1,
        }

    def test_reason_counts_empty_when_nothing_to_explain(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(_make_record(batch_id, "TXN_CLEAN"), StageStatus.S5_PENDING)
        store.mark_stage_done("TXN_CLEAN", batch_id, StageStatus.S5_DONE)
        store.flush()
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        assert details.reason_counts == ()

    def test_retry_failed_clears_the_reason_together_with_the_error(
        self, store: SQLiteTrackingStore
    ) -> None:
        """La razón muere con la falla que la produjo.

        ``retry_failed`` ya limpiaba ``error_message``; si la razón
        sobreviviera, un doc reintentado con éxito quedaría contado en el
        censo Y en los subidos, y los números dejarían de sumar.
        """
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(_make_record(batch_id, "TXN_RETRY"), StageStatus.S5_PENDING)
        store.mark_stage_failed(
            "TXN_RETRY",
            batch_id,
            StageStatus.S5_FAILED,
            "cmis 500",
            reason_code=ReasonCode.CM_ERROR_5XX,
        )
        store.flush()
        assert store.retry_failed(batch_id) == 1
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        assert details.reason_counts == ()


class TestRetryFailedRespetaElBalde150:
    """150: reintentar una DECISIÓN DE NEGOCIO no tiene sentido.

    El discriminador correcto es el **balde**, no el ``status``. Una fila
    ``CLIENT_NOT_ACTIVE`` es ``S2_FAILED`` por el eje ortogonal de 148, así
    que el ``LIKE '%_FAILED'`` de ``retry_failed`` se la llevaba puesta: con
    la lista real del operador eso son CIENTOS DE MILES de documentos
    re-procesados —cada uno pagando la cadena completa de identidad— para
    volver a excluirlos exactamente igual. La lista no va a cambiar de
    opinión.
    """

    @staticmethod
    def _seed_three_buckets(store: SQLiteTrackingStore) -> str:
        """Un doc por balde: EXCLUIDO, BLOQUEADO y FALLO."""
        batch_id = store.start_batch(total_records=3)
        for txn, stage, reason in (
            ("TXN_INACTIVE", StageStatus.S2_FAILED, ReasonCode.CLIENT_NOT_ACTIVE),
            ("TXN_UNMAPPED", StageStatus.S2_FAILED, ReasonCode.CODE_NOT_MAPPED),
            ("TXN_TIMEOUT", StageStatus.S5_FAILED, ReasonCode.CM_TIMEOUT),
        ):
            store.mark_stage_pending(_make_record(batch_id, txn), StageStatus.S2_PENDING)
            store.mark_stage_failed(txn, batch_id, stage, "synthetic", reason_code=reason)
        store.flush()
        return batch_id

    @staticmethod
    def _reasons(store: SQLiteTrackingStore, batch_id: str) -> dict[str, str]:
        return {d.txn_num: d.reason_code for d in store.list_docs_for_batch(batch_id)}

    def test_no_reintenta_el_balde_excluido(self, store: SQLiteTrackingStore) -> None:
        batch_id = self._seed_three_buckets(store)
        reset = store.retry_failed(batch_id)
        reasons = self._reasons(store, batch_id)
        store.close()
        assert reset == 2
        # El inactivo conserva su razón: no se tocó.
        assert reasons["TXN_INACTIVE"] == ReasonCode.CLIENT_NOT_ACTIVE.value
        # Los otros dos se reintentan, y su razón muere con la falla (148).
        assert reasons["TXN_UNMAPPED"] == ""
        assert reasons["TXN_TIMEOUT"] == ""

    def test_el_inactivo_sigue_en_su_estado_terminal(self, store: SQLiteTrackingStore) -> None:
        batch_id = self._seed_three_buckets(store)
        store.retry_failed(batch_id)
        statuses = {d.txn_num: d.status for d in store.list_docs_for_batch(batch_id)}
        store.close()
        assert statuses["TXN_INACTIVE"] == StageStatus.S2_FAILED.value
        assert statuses["TXN_UNMAPPED"] == StageStatus.S2_PENDING.value
        assert statuses["TXN_TIMEOUT"] == StageStatus.S5_PENDING.value

    def test_tambien_con_el_filtro_por_etapa(self, store: SQLiteTrackingStore) -> None:
        """El balde manda también cuando el operador acota a una etapa."""
        batch_id = self._seed_three_buckets(store)
        reset = store.retry_failed(batch_id, stage=StageStatus.S2_FAILED)
        reasons = self._reasons(store, batch_id)
        store.close()
        assert reset == 1
        assert reasons["TXN_INACTIVE"] == ReasonCode.CLIENT_NOT_ACTIVE.value
        assert reasons["TXN_UNMAPPED"] == ""

    def test_una_fila_sin_razon_se_sigue_reintentando(self, store: SQLiteTrackingStore) -> None:
        """``NOT IN`` sobre NULL da NULL: sin el guard, una fila legacy (o ya
        reintentada, que quedó con ``reason_code`` NULL) dejaría de
        reintentarse para siempre."""
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(_make_record(batch_id, "TXN_LEGACY"), StageStatus.S5_PENDING)
        store.mark_stage_failed("TXN_LEGACY", batch_id, StageStatus.S5_FAILED, "boom")
        store.flush()
        reset = store.retry_failed(batch_id)
        store.close()
        assert reset == 1

    def test_todo_codigo_excluido_es_no_reintentable(self) -> None:
        """El guard se DERIVA del mapeo del dominio, no se hardcodea: una
        razón EXCLUIDO nueva queda no-reintentable sola."""
        from cmcourier.adapters.tracking.sqlite import _NON_RETRYABLE_REASONS

        expected = {c.value for c in ReasonCode if c.bucket is ReasonBucket.EXCLUIDO}
        assert set(_NON_RETRYABLE_REASONS) == expected


class TestSourceTotal148:
    """REQ-005: el denominador real, no el batch_size configurado."""

    def test_increment_accumulates_as_s1_sees_documents(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=0)  # streaming no sabe el total
        store.increment_source_total(batch_id, 500)
        store.increment_source_total(batch_id, 500)
        store.increment_source_total(batch_id, 37)
        store.flush()
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        assert details.info.total_records == 1037

    def test_set_overrides_the_seed_written_by_start_batch(
        self, store: SQLiteTrackingStore
    ) -> None:
        """staged siembra el batch_size configurado: no es un conteo del origen."""
        batch_id = store.start_batch(total_records=1000)
        store.set_source_total(batch_id, 0)
        store.increment_source_total(batch_id, 12)
        store.flush()
        assert [b.total_records for b in store.list_batches() if b.batch_id == batch_id] == [12]
        store.close()

    def test_increment_is_visible_to_list_batches(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=0)
        store.increment_source_total(batch_id, 3)
        store.flush()
        infos = {b.batch_id: b for b in store.list_batches()}
        store.close()
        assert infos[batch_id].total_records == 3

    def test_increment_on_unknown_batch_is_a_noop(self, store: SQLiteTrackingStore) -> None:
        store.increment_source_total("ghost-789", 10)
        store.flush()
        assert store.list_batches() == []
        store.close()


class TestPivotRendersEveryOutcome148:
    """REQ-006: S1_FILTERED y S1_SKIPPED dejan de caerse de los agregados."""

    def _seed(self, store: SQLiteTrackingStore) -> str:
        batch_id = store.start_batch(total_records=4)
        store.mark_stage_pending(_make_record(batch_id, "TXN_D"), StageStatus.S5_PENDING)
        store.mark_stage_done("TXN_D", batch_id, StageStatus.S5_DONE)
        store.mark_stage_pending(_make_record(batch_id, "TXN_F1"), StageStatus.S1_PENDING)
        store.mark_stage_terminal("TXN_F1", batch_id, StageStatus.S1_FILTERED, "borrado")
        store.mark_stage_pending(_make_record(batch_id, "TXN_F2"), StageStatus.S1_PENDING)
        store.mark_stage_terminal("TXN_F2", batch_id, StageStatus.S1_FILTERED, "borrado")
        store.mark_stage_pending(_make_record(batch_id, "TXN_S1"), StageStatus.S1_PENDING)
        store.mark_stage_terminal("TXN_S1", batch_id, StageStatus.S1_SKIPPED, "ya subido")
        store.flush()
        return batch_id

    def test_filtered_and_skipped_are_reported(self, store: SQLiteTrackingStore) -> None:
        batch_id = self._seed(store)
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        assert details.stage_counts["S1"]["FILTERED"] == 2
        assert details.stage_counts["S1"]["SKIPPED"] == 1
        assert details.stage_counts["S5"]["DONE"] == 1

    def test_numbers_add_up_to_the_row_count(self, store: SQLiteTrackingStore) -> None:
        """Antes de 148, DONE+FAILED+PENDING no sumaba el total y nadie avisaba."""
        batch_id = self._seed(store)
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        total = sum(n for states in details.stage_counts.values() for n in states.values())
        assert total == 4

    def test_legacy_outcomes_come_first_and_in_order(self, store: SQLiteTrackingStore) -> None:
        """La salida actual no se reordena: DONE, FAILED, PENDING y después el resto."""
        batch_id = self._seed(store)
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        for states in details.stage_counts.values():
            assert list(states)[:3] == ["DONE", "FAILED", "PENDING"]

    def test_shape_stays_rectangular_across_stages(self, store: SQLiteTrackingStore) -> None:
        """Todas las etapas exponen las mismas claves — el renderer no adivina."""
        batch_id = self._seed(store)
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        shapes = {tuple(states) for states in details.stage_counts.values()}
        assert len(shapes) == 1
        assert set(shapes.pop()) == {"DONE", "FAILED", "PENDING", "FILTERED", "SKIPPED"}

    def test_clean_batch_keeps_the_three_legacy_outcomes(self, store: SQLiteTrackingStore) -> None:
        batch_id = store.start_batch(total_records=1)
        store.mark_stage_pending(_make_record(batch_id, "TXN_ONLY"), StageStatus.S1_PENDING)
        store.mark_stage_done("TXN_ONLY", batch_id, StageStatus.S1_DONE)
        store.flush()
        details = store.get_batch_details(batch_id)
        store.close()
        assert details is not None
        for stage in ("S0", "S1", "S2", "S3", "S4", "S5"):
            assert list(details.stage_counts[stage]) == ["DONE", "FAILED", "PENDING"]
