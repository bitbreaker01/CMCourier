"""Integration tests for ``cmcourier sync`` subcommands (034 phase 4).

The CLI is wired against fake AS400 (pyodbc cursor at the driver
boundary) + a real SQLite store. Test focus: argument parsing +
exit codes + the resolver's effect on SQLite.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from click.testing import CliRunner

from cmcourier.adapters.tracking import SQLiteTrackingStore
from cmcourier.adapters.tracking import as400_niarvilog as niarvilog_module
from cmcourier.cli.app import main
from cmcourier.domain.models import MigrationRecord, StageStatus

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Reuse the pyodbc fake from test_as400_niarvilog.py
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, list[Any]]] = []
        self.fetch_queue: list[tuple[list[tuple[Any, ...]], tuple[str, ...]]] = []
        self.rowcount_queue: list[int] = []
        self._current_rows: list[list[Any]] = []
        self._current_columns: list[str] = []
        self.rowcount = -1

    @property
    def description(self) -> list[tuple[str, ...]]:
        return [(c,) for c in self._current_columns]

    def execute(self, sql: str, params: list[Any] | None = None) -> _FakeCursor:
        self.executions.append((sql, list(params or [])))
        if self.fetch_queue:
            rows, columns = self.fetch_queue.pop(0)
            self._current_rows = [list(r) for r in rows]
            self._current_columns = list(columns)
        else:
            self._current_rows = []
            self._current_columns = []
        self.rowcount = self.rowcount_queue.pop(0) if self.rowcount_queue else -1
        return self

    def fetchall(self) -> list[list[Any]]:
        out = self._current_rows
        self._current_rows = []
        return out

    def fetchmany(self, size: int) -> list[list[Any]]:
        """151: la lectura en streaming de ``sync pull`` / ``sync status``."""
        chunk, self._current_rows = self._current_rows[:size], self._current_rows[size:]
        return chunk

    def fetchone(self) -> list[Any] | None:
        return self._current_rows.pop(0) if self._current_rows else None

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakePyodbcModule:
    class Error(Exception):
        pass

    class IntegrityError(Error):
        pass

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self, cs: str) -> _FakeConn:  # noqa: ARG002
        return self._conn


def _patch_pyodbc(monkeypatch: pytest.MonkeyPatch, cursor: _FakeCursor) -> _FakeConn:
    conn = _FakeConn(cursor)
    monkeypatch.setattr(niarvilog_module, "pyodbc", _FakePyodbcModule(conn))
    return conn


_COLUMNS = (
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
)


def _niarvilog_tuple(
    *,
    trnnum: str = "0000001",
    stscod: str = "O",
    objidn: str = "cmis-abc",
) -> tuple[Any, ...]:
    now = datetime(2025, 11, 17, 10, 0, 0)
    return (
        "1",  # SISCOD
        trnnum,
        "CC03",
        "DAAAH9X4.001",
        "B",
        "TESTCLIENT01",
        123456,
        stscod,
        "CN01",
        "MyType",
        objidn,
        0,
        now,
        now,
        "",
    )


# ---------------------------------------------------------------------------
# YAML helper (minimal, AS400 sync enabled)
# ---------------------------------------------------------------------------


_TESTS_ROOT = Path(__file__).parent.parent.parent
_PIPELINE_FIXTURES = _TESTS_ROOT / "fixtures" / "pipeline"
_SERVICES_FIXTURES = _TESTS_ROOT / "fixtures" / "services"
_ASSEMBLY_FIXTURES = _TESTS_ROOT / "fixtures" / "assembly"


def _write_yaml(tmp_path: Path) -> Path:
    triggers = tmp_path / "triggers.csv"
    triggers.write_text("ShortName,CIF,SystemID\n")
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        dedent(
            f"""\
            trigger:
              csv_path: {triggers}
            indexing:
              source:
                kind: csv
                csv_path: {_PIPELINE_FIXTURES / "rvabrep.csv"}
            mapping:
              csv_path: {_SERVICES_FIXTURES / "modelo_documental.csv"}
            metadata:
              field_sources:
                BAC_CIF:
                  sources:
                    - source_type: trigger
                      lookup_value_column: cif
            assembly:
              source_root: {_ASSEMBLY_FIXTURES}
              temp_dir: {tmp_path / "stg"}
            cmis:
              base_url: "http://cmis.test:9080/cmis"
              repo_id: "$x!testrepo"
            tracking:
              db_path: {tmp_path / "tracking.db"}
              as400_sync:
                enabled: true
                connection:
                  host: 10.0.0.1
                library: RVILIB
                table: NIARVILOG
                stale_in_progress_minutes: 30
                retry_attempts: 3
                retry_base_delay_s: 0.001
            """
        )
    )
    return yaml_path


def _set_as400_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AS400_USERNAME", "tester")
    monkeypatch.setenv("AS400_PASSWORD", "secret-not-real")
    monkeypatch.setenv("CMIS_USERNAME", "tester")
    monkeypatch.setenv("CMIS_PASSWORD", "secret-not-real")


# ---------------------------------------------------------------------------
# Help discovery
# ---------------------------------------------------------------------------


class TestSyncHelp:
    def test_root_help_lists_sync(self) -> None:
        result = CliRunner().invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "sync" in result.stdout

    def test_sync_help_lists_subcommands(self) -> None:
        result = CliRunner().invoke(main, ["sync", "--help"])
        assert result.exit_code == 0
        assert "resolve" in result.stdout
        assert "status" in result.stdout
        assert "recover" in result.stdout
        assert "pull" in result.stdout  # 151


# ---------------------------------------------------------------------------
# sync status
# ---------------------------------------------------------------------------


class TestSyncStatus:
    def test_reports_no_conflicts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_as400_env(monkeypatch)
        cur = _FakeCursor()
        # cleanup_stale returns 0 rows
        cur.rowcount_queue = [0]
        _patch_pyodbc(monkeypatch, cur)
        yaml_path = _write_yaml(tmp_path)

        result = CliRunner().invoke(main, ["sync", "status", "--config", str(yaml_path)])
        assert result.exit_code == 0, result.stderr
        assert "stale_cleaned=0" in result.stdout

    def test_reports_a_divergence_without_writing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """151 REQ-005: ``sync status`` decía reportar conflictos y sólo
        limpiaba los ``'I'`` vencidos. Ahora los reporta de verdad."""
        _set_as400_env(monkeypatch)
        yaml_path = _write_yaml(tmp_path)
        _seed_uploaded(tmp_path, txn="0000001", cm_object_id="cmis-abc")
        cur = _FakeCursor()
        cur.rowcount_queue = [0]
        # 1) el cleanup de stale (UPDATE, sin filas); 2) el barrido, que
        # devuelve la fila en 'F' mientras el tracking local dice S5_DONE.
        cur.fetch_queue = [
            ([], _COLUMNS),
            ([_niarvilog_tuple(stscod="F")], _COLUMNS),
        ]
        _patch_pyodbc(monkeypatch, cur)

        result = CliRunner().invoke(main, ["sync", "status", "--config", str(yaml_path)])

        assert result.exit_code == 0, result.stderr
        assert "divergentes=1" in result.stdout
        assert "0000001" in result.output
        # read-only: ni un INSERT ni un UPDATE al tracking (el único UPDATE
        # que se ve es el cleanup de stale, que ya estaba).
        selects = [e for e in cur.executions if e[0].lstrip().upper().startswith("SELECT")]
        updates = [e for e in cur.executions if e[0].lstrip().upper().startswith("UPDATE")]
        assert len(selects) == 1
        assert len(updates) == 1 and "STSCOD = 'N'" in updates[0][0]


# ---------------------------------------------------------------------------
# 151 — sync pull y el ciclo completo de las dos direcciones
# ---------------------------------------------------------------------------


def _seed_uploaded(tmp_path: Path, *, txn: str, cm_object_id: str) -> None:
    """Deja UN documento ``S5_DONE`` en el tracking local real."""
    store = SQLiteTrackingStore(tmp_path / "tracking.db")
    batch_id = store.start_batch(total_records=1)
    record = MigrationRecord(
        trigger_shortname="TESTCLIENT01",
        trigger_cif="123456",
        trigger_system_id="1",
        rvabrep_txn_num=txn,
        rvabrep_file_name="DAAAH9X4.001",
        batch_id=batch_id,
        status=StageStatus.S5_PENDING,
        created_at=datetime(2026, 1, 1, 0, 0),
    )
    store.mark_stage_pending(record, StageStatus.S5_PENDING)
    store.mark_stage_done(txn, batch_id, StageStatus.S5_DONE, cm_object_id=cm_object_id)
    store.flush()
    store.close()


def _local_rows(tmp_path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(tmp_path / "tracking.db")
    rows = conn.execute(
        "SELECT rvabrep_txn_num, status, batch_id, COALESCE(reason_code, ''), "
        "COALESCE(cm_object_id, '') FROM migration_log ORDER BY rvabrep_txn_num"
    ).fetchall()
    conn.close()
    return rows


class TestSyncPull151:
    def test_imports_what_another_program_did(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_as400_env(monkeypatch)
        yaml_path = _write_yaml(tmp_path)
        cur = _FakeCursor()
        cur.fetch_queue = [
            (
                [
                    _niarvilog_tuple(trnnum="0000009", stscod="O", objidn="cmis-otro"),
                    _niarvilog_tuple(trnnum="0000010", stscod="F", objidn=""),
                ],
                _COLUMNS,
            )
        ]
        _patch_pyodbc(monkeypatch, cur)

        result = CliRunner().invoke(main, ["sync", "pull", "--config", str(yaml_path), "--apply"])

        assert result.exit_code == 0, result.stderr
        assert "importadas_ok=1" in result.stdout
        assert "importadas_fallidas=1" in result.stdout
        rows = _local_rows(tmp_path)
        assert rows == [
            ("0000009", "S5_DONE", "__as400_import__", "", "cmis-otro"),
            ("0000010", "S5_FAILED", "__as400_import__", "EXTERNAL_FAILURE", ""),
        ]

    def test_dry_run_writes_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_as400_env(monkeypatch)
        yaml_path = _write_yaml(tmp_path)
        cur = _FakeCursor()
        cur.fetch_queue = [([_niarvilog_tuple(trnnum="0000009", stscod="O")], _COLUMNS)]
        _patch_pyodbc(monkeypatch, cur)

        result = CliRunner().invoke(main, ["sync", "pull", "--config", str(yaml_path)])

        assert result.exit_code == 0, result.stderr
        assert "[DRY-RUN]" in result.stdout
        assert _local_rows(tmp_path) == []

    def test_never_overwrites_a_local_terminal_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-001 de punta a punta: el AS400 dice ``'F'``, el tracking
        local dice ``S5_DONE``. El pull NO lo pisa."""
        _set_as400_env(monkeypatch)
        yaml_path = _write_yaml(tmp_path)
        _seed_uploaded(tmp_path, txn="0000001", cm_object_id="cmis-abc")
        antes = _local_rows(tmp_path)
        cur = _FakeCursor()
        cur.fetch_queue = [([_niarvilog_tuple(stscod="F")], _COLUMNS)]
        _patch_pyodbc(monkeypatch, cur)

        result = CliRunner().invoke(main, ["sync", "pull", "--config", str(yaml_path), "--apply"])

        assert result.exit_code == 0, result.stderr
        assert "divergentes=1" in result.stdout
        assert "importadas_ok=0" in result.stdout
        assert _local_rows(tmp_path) == antes  # intacto


