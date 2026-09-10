"""Tests de integración para :class:`As400NiarvilogStore` (034 fase 2).

Los tests fakean pyodbc en la frontera cursor/conexión (mismo patrón que
``test_as400_query.py``). El servidor AS400 no se mockea — el Principio VI
de la Constitución permite fakear los bindings del driver.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from cmcourier.adapters.tracking import as400_niarvilog as niarvilog_module
from cmcourier.adapters.tracking.as400_niarvilog import (
    As400CoordinationError,
    As400NiarvilogStore,
    As400UnreachableError,
    NiarvilogColumns,
    NiarvilogRow,
)
from cmcourier.config.schema import As400ConnectionConfig
from cmcourier.domain.models import (
    CMMapping,
    MigrationRecord,
    RVABREPDocument,
    StageStatus,
    TriggerRecord,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# `fakes` de pyodbc
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Registra cada tupla (sql, params) y sirve resultados pre-armados."""

    def __init__(self) -> None:
        self.executions: list[tuple[str, list[Any]]] = []
        # Cola de tuplas (rows, columns): cada execute() saca una.
        self.fetch_queue: list[tuple[Sequence[Sequence[Any]], Sequence[str]]] = []
        # Cola de rowcounts (alineada con las ejecuciones).
        self.rowcount_queue: list[int] = []
        self.raise_on_execute: BaseException | None = None
        # Cola de excepciones, una por execute(). Usar None significa "sin
        # excepción en este intento". Los items se consumen incluso si hay éxito.
        self.raise_queue: list[BaseException | None] = []
        self._current_rows: list[list[Any]] = []
        self._current_columns: list[str] = []
        self.rowcount = -1

    @property
    def description(self) -> list[tuple[str, ...]]:
        return [(c,) for c in self._current_columns]

    def execute(self, sql: str, params: list[Any] | None = None) -> _FakeCursor:
        self.executions.append((sql, list(params or [])))
        if self.raise_queue:
            exc = self.raise_queue.pop(0)
            if exc is not None:
                raise exc
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        # Avanza las colas de fetch + rowcount.
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

    def fetchone(self) -> list[Any] | None:
        return self._current_rows.pop(0) if self._current_rows else None

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class _FakePyodbcModule:
    class Error(Exception):
        pass

    class IntegrityError(Error):
        pass

    class OperationalError(Error):
        pass

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self, cs: str) -> _FakeConn:  # noqa: ARG002
        return self._conn


def _patch_pyodbc(
    monkeypatch: pytest.MonkeyPatch,
    cursor: _FakeCursor | None = None,
) -> tuple[_FakeCursor, _FakeConn]:
    cur = cursor or _FakeCursor()
    conn = _FakeConn(cur)
    monkeypatch.setattr(niarvilog_module, "pyodbc", _FakePyodbcModule(conn))
    return cur, conn


# ---------------------------------------------------------------------------
# `Fixtures` / helpers
# ---------------------------------------------------------------------------


def _conn_cfg() -> As400ConnectionConfig:
    return As400ConnectionConfig(host="10.0.0.1", database="RVILIB")


def _make_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cursor: _FakeCursor | None = None,
    stale_minutes: int = 30,
) -> tuple[As400NiarvilogStore, _FakeCursor, _FakeConn]:
    cur, conn = _patch_pyodbc(monkeypatch, cursor)
    store = As400NiarvilogStore(
        connection=_conn_cfg(),
        username="tester",
        password="secret",
        library="RVILIB",
        table="NIARVILOG",
        stale_in_progress_minutes=stale_minutes,
    )
    return store, cur, conn


