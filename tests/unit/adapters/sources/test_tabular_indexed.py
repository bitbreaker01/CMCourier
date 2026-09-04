"""Tests de los lookups indexados de TabularDataSource (112).

Pre-112 ``get_by_fields`` hacía una máscara booleana O(filas) por cada
filtro en cada llamada — una vez POR DOCUMENTO desde S1/S3/local-scan.
112 construye un índice hash lazy por combinación de columnas
(``groupby(...).indices``) y las consultas pasan a lookup O(1),
preservando la semántica exacta del scan.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cmcourier.adapters.sources.tabular import TabularDataSource

pytestmark = pytest.mark.unit

_CSV = """\
shortname,system_id,txn_num,amount
SHORT1,1,T001,100
SHORT2,1,T002,200
SHORT1,2,T003,300
SHORT1,1,T004,400
NOVALUE,,T005,500
"""


@pytest.fixture
def source(tmp_path: Path) -> TabularDataSource:
    p = tmp_path / "data.csv"
    p.write_text(_CSV)
    src = TabularDataSource(p)
    yield src
    src.close()


class TestEquivalence:
    def test_single_field_filter(self, source: TabularDataSource) -> None:
        rows = source.get_by_fields({"shortname": "SHORT1"})
        assert [r["txn_num"] for r in rows] == ["T001", "T003", "T004"]

    def test_multi_field_filter(self, source: TabularDataSource) -> None:
        rows = source.get_by_fields({"shortname": "SHORT1", "system_id": "1"})
        assert [r["txn_num"] for r in rows] == ["T001", "T004"]

    def test_empty_filters_returns_all_rows(self, source: TabularDataSource) -> None:
        assert len(source.get_by_fields({})) == 5

    def test_no_match_returns_empty(self, source: TabularDataSource) -> None:
        assert source.get_by_fields({"shortname": "NOPE"}) == []
        assert source.get_by_fields({"shortname": "SHORT1", "system_id": "9"}) == []

    def test_missing_column_raises_keyerror(self, source: TabularDataSource) -> None:
        with pytest.raises(KeyError):
            source.get_by_fields({"bogus": "x"})

    def test_nan_cells_never_match(self, source: TabularDataSource) -> None:
        # E3: la fila NOVALUE tiene system_id vacío (NaN) — no matchea
        # ni con "" ni con None.
        assert source.get_by_fields({"system_id": ""}) == []
        assert source.get_by_fields({"system_id": None}) == []

    def test_rows_are_normalized(self, source: TabularDataSource) -> None:
        rows = source.get_by_fields({"shortname": "NOVALUE"})
        assert rows == [
            {"shortname": "NOVALUE", "system_id": None, "txn_num": "T005", "amount": "500"}
        ]


class TestIndexReuse:
    def test_index_built_once_per_column_combo(self, source: TabularDataSource) -> None:
        # E2: dos consultas con la misma combinación → un índice.
        source.get_by_fields({"shortname": "SHORT1"})
        source.get_by_fields({"shortname": "SHORT2"})
        source.get_by_fields({"shortname": "SHORT1", "system_id": "1"})
        assert len(source._indexes) == 2  # noqa: SLF001

    def test_filter_key_order_shares_index(self, source: TabularDataSource) -> None:
        a = source.get_by_fields({"shortname": "SHORT1", "system_id": "1"})
        b = source.get_by_fields({"system_id": "1", "shortname": "SHORT1"})
        assert a == b
        assert len(source._indexes) == 1  # noqa: SLF001


class TestConcurrency:
    def test_concurrent_queries_are_correct(self, tmp_path: Path) -> None:
        # E4: 8 threads martillando la misma fuente.
        p = tmp_path / "big.csv"
        lines = ["shortname,system_id,txn_num"]
        lines.extend(f"S{i % 10},1,T{i:04d}" for i in range(1000))
        p.write_text("\n".join(lines) + "\n")
        src = TabularDataSource(p)
        try:
            barrier = threading.Barrier(8)

            def query(worker: int) -> int:
                barrier.wait()
                total = 0
                for i in range(50):
                    rows = src.get_by_fields({"shortname": f"S{(worker + i) % 10}"})
                    total += len(rows)
                return total

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(query, range(8)))
            assert all(r == 50 * 100 for r in results)
        finally:
            src.close()