class TestDosDireccionesE2E151:
    def test_done_local_y_f_en_as400_queda_consistente_tras_recover_apply(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """El caso que motivó la spec, de punta a punta.

        REQ-001: el lado local manda sobre los documentos que CMCourier
        procesó. El doc está ``S5_DONE`` acá y quedó ``'F'`` allá, así que
        ``recover`` lo ACTUALIZA (pre-151 lo contaba como
        ``already_present`` y lo dejaba divergente para siempre). Después
        de eso, las dos direcciones lo ven igual.
        """
        _set_as400_env(monkeypatch)
        yaml_path = _write_yaml(tmp_path)
        _seed_uploaded(tmp_path, txn="0000001", cm_object_id="cmis-abc")

        # --- 1) recover --apply: la fila está, pero en 'F' → UPDATE a 'O'
        cur = _FakeCursor()
        cur.fetch_queue = [([_niarvilog_tuple(stscod="F")], _COLUMNS)]
        cur.rowcount_queue = [0, 1]  # SELECT sin rowcount, UPDATE = 1 fila
        _patch_pyodbc(monkeypatch, cur)

        result = CliRunner().invoke(
            main, ["sync", "recover", "--config", str(yaml_path), "--apply"]
        )

        assert result.exit_code == 0, result.stderr
        assert "actualizadas=1" in result.stdout
        assert "recuperadas=0" in result.stdout  # no era un INSERT: la fila estaba
        update = next(e for e in cur.executions if e[0].lstrip().upper().startswith("UPDATE"))
        assert "STSCOD = 'O'" in update[0]
        assert "STSCOD <> 'O'" in update[0]  # la guarda: no pisa un terminal ajeno
        assert update[1] == ["cmis-abc", "0000001"]

        # --- 2) con el AS400 ya en 'O', las dos direcciones lo ven igual
        cur2 = _FakeCursor()
        cur2.fetch_queue = [
            ([_niarvilog_tuple(stscod="O", objidn="cmis-abc")], _COLUMNS),  # recover
            ([_niarvilog_tuple(stscod="O", objidn="cmis-abc")], _COLUMNS),  # pull
        ]
        _patch_pyodbc(monkeypatch, cur2)

        recover_again = CliRunner().invoke(main, ["sync", "recover", "--config", str(yaml_path)])
        pull_after = CliRunner().invoke(main, ["sync", "pull", "--config", str(yaml_path)])

        assert recover_again.exit_code == 0, recover_again.stderr
        assert "consistentes=1" in recover_again.stdout
        assert "divergentes=0" in recover_again.stdout
        assert pull_after.exit_code == 0, pull_after.stderr
        assert "consistentes=1" in pull_after.stdout
        assert "divergentes=0" in pull_after.stdout


# ---------------------------------------------------------------------------
# sync resolve --prefer-as400
# ---------------------------------------------------------------------------


class TestSyncResolvePreferAs400:
    def test_pulls_as400_state_into_sqlite(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_as400_env(monkeypatch)
        cur = _FakeCursor()
        # First call: read_state_by_txn returns a 'O' row.
        cur.fetch_queue = [
            ([_niarvilog_tuple(stscod="O", objidn="cmis-abc")], _COLUMNS),
        ]
        _patch_pyodbc(monkeypatch, cur)
        yaml_path = _write_yaml(tmp_path)

        result = CliRunner().invoke(
            main,
            [
                "sync",
                "resolve",
                "0000001",
                "--prefer-as400",
                "--config",
                str(yaml_path),
            ],
        )
        assert result.exit_code == 0, result.stderr
        assert "0000001" in result.stdout
        assert "resolved" in result.stdout.lower() or "imported" in result.stdout.lower()

    def test_errors_when_txn_not_in_as400(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_as400_env(monkeypatch)
        cur = _FakeCursor()
        cur.fetch_queue = [([], _COLUMNS)]  # row absent
        _patch_pyodbc(monkeypatch, cur)
        yaml_path = _write_yaml(tmp_path)

        result = CliRunner().invoke(
            main,
            [
                "sync",
                "resolve",
                "0000001",
                "--prefer-as400",
                "--config",
                str(yaml_path),
            ],
        )
        # Exit 1 — operator asked to import but there's nothing to import.
        assert result.exit_code == 1
        assert "not found in AS400" in result.stderr or "not found" in result.stdout


# ---------------------------------------------------------------------------
# sync resolve --prefer-local
# ---------------------------------------------------------------------------


class TestSyncResolvePreferLocal:
    def test_pushes_supplied_objidn_to_as400(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_as400_env(monkeypatch)
        cur = _FakeCursor()
        # First exec (read_state_by_txn → SELECT) returns the existing row;
        # second exec (mark_uploaded_by_txn → UPDATE) returns rowcount=1.
        cur.fetch_queue = [
            ([_niarvilog_tuple(stscod="N", objidn="")], _COLUMNS),
        ]
        cur.rowcount_queue = [0, 1]  # SELECT no rowcount, UPDATE=1
        _patch_pyodbc(monkeypatch, cur)
        yaml_path = _write_yaml(tmp_path)

        result = CliRunner().invoke(
            main,
            [
                "sync",
                "resolve",
                "0000001",
                "--prefer-local",
                "--cm-object-id",
                "cmis-local-id",
                "--config",
                str(yaml_path),
            ],
        )
        assert result.exit_code == 0, result.stderr
        # The UPDATE was executed with the supplied cm_object_id.
        update_call = next((e for e in cur.executions if "UPDATE" in e[0].upper()), None)
        assert update_call is not None
        assert "cmis-local-id" in update_call[1]

    def test_prefer_local_without_cm_object_id_exits_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_as400_env(monkeypatch)
        _patch_pyodbc(monkeypatch, _FakeCursor())
        yaml_path = _write_yaml(tmp_path)
        result = CliRunner().invoke(
            main,
            [
                "sync",
                "resolve",
                "0000001",
                "--prefer-local",
                "--config",
                str(yaml_path),
            ],
        )
        assert result.exit_code == 2
        assert "--cm-object-id" in result.stderr