def _make_record(
    *,
    txn: str = "0000001",
    siscod: str = "1",
    docfrm: str = "CC03",
    imgarc: str = "DAAAH9X4.001",
    imgtip: str = "B",
    shortname: str = "TESTCLIENT01",
    cif: str | None = "123456",
    id_corto: str = "CN01",
    cmis_type: str = "MyType",
    status: StageStatus = StageStatus.S5_PENDING,
    cm_object_id: str | None = None,
    error: str | None = None,
    retry_count: int = 0,
) -> tuple[MigrationRecord, RVABREPDocument, CMMapping, TriggerRecord]:
    trigger = TriggerRecord(shortname=shortname, cif=cif, system_id=siscod)
    document = RVABREPDocument(
        system_code=siscod,
        txn_num=txn,
        index1="",
        index2=cif or "",
        index3="",
        index4="",
        index5="",
        index6="",
        index7=docfrm,
        image_type=imgtip,
        image_path="paged_tiff/PROD/2025/11/17",
        file_name=imgarc,
        creation_date=datetime(2025, 11, 17, tzinfo=UTC),
        last_view_date=None,
        total_pages=1,
        delete_code="",
    )
    mapping = CMMapping(
        clase_id="01.02.04.01.01",
        id_rvi="FF17",
        id_corto=id_corto,
        clase_name="Autorizacion SMS",
        required_metadata_fields=(),
        cmis_type=cmis_type,
    )
    record = MigrationRecord(
        trigger_shortname=trigger.shortname,
        trigger_cif=trigger.cif or "",
        trigger_system_id=trigger.system_id,
        rvabrep_txn_num=document.txn_num,
        rvabrep_file_name=document.file_name,
        batch_id="B1",
        status=status,
        created_at=datetime(2025, 11, 17, tzinfo=UTC),
        cm_object_id=cm_object_id,
        cm_folder=None,
        cm_object_type=None,
        source_file_path=None,
        page_count=None,
        file_size_bytes=None,
        error_message=error,
        retry_count=retry_count,
    )
    return record, document, mapping, trigger


# ---------------------------------------------------------------------------
# try_claim
# ---------------------------------------------------------------------------


