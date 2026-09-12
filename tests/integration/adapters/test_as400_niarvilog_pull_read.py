"""151 — la lectura que LISTA NIARVILOG y el UPDATE guardado del recover.

Hasta 151 todas las lecturas del AS400 partían de una lista de TXN que el
lado local ya conocía (``read_states_by_txns``): se podía preguntar "¿qué
sabés de estos que yo ya tengo?" pero nunca "¿qué hay ahí?". ``sync pull``
necesita lo segundo, y **en streaming**: el precedente es 148, donde
``get_by_fields_in`` terminaba en ``fetchall()`` y por eso el censo no
podía barrer un sistema entero.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import pytest

from cmcourier.adapters.tracking import as400_niarvilog as niarvilog_module
from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore, NiarvilogRow
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


def _row_for(txn: str, stscod: str = "O", eerrmsg: str = "") -> list[Any]:
    now = datetime(2026, 9, 4, 10, 0, 0)
    return [
        "1",
        txn,
        "CC03",
        "F.001",
        "B",
        "CLI",
        123,
        stscod,
        "CN01",
        "T",
        f"cm-{txn}",
        0,
        now,
        now,
        eerrmsg,
    ]


class _StreamFakeCursor:
    """Cursor que sirve las filas de a ``fetchmany``. ``fetchall`` EXPLOTA:
    la spec exige streaming y el test tiene que poder probarlo."""

    def __init__(self, module: _StreamFakeModule) -> None:
        self._module = module
        self._pending: list[list[Any]] = []
        self.rowcount = module.write_rowcount

    @property
    def description(self) -> list[tuple[str, ...]]:
        return [(c,) for c in _COLS]

    def execute(self, sql: str, params: list[Any] | None = None) -> _StreamFakeCursor:
        self._module.executed.append((sql, list(params or [])))
        if sql.lstrip().upper().startswith("SELECT"):
            self._pending = [list(r) for r in self._module.table]
        return self

    def fetchmany(self, size: int) -> list[list[Any]]:
        self._module.fetchmany_sizes.append(size)
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    def fetchall(self) -> list[list[Any]]:
        raise AssertionError("el pull NO puede materializar la tabla entera")

    def close(self) -> None:
        self._module.closed_cursors += 1


class _StreamFakeConn:
    def __init__(self, module: _StreamFakeModule) -> None:
        self._module = module

    def cursor(self) -> _StreamFakeCursor:
        return _StreamFakeCursor(self._module)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class _StreamFakeModule:
    class Error(Exception):
        pass

    class IntegrityError(Error):
        pass

    class OperationalError(Error):
        pass

    def __init__(self, table: list[list[Any]]) -> None:
        self.table = table
        self.executed: list[tuple[str, list[Any]]] = []
        self.fetchmany_sizes: list[int] = []
        self.closed_cursors = 0
        self.write_rowcount = 1
        self._lock = threading.Lock()

    def connect(self, cs: str) -> _StreamFakeConn:  # noqa: ARG002
        return _StreamFakeConn(self)


def _make_store(
    monkeypatch: pytest.MonkeyPatch, table: list[list[Any]]
) -> tuple[As400NiarvilogStore, _StreamFakeModule]:
    module = _StreamFakeModule(table)
    monkeypatch.setattr(niarvilog_module, "pyodbc", module)
    store = As400NiarvilogStore(
        connection=As400ConnectionConfig(host="10.0.0.1", database="RVILIB"),
        username="tester",
        password="secret",
        retry_attempts=2,
        retry_base_delay_s=0.001,
    )
    return store, module


class TestStreamRowsByStatus151:
    """151 REQ-004: la primera lectura del AS400 que no parte de una lista
    de TXN conocidos."""

    def test_lista_la_tabla_filtrando_por_estado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = [_row_for("0000001"), _row_for("0000002", "F", "boom")]
        store, module = _make_store(monkeypatch, table)

        rows = list(store.stream_rows_by_status())

        assert [r.trnnum for r in rows] == ["0000001", "0000002"]
        assert isinstance(rows[0], NiarvilogRow)
        assert rows[1].stscod == "F"
        assert rows[1].eerrmsg == "boom"
        sql, params = module.executed[0]
        assert "SELECT" in sql and "STSCOD IN" in sql
        assert params == ["O", "F"]  # 'I' y 'N' están fuera de alcance a propósito
        store.close()

    def test_usa_fetchmany_y_nunca_fetchall(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El fake revienta en ``fetchall``: si este test pasa, es streaming."""
        table = [_row_for(f"{i:07d}") for i in range(1200)]
        store, module = _make_store(monkeypatch, table)

        rows = list(store.stream_rows_by_status())

        assert len(rows) == 1200
        # ceil(1200/500) = 3 lotes con filas + 1 vacío que corta el loop.
        assert module.fetchmany_sizes == [500, 500, 500, 500]
        store.close()

    def test_no_ejecuta_sql_hasta_el_primer_next(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Es un generator: sin consumo no hay round-trip al iSeries."""
        store, module = _make_store(monkeypatch, [_row_for("0000001")])

        stream = store.stream_rows_by_status()

        assert module.executed == []
        assert next(iter(stream)).trnnum == "0000001"
        assert len(module.executed) == 1
        store.close()

    def test_tabla_vacia_no_rinde_nada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, _module = _make_store(monkeypatch, [])
        assert list(store.stream_rows_by_status()) == []
        store.close()

    def test_estados_vacios_no_consultan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, module = _make_store(monkeypatch, [_row_for("0000001")])
        assert list(store.stream_rows_by_status(statuses=())) == []
        assert module.executed == []
        store.close()

    def test_cierra_el_cursor_aunque_el_consumidor_abandone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un ``break`` del caller no puede dejar el cursor abierto."""
        table = [_row_for(f"{i:07d}") for i in range(1200)]
        store, module = _make_store(monkeypatch, table)

        stream = store.stream_rows_by_status()
        next(iter(stream))
        stream.close()

        assert module.closed_cursors == 1
        store.close()


class TestMarkUploadedIfStaleByTxn151:
    """151 REQ-003: el UPDATE del recover lleva guarda de estado; el de
    ``sync resolve --prefer-local`` (manual, explícito) no."""

    def test_el_update_lleva_la_guarda_de_estado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, module = _make_store(monkeypatch, [])

        rowcount = store.mark_uploaded_if_stale_by_txn(trnnum="0000001", cm_object_id="cm-1")

        assert rowcount == 1
        sql, params = module.executed[-1]
        assert sql.lstrip().upper().startswith("UPDATE")
        assert "STSCOD = 'O'" in sql
        assert "STSCOD <> 'O'" in sql  # la guarda: no pisa un terminal ajeno
        assert "EERRMSG = ''" in sql  # el error del intento previo se limpia
        assert params == ["cm-1", "0000001"]
        store.close()

    def test_rowcount_cero_cuando_la_guarda_no_matchea(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, module = _make_store(monkeypatch, [])
        module.write_rowcount = 0

        assert store.mark_uploaded_if_stale_by_txn(trnnum="0000001", cm_object_id="cm-1") == 0
        store.close()

    def test_el_helper_manual_sigue_sin_guarda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``sync resolve --prefer-local`` es una decisión explícita del
        operador sobre UN txn: ahí la guarda sobraría."""
        store, module = _make_store(monkeypatch, [])

        store.mark_uploaded_by_txn(trnnum="0000001", cm_object_id="cm-1")

        sql, _params = module.executed[-1]
        assert "STSCOD <> 'O'" not in sql
        store.close()
