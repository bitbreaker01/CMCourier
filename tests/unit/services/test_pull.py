"""Tests de :class:`As400Pull` (151 REQ-004).

La dirección AS400 → local: traer lo que hicieron los otros programas de
la migración. Es orquestación pura — stores mockeados.

La regla que gobierna todo (REQ-001): *el lado local manda sobre los
documentos que CMCourier procesó; el AS400 manda sobre los documentos que
CMCourier nunca vio*. Acá eso significa que el pull **rellena huecos y
nunca pisa un estado terminal local**.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.adapters.tracking.as400_niarvilog import NiarvilogRow
from cmcourier.services.pull import As400Pull, PullResult
from cmcourier.services.sync_progress import SyncProgress

pytestmark = pytest.mark.unit


def _niarvilog_row(
    txn: str,
    stscod: str = "O",
    *,
    objidn: str | None = None,
    eerrmsg: str = "",
) -> NiarvilogRow:
    now = datetime(2026, 9, 4, 10, 0, 0)
    return NiarvilogRow(
        siscod="1",
        trnnum=txn,
        docfrm="CC03",
        imgarc=f"{txn}.001",
        imgtip="B",
        ctecif="CLIENTE01",
        ctenum=123456,
        stscod=stscod,
        idnbac="CN01",
        tipidn="TipoX",
        objidn=f"cm-{txn}" if objidn is None else objidn,
        numrei=0,
        pmrrei=now,
        finrei=now,
        eerrmsg=eerrmsg,
    )


class _FakeAs400:
    """Store AS400 que rinde filas de a una y anota cuándo lo hace."""

    def __init__(self, rows: list[NiarvilogRow], events: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.events = events
        self.statuses_asked: list[tuple[str, ...]] = []
        self.closed = False

    def stream_rows_by_status(self, statuses: Sequence[str] = ("O", "F")) -> Iterator[NiarvilogRow]:
        self.statuses_asked.append(tuple(statuses))
        for row in self.rows:
            self.events.append(("yield", row.trnnum))
            yield row

    def read_states_by_txns(self, trnnums: list[str]) -> dict[str, NiarvilogRow]:
        raise AssertionError("el pull LISTA la tabla; no parte de txns conocidos")

    def close(self) -> None:
        self.closed = True


class _FakeSqlite:
    """Tracking local: estados terminales conocidos + registro de escrituras."""

    def __init__(
        self, terminal: dict[str, str] | None = None, events: list[tuple[str, str]] | None = None
    ) -> None:
        self.terminal = terminal or {}
        self.events = events if events is not None else []
        self.uploads: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.flushes = 0
        self.lookups: list[list[str]] = []

    def terminal_states_by_txns(self, txn_nums: list[str]) -> dict[str, str]:
        self.lookups.append(list(txn_nums))
        return {t: s for t, s in self.terminal.items() if t in set(txn_nums)}

    def record_external_upload(self, **kwargs: Any) -> None:
        self.events.append(("write", str(kwargs["txn_num"])))
        self.uploads.append(kwargs)

    def record_external_failure(self, **kwargs: Any) -> None:
        self.events.append(("write", str(kwargs["txn_num"])))
        self.failures.append(kwargs)

    def flush(self) -> None:
        self.flushes += 1


def _pull(
    rows: list[NiarvilogRow],
    terminal: dict[str, str] | None = None,
    *,
    chunk_size: int = 500,
) -> tuple[As400Pull, _FakeAs400, _FakeSqlite]:
    events: list[tuple[str, str]] = []
    as400 = _FakeAs400(rows, events)
    sqlite = _FakeSqlite(terminal, events)
    pull = As400Pull(
        sqlite_store=sqlite,  # type: ignore[arg-type]
        as400_store=as400,  # type: ignore[arg-type]
        chunk_size=chunk_size,
    )
    return pull, as400, sqlite


# ---------------------------------------------------------------------------
# Importar lo que CMCourier nunca vio
# ---------------------------------------------------------------------------


class TestImporta151:
    def test_una_fila_o_sin_terminal_local_entra_como_s5_done(self) -> None:
        pull, _as400, sqlite = _pull([_niarvilog_row("0000001")])

        result = pull.pull(apply=True)

        assert result.imported_uploaded == 1
        assert sqlite.uploads == [
            {
                "txn_num": "0000001",
                "file_name": "0000001.001",
                "shortname": "CLIENTE01",
                "cif": "123456",
                "system_id": "1",
                "cm_object_id": "cm-0000001",
                "id_rvi": "CC03",
            }
        ]
        assert sqlite.failures == []

    def test_una_fila_f_sin_terminal_local_entra_como_s5_failed(self) -> None:
        pull, _as400, sqlite = _pull([_niarvilog_row("0000002", "F", eerrmsg="SQL0803")])

        result = pull.pull(apply=True)

        assert result.imported_failed == 1
        assert sqlite.failures == [
            {
                "txn_num": "0000002",
                "file_name": "0000002.001",
                "shortname": "CLIENTE01",
                "cif": "123456",
                "system_id": "1",
                "error_message": "SQL0803",
                "id_rvi": "CC03",
            }
        ]
        assert sqlite.uploads == []

    def test_dry_run_no_escribe_nada(self) -> None:
        pull, _as400, sqlite = _pull(
            [_niarvilog_row("0000001"), _niarvilog_row("0000002", "F", eerrmsg="boom")]
        )

        result = pull.pull(apply=False)

        assert result.imported_uploaded == 1
        assert result.imported_failed == 1
        assert sqlite.uploads == [] and sqlite.failures == []

    def test_solo_pide_o_y_f(self) -> None:
        """``'I'`` y ``'N'`` son estados en vuelo: importarlos sería
        fotografiar algo que ya no es cierto."""
        pull, as400, _sqlite = _pull([])

        pull.pull(apply=True)

        assert as400.statuses_asked == [("O", "F")]


# ---------------------------------------------------------------------------
# REQ-001 — nunca pisar un estado terminal local
# ---------------------------------------------------------------------------


class TestNoPisaLoLocal151:
    def test_terminal_local_que_coincide_no_hace_nada(self) -> None:
        pull, _as400, sqlite = _pull(
            [_niarvilog_row("0000001", "O"), _niarvilog_row("0000002", "F")],
            terminal={"0000001": "S5_DONE", "0000002": "S5_FAILED"},
        )

        result = pull.pull(apply=True)

        assert result.consistent == 2
        assert result.divergent == []
        assert sqlite.uploads == [] and sqlite.failures == []

    def test_as400_dice_f_y_local_dice_done_es_divergencia(self) -> None:
        """El caso del bug: nosotros lo subimos, el AS400 quedó viejo. El
        pull NO lo pisa — lo arregla ``recover`` en la otra dirección."""
        pull, _as400, sqlite = _pull(
            [_niarvilog_row("0000001", "F", eerrmsg="timeout")],
            terminal={"0000001": "S5_DONE"},
        )

        result = pull.pull(apply=True)

        assert [i.txn_num for i in result.divergent] == ["0000001"]
        assert "S5_DONE" in result.divergent[0].reason
        assert "'F'" in result.divergent[0].reason
        assert sqlite.uploads == [] and sqlite.failures == []

    def test_as400_dice_o_y_local_dice_failed_es_divergencia(self) -> None:
        pull, _as400, sqlite = _pull(
            [_niarvilog_row("0000001", "O")], terminal={"0000001": "S5_FAILED"}
        )

        result = pull.pull(apply=True)

        assert [i.txn_num for i in result.divergent] == ["0000001"]
        assert sqlite.uploads == []

    def test_ninguna_escritura_toca_una_fila_terminal_local(self) -> None:
        """La garantía en una sola línea: el pull sólo escribe TXN que el
        tracking local no tenía terminados."""
        rows = [_niarvilog_row(f"{i:07d}", "O" if i % 2 else "F") for i in range(10)]
        terminal = {f"{i:07d}": "S5_DONE" for i in range(0, 10, 3)}
        pull, _as400, sqlite = _pull(rows, terminal=terminal)

        pull.pull(apply=True)

        escritos = {u["txn_num"] for u in sqlite.uploads} | {f["txn_num"] for f in sqlite.failures}
        assert escritos.isdisjoint(terminal)


# ---------------------------------------------------------------------------
# Streaming, no `fetchall`
# ---------------------------------------------------------------------------


class TestStreaming151:
    def test_escribe_antes_de_terminar_de_leer(self) -> None:
        """148 como precedente: "todo" no puede significar "todo en RAM".
        Si el pull materializara la tabla, la primera escritura llegaría
        DESPUÉS del último yield."""
        rows = [_niarvilog_row(f"{i:07d}") for i in range(1200)]
        pull, as400, _sqlite = _pull(rows, chunk_size=500)

        pull.pull(apply=True)

        kinds = [k for k, _ in as400.events]
        assert kinds.index("write") < len(kinds) - 1 - kinds[::-1].index("yield")

    def test_consulta_el_tracking_una_vez_por_lote_no_por_fila(self) -> None:
        rows = [_niarvilog_row(f"{i:07d}") for i in range(1200)]
        pull, _as400, sqlite = _pull(rows, chunk_size=500)

        pull.pull(apply=False)

        assert [len(c) for c in sqlite.lookups] == [500, 500, 200]

    def test_apply_flushea_por_lote(self) -> None:
        """Las escrituras a SQLite van por lotes: la cola del writer no
        crece con el tamaño de la tabla."""
        rows = [_niarvilog_row(f"{i:07d}") for i in range(1200)]
        pull, _as400, sqlite = _pull(rows, chunk_size=500)

        pull.pull(apply=True)

        assert sqlite.flushes == 3

    def test_dry_run_no_flushea(self) -> None:
        pull, _as400, sqlite = _pull([_niarvilog_row("0000001")])
        pull.pull(apply=False)
        assert sqlite.flushes == 0

    def test_tabla_vacia(self) -> None:
        pull, _as400, sqlite = _pull([])

        result = pull.pull(apply=True)

        assert result == PullResult()
        assert sqlite.lookups == []


# ---------------------------------------------------------------------------
# Reporte y progreso
# ---------------------------------------------------------------------------


class TestReporte151:
    def test_los_cuatro_grupos_conviven(self) -> None:
        rows = [
            _niarvilog_row("0000001", "O"),  # importa
            _niarvilog_row("0000002", "F", eerrmsg="boom"),  # importa fallido
            _niarvilog_row("0000003", "O"),  # consistente
            _niarvilog_row("0000004", "F"),  # divergente
        ]
        pull, _as400, _sqlite = _pull(rows, terminal={"0000003": "S5_DONE", "0000004": "S5_DONE"})

        result = pull.pull(apply=True)

        assert result.scanned == 4
        assert result.imported_uploaded == 1
        assert result.imported_failed == 1
        assert result.consistent == 1
        assert [i.txn_num for i in result.divergent] == ["0000004"]

    def test_emite_progreso_por_lote(self) -> None:
        rows = [_niarvilog_row(f"{i:07d}") for i in range(250)]
        pull, _as400, _sqlite = _pull(rows, chunk_size=100)
        events: list[SyncProgress] = []

        pull.pull(apply=False, on_progress=events.append)

        assert [e.done for e in events if e.phase == "listando NIARVILOG"] == [0, 100, 200, 250]

    def test_un_callback_que_revienta_no_aborta_el_pull(self) -> None:
        pull, _as400, sqlite = _pull([_niarvilog_row("0000001")])

        def bad(_: SyncProgress) -> None:
            raise ValueError("UI muerta")

        result = pull.pull(apply=True, on_progress=bad)

        assert result.imported_uploaded == 1
        assert len(sqlite.uploads) == 1


class TestClose151:
    def test_close_cierra_el_store_as400(self) -> None:
        pull, as400, _sqlite = _pull([])
        pull.close()
        assert as400.closed is True

    def test_el_sqlite_es_del_caller(self) -> None:
        sqlite = MagicMock()
        pull = As400Pull(sqlite_store=sqlite, as400_store=MagicMock())
        pull.close()
        sqlite.close.assert_not_called()
