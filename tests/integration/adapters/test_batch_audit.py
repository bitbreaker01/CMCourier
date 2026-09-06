"""Tests de las columnas de auditoría de migration_batch (124).

C3 del informe UX v2: la auditoría (quién lanzó qué, con qué config y
overrides, con qué veredicto del doctor, y cómo terminó) necesita
columnas reales. La migración es idempotente y las DBs pre-124 las
adquieren al reabrirse.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cmcourier.adapters.tracking.sqlite import SQLiteTrackingStore

pytestmark = pytest.mark.integration

_AUDIT_COLS = {
    "operator",
    "station",
    "pipeline_kind",
    "environment",
    "config_hash",
    "overrides_json",
    "doctor_verdict",
    "outcome",
}


def _cols(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(migration_batch)")}
    finally:
        conn.close()


class TestAuditSchema:
    def test_fresh_store_has_audit_columns(self, tmp_path: Path) -> None:
        store = SQLiteTrackingStore(tmp_path / "t.db")
        store.close()
        assert _cols(tmp_path / "t.db") >= _AUDIT_COLS

    def test_pre_124_db_migrates_on_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE migration_batch (batch_id TEXT PRIMARY KEY, "
            "total_records INTEGER NOT NULL, started_at TEXT NOT NULL, completed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO migration_batch VALUES ('legacy', 5, '2026-01-01T00:00:00', NULL)"
        )
        conn.commit()
        conn.close()

        store = SQLiteTrackingStore(db)
        try:
            assert _cols(db) >= _AUDIT_COLS
            # la fila legacy sobrevive con audit en NULL
            batches = store.list_batches()
            assert any(b.batch_id == "legacy" for b in batches)
        finally:
            store.close()


class TestAuditWrites:
    def test_record_audit_and_outcome_roundtrip(self, tmp_path: Path) -> None:
        store = SQLiteTrackingStore(tmp_path / "t.db")
        try:
            batch_id = store.start_batch(total_records=10)
            store.record_batch_audit(
                batch_id,
                operator="gmaker",
                station="MIGRA-01",
                pipeline_kind="csv",
                environment="staging",
                config_hash="e41f9a",
                overrides_json='{"workers": 12}',
                doctor_verdict="aprobado",
            )
            store.set_batch_outcome(batch_id, "cancelled")
            store.flush()
            conn = sqlite3.connect(str(tmp_path / "t.db"))
            row = conn.execute(
                "SELECT operator, station, pipeline_kind, environment, config_hash, "
                "overrides_json, doctor_verdict, outcome FROM migration_batch "
                "WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            conn.close()
            assert row == (
                "gmaker",
                "MIGRA-01",
                "csv",
                "staging",
                "e41f9a",
                '{"workers": 12}',
                "aprobado",
                "cancelled",
            )
        finally:
            store.close()