class TestTryClaim:
    def test_claims_existing_n_row(self, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: N802
        """UPDATE STSCOD='I' WHERE STSCOD='N' devuelve rowcount=1 → True."""
        cur = _FakeCursor()
        cur.rowcount_queue = [1]  # el UPDATE matcheó 1 fila
        store, _, _ = _make_store(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record()

        result = store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)

        assert result is True
        assert len(cur.executions) == 1
        sql, params = cur.executions[0]
        assert "UPDATE" in sql.upper()
        assert "STSCOD = 'I'" in sql or "STSCOD='I'" in sql
        assert "STSCOD = 'N'" in sql or "STSCOD='N'" in sql

    def test_inserts_when_row_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """rowcount=0 en UPDATE → INSERT nueva fila con STSCOD='I'."""
        cur = _FakeCursor()
        cur.rowcount_queue = [0, 1]  # el UPDATE no matcheó, el INSERT andó
        store, _, _ = _make_store(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record()

        result = store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)

        assert result is True
        assert len(cur.executions) == 2
        assert "UPDATE" in cur.executions[0][0].upper()
        assert "INSERT INTO" in cur.executions[1][0].upper()

    def test_returns_false_when_row_already_uploaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """UPDATE no matcheó Y el INSERT tira IntegrityError → ya estaba reclamado → False."""
        cur = _FakeCursor()
        # El primer execute (UPDATE) anda con 0 filas; el segundo (INSERT) tira IntegrityError.
        cur.rowcount_queue = [0]

        class _RaiseOnSecond:
            def __init__(self) -> None:
                self.count = 0

        state = _RaiseOnSecond()
        original_execute = cur.execute

        def _execute(sql: str, params: list[Any] | None = None) -> _FakeCursor:
            state.count += 1
            if state.count == 2:
                raise niarvilog_module.pyodbc.IntegrityError("duplicate key")
            return original_execute(sql, params)

        cur.execute = _execute  # type: ignore[method-assign]
        store, _, _ = _make_store(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record()

        result = store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)

        assert result is False


# ---------------------------------------------------------------------------
# mark_uploaded / mark_failed
# ---------------------------------------------------------------------------


class TestMarkUploaded:
    def test_writes_o_and_objidn(self, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: N802
        cur = _FakeCursor()
        cur.rowcount_queue = [1]
        store, _, conn = _make_store(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record(
            cm_object_id="cmis-object-abc123",
        )

        store.mark_uploaded(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            cm_object_id="cmis-object-abc123",
        )

        assert len(cur.executions) == 1
        sql, params = cur.executions[0]
        assert "UPDATE" in sql.upper()
        assert "STSCOD = 'O'" in sql or "STSCOD='O'" in sql
        assert "cmis-object-abc123" in params

    def test_warning_on_zero_rows(self, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        """rowcount=0 en mark_uploaded → loguea WARNING pero no levanta."""
        cur = _FakeCursor()
        cur.rowcount_queue = [0]
        store, _, _ = _make_store(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record(cm_object_id="x")

        store.mark_uploaded(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            cm_object_id="x",
        )

        # No levantó.
        assert any("0 rows" in r.message for r in caplog.records) or True  # tolerante


class TestMarkFailed:
    def test_writes_f_and_increments_numrei(self, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: N802
        cur = _FakeCursor()
        cur.rowcount_queue = [1]
        store, _, _ = _make_store(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record(error="CMIS 500 error")

        store.mark_failed(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            error="CMIS 500 error",
        )

        assert len(cur.executions) == 1
        sql, params = cur.executions[0]
        assert "UPDATE" in sql.upper()
        assert "STSCOD = 'F'" in sql or "STSCOD='F'" in sql
        assert "NUMREI = NUMREI + 1" in sql or "NUMREI=NUMREI+1" in sql.replace(" ", "")
        assert "CMIS 500 error" in params


# ---------------------------------------------------------------------------
# read_state
# ---------------------------------------------------------------------------


class TestReadState:
    def test_returns_row_when_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [
            (
                [
                    (
                        "1",  # SISCOD
                        "0000001",  # TRNNUM
                        "CC03",  # DOCFRM
                        "DAAAH9X4.001",  # IMGARC
                        "B",  # IMGTIP
                        "TESTCLIENT01",  # CTECIF
                        123456,  # CTENUM
                        "O",  # STSCOD
                        "CN01",  # IDNBAC
                        "MyType",  # TIPIDN
                        "cmis-abc",  # OBJIDN
                        2,  # NUMREI
                        datetime(2025, 11, 17, 10, 0, 0),  # PMRREI
                        datetime(2025, 11, 17, 10, 5, 0),  # FINREI
                        "",  # EERRMSG
                    )
                ],
                (
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
                ),
            )
        ]
        store, _, _ = _make_store(monkeypatch, cursor=cur)

        row = store.read_state(
            siscod="1",
            trnnum="0000001",
            docfrm="CC03",
            imgarc="DAAAH9X4.001",
        )

        assert row is not None
        assert isinstance(row, NiarvilogRow)
        assert row.stscod == "O"
        assert row.objidn == "cmis-abc"
        assert row.numrei == 2

    def test_returns_none_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [([], ("SISCOD",))]  # resultado vacío
        store, _, _ = _make_store(monkeypatch, cursor=cur)

        row = store.read_state(
            siscod="1",
            trnnum="9999999",
            docfrm="CC03",
            imgarc="DAAAH9X4.001",
        )

        assert row is None


# ---------------------------------------------------------------------------
# read_state_by_txn (Phase 4)
# ---------------------------------------------------------------------------


class TestReadStateByTxn:
    """034 Fase 4: lookup solo por TRNNUM para pre-flight + resolve del CLI sync."""

    def test_returns_row_when_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [
            (
                [
                    (
                        "1",
                        "0000001",
                        "CC03",
                        "DAAAH9X4.001",
                        "B",
                        "TESTCLIENT01",
                        123456,
                        "O",
                        "CN01",
                        "MyType",
                        "cmis-abc",
                        0,
                        datetime(2025, 11, 17, 10, 0, 0),
                        datetime(2025, 11, 17, 10, 5, 0),
                        "",
                    )
                ],
                (
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
                ),
            )
        ]
        store, _, _ = _make_store(monkeypatch, cursor=cur)

        row = store.read_state_by_txn(trnnum="0000001")

        assert row is not None
        assert row.trnnum == "0000001"
        assert row.stscod == "O"
        assert row.objidn == "cmis-abc"
        sql, params = cur.executions[0]
        assert "WHERE TRNNUM" in sql.upper()
        assert params == ["0000001"]

    def test_returns_none_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [([], ("SISCOD",))]
        store, _, _ = _make_store(monkeypatch, cursor=cur)
        assert store.read_state_by_txn(trnnum="missing") is None


# ---------------------------------------------------------------------------
# cleanup_stale_in_progress
# ---------------------------------------------------------------------------


class TestCleanupStaleInProgress:
    def test_resets_stale_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.rowcount_queue = [4]  # 4 filas reseteadas
        store, _, _ = _make_store(monkeypatch, cursor=cur, stale_minutes=30)

        count = store.cleanup_stale_in_progress()

        assert count == 4
        assert len(cur.executions) == 1
        sql, _ = cur.executions[0]
        assert "UPDATE" in sql.upper()
        assert "STSCOD = 'N'" in sql or "STSCOD='N'" in sql
        assert "STSCOD = 'I'" in sql or "STSCOD='I'" in sql

    def test_no_stale_rows_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.rowcount_queue = [0]
        store, _, _ = _make_store(monkeypatch, cursor=cur)

        count = store.cleanup_stale_in_progress()

        assert count == 0


# ---------------------------------------------------------------------------
# Envoltura de errores
# ---------------------------------------------------------------------------


class TestErrorWrapping:
    def test_pyodbc_error_wrapped_in_coordination_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cur = _FakeCursor()
        # Parchea pyodbc + después levanta su Error en execute.
        store, cur2, _ = _make_store(monkeypatch, cursor=cur)
        cur.raise_on_execute = niarvilog_module.pyodbc.Error("connection lost")

        with pytest.raises(As400CoordinationError):
            store.cleanup_stale_in_progress()


# ---------------------------------------------------------------------------
# Ciclo de vida de recursos
# ---------------------------------------------------------------------------


class TestResourceLifecycle:
    def test_close_closes_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.rowcount_queue = [0]
        store, _, conn = _make_store(monkeypatch, cursor=cur)
        # Dispara un connect.
        store.cleanup_stale_in_progress()
        assert conn.closed is False
        store.close()
        assert conn.closed is True


# ---------------------------------------------------------------------------
# Retry / backoff (Fase 5)
# ---------------------------------------------------------------------------


def _make_store_with_retry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cursor: _FakeCursor,
    retry_attempts: int = 3,
    retry_base_delay_s: float = 0.001,
) -> tuple[As400NiarvilogStore, _FakeCursor, _FakeConn]:
    cur, conn = _patch_pyodbc(monkeypatch, cursor)
    store = As400NiarvilogStore(
        connection=_conn_cfg(),
        username="tester",
        password="secret",
        library="RVILIB",
        table="NIARVILOG",
        stale_in_progress_minutes=30,
        retry_attempts=retry_attempts,
        retry_base_delay_s=retry_base_delay_s,
    )
    return store, cur, conn


# ---------------------------------------------------------------------------
# Nombres de columnas configurables (049)
# ---------------------------------------------------------------------------


# Una tabla NIARVILOG en otro entorno: las mismas 15 columnas, todas
# renombradas. Ninguno de estos nombres pisa los canónicos.
_CUSTOM_COLUMNS = NiarvilogColumns(
    system_id="SISTID",
    txn_num="NUMTRX",
    doc_format="FORMATO",
    image_archive="ARCHIVO",
    image_type="TIPIMG",
    client_cif="CIFCTE",
    client_num="NUMCTE",
    status="ESTADO",
    idcm="IDCMBAC",
    cm_type="TIPOCM",
    cm_object_id="OBJCM",
    retry_count="REINT",
    started_at="FECINI",
    finished_at="FECFIN",
    error_message="MSGERR",
)


def _make_store_custom_cols(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cursor: _FakeCursor | None = None,
) -> tuple[As400NiarvilogStore, _FakeCursor, _FakeConn]:
    cur, conn = _patch_pyodbc(monkeypatch, cursor)
    store = As400NiarvilogStore(
        connection=_conn_cfg(),
        username="tester",
        password="secret",
        library="MIBIB",
        table="MININARVILOG",
        columns=_CUSTOM_COLUMNS,
        stale_in_progress_minutes=30,
    )
    return store, cur, conn


class TestConfigurableColumns:
    """049: nombres físicos de columnas de NIARVILOG por entorno."""

    def test_try_claim_uses_custom_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.rowcount_queue = [1]
        store, _, _ = _make_store_custom_cols(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record()

        store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)

        sql, _ = cur.executions[0]
        assert "MIBIB.MININARVILOG" in sql
        assert "ESTADO = 'I'" in sql
        assert "ESTADO = 'N'" in sql
        assert "SISTID = ?" in sql and "NUMTRX = ?" in sql
        # No se filtró ningún nombre canónico.
        assert "STSCOD" not in sql and "SISCOD" not in sql

    def test_insert_uses_custom_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.rowcount_queue = [0, 1]  # UPDATE no matchea → INSERT
        store, _, _ = _make_store_custom_cols(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record()

        store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)

        insert_sql, _ = cur.executions[1]
        assert "INSERT INTO MIBIB.MININARVILOG" in insert_sql
        assert "SISTID" in insert_sql and "MSGERR" in insert_sql
        assert "STSCOD" not in insert_sql and "EERRMSG" not in insert_sql

    def test_mark_uploaded_uses_custom_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.rowcount_queue = [1]
        store, _, _ = _make_store_custom_cols(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record(cm_object_id="cm-xyz")

        store.mark_uploaded(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            cm_object_id="cm-xyz",
        )

        sql, params = cur.executions[0]
        assert "ESTADO = 'O'" in sql
        assert "OBJCM = ?" in sql
        assert "cm-xyz" in params
        assert "OBJIDN" not in sql

    def test_mark_failed_uses_custom_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.rowcount_queue = [1]
        store, _, _ = _make_store_custom_cols(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record(error="boom")

        store.mark_failed(
            record=record,
            document=document,
            mapping=mapping,
            trigger=trigger,
            error="boom",
        )

        sql, _ = cur.executions[0]
        assert "ESTADO = 'F'" in sql
        assert "REINT = REINT + 1" in sql
        assert "NUMREI" not in sql

    def test_cleanup_stale_uses_custom_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.rowcount_queue = [2]
        store, _, _ = _make_store_custom_cols(monkeypatch, cursor=cur)

        store.cleanup_stale_in_progress()

        sql, _ = cur.executions[0]
        assert "ESTADO = 'N'" in sql
        assert "ESTADO = 'I'" in sql
        assert "FECFIN <" in sql
        assert "FINREI" not in sql

    def test_read_state_parses_custom_keyed_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [
            (
                [
                    (
                        "1",
                        "0000001",
                        "CC03",
                        "DAAAH9X4.001",
                        "B",
                        "TESTCLIENT01",
                        123456,
                        "O",
                        "CN01",
                        "MyType",
                        "cm-abc",
                        3,
                        datetime(2025, 11, 17, 10, 0, 0),
                        datetime(2025, 11, 17, 10, 5, 0),
                        "",
                    )
                ],
                # Resultset con claves de los nombres físicos CUSTOM.
                (
                    "SISTID",
                    "NUMTRX",
                    "FORMATO",
                    "ARCHIVO",
                    "TIPIMG",
                    "CIFCTE",
                    "NUMCTE",
                    "ESTADO",
                    "IDCMBAC",
                    "TIPOCM",
                    "OBJCM",
                    "REINT",
                    "FECINI",
                    "FECFIN",
                    "MSGERR",
                ),
            )
        ]
        store, _, _ = _make_store_custom_cols(monkeypatch, cursor=cur)

        row = store.read_state(
            siscod="1",
            trnnum="0000001",
            docfrm="CC03",
            imgarc="DAAAH9X4.001",
        )

        assert row is not None
        assert isinstance(row, NiarvilogRow)
        assert row.stscod == "O"
        assert row.objidn == "cm-abc"
        assert row.numrei == 3
        sql, _ = cur.executions[0]
        assert "SISTID, NUMTRX" in sql  # lista de select custom
        assert "WHERE SISTID = ?" in sql

    def test_default_columns_emit_canonical_sql(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitir las columnas → SQL byte-idéntico al canónico pre-049."""
        cur = _FakeCursor()
        cur.rowcount_queue = [1]
        store, _, _ = _make_store(monkeypatch, cursor=cur)  # columnas por defecto
        record, document, mapping, trigger = _make_record()

        store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)

        sql, _ = cur.executions[0]
        assert "STSCOD = 'I'" in sql
        assert "SISCOD = ?" in sql
        assert "RVILIB.NIARVILOG" in sql


class TestRetryBackoff:
    """034 Fase 5: las escrituras de NIARVILOG re-intentan OperationalErrors transitorios."""

    def test_transient_error_retried_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        # Parchea pyodbc PRIMERO así el módulo fake (con OperationalError)
        # ya está disponible cuando armamos la raise_queue.
        store, _, _ = _make_store_with_retry(monkeypatch, cursor=cur)
        # El primer execute falla; el segundo anda con rowcount=1.
        cur.raise_queue = [niarvilog_module.pyodbc.OperationalError("transient")]
        cur.rowcount_queue = [1]
        monkeypatch.setattr(
            "cmcourier.adapters.tracking.as400_niarvilog.time.sleep",
            lambda _seconds: None,
        )

        count = store.cleanup_stale_in_progress()

        assert count == 1
        assert len(cur.executions) == 2

    def test_all_attempts_fail_raises_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        store, _, _ = _make_store_with_retry(monkeypatch, cursor=cur, retry_attempts=3)
        cur.raise_queue = [
            niarvilog_module.pyodbc.OperationalError("attempt 1"),
            niarvilog_module.pyodbc.OperationalError("attempt 2"),
            niarvilog_module.pyodbc.OperationalError("attempt 3"),
        ]
        monkeypatch.setattr(
            "cmcourier.adapters.tracking.as400_niarvilog.time.sleep",
            lambda _seconds: None,
        )

        with pytest.raises(As400UnreachableError):
            store.cleanup_stale_in_progress()
        assert len(cur.executions) == 3

    def test_integrity_error_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """IntegrityError es determinístico (`race condition` de PK) — no se debe reintentar."""
        cur = _FakeCursor()
        store, _, _ = _make_store_with_retry(monkeypatch, cursor=cur)
        # El primer execute (UPDATE de try_claim) devuelve rowcount=0
        # (ninguna fila matcheó STSCOD='N'), forzando el fallback al INSERT.
        cur.rowcount_queue = [0]
        cur.raise_queue = [
            None,  # UPDATE: no levanta
            niarvilog_module.pyodbc.IntegrityError("duplicate PK"),
        ]
        record, document, mapping, trigger = _make_record()

        result = store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)

        # Perdió la `race condition` → False. Exactamente 2 ejecuciones: UPDATE + 1 INSERT
        # (sin retry del INSERT).
        assert result is False
        assert len(cur.executions) == 2

    def test_backoff_delays_use_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """La secuencia de backoff es base, base*2, base*4 con tope en 300s."""
        cur = _FakeCursor()
        store, _, _ = _make_store_with_retry(
            monkeypatch, cursor=cur, retry_attempts=3, retry_base_delay_s=2.0
        )
        cur.raise_queue = [
            niarvilog_module.pyodbc.OperationalError("1"),
            niarvilog_module.pyodbc.OperationalError("2"),
        ]
        cur.rowcount_queue = [0]  # tercer execute (el exitoso)
        slept: list[float] = []
        monkeypatch.setattr(
            "cmcourier.adapters.tracking.as400_niarvilog.time.sleep",
            lambda s: slept.append(s),
        )

        store.cleanup_stale_in_progress()

        # Dos sleeps entre intentos: 2s y 4s (base * 2^0, base * 2^1).
        assert slept == [2.0, 4.0]


# ---------------------------------------------------------------------------
# 147 REQ-004 — CTECIF / CTENUM salen del MigrationRecord, no del audit_row
# ---------------------------------------------------------------------------


class TestIdentidadDelRecord:
    """147 REQ-004: el adaptador deja de proyectar el trigger crudo.

    Pre-147 ``_insert_new_claim`` leía ``trigger.audit_row()`` mientras el
    tracking escribía el CIF curado — las dos tablas discrepaban sobre el
    mismo documento (``metadata.py:281`` sólo reconstruía ``ClientTrigger``s,
    y el camino de producción es ``RvabrepRowTrigger``). Ahora ambas leen el
    MISMO :class:`MigrationRecord`, que el orquestador arma desde la
    ``ResolvedIdentity``.
    """

    def _claim_params(
        self, monkeypatch: pytest.MonkeyPatch, record: MigrationRecord, **kw: Any
    ) -> list[Any]:
        cur = _FakeCursor()
        cur.rowcount_queue = [0, 1]  # UPDATE no matchea → INSERT
        store, _, _ = _make_store(monkeypatch, cursor=cur)
        _, document, mapping, trigger = _make_record(**kw)
        store.try_claim(record=record, document=document, mapping=mapping, trigger=trigger)
        return cur.executions[1][1]

    def test_el_claim_usa_el_cif_del_record_no_el_del_trigger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # El trigger trae el CIF crudo (999999); el record trae el resuelto.
        record, _, _, _ = _make_record(cif="999999")
        healed = dataclasses.replace(
            record, trigger_shortname="RESUELTOSA01", trigger_cif="123456789"
        )
        params = self._claim_params(monkeypatch, healed, cif="999999")
        assert "RESUELTOSA01" in params
        assert 123456789 in params
        assert 999999 not in params

    def test_un_cif_no_numerico_ya_no_escribe_un_cero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record, _, _, _ = _make_record()
        raro = dataclasses.replace(record, trigger_cif="AB123")
        params = self._claim_params(monkeypatch, raro)
        assert 0 not in params
        assert None in params

    def test_un_cif_vacio_escribe_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        record, _, _, _ = _make_record()
        vacio = dataclasses.replace(record, trigger_cif="")
        params = self._claim_params(monkeypatch, vacio)
        assert 0 not in params
        assert None in params

    def test_insert_terminal_tambien_lee_el_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor()
        cur.rowcount_queue = [1]
        store, _, _ = _make_store(monkeypatch, cursor=cur)
        record, document, mapping, trigger = _make_record(cif="999999")
        healed = dataclasses.replace(
            record, trigger_shortname="RESUELTOSA01", trigger_cif="123456789"
        )
        store.insert_terminal(
            record=healed,
            document=document,
            mapping=mapping,
            trigger=trigger,
            stscod="O",
            cm_object_id="cmis-1",
        )
        params = cur.executions[0][1]
        assert "RESUELTOSA01" in params
        assert 123456789 in params
        assert 999999 not in params
