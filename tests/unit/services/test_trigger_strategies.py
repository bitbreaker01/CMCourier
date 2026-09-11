"""Unit tests for the S0 trigger strategies.

Real ``TabularDataSource`` over CSV fixtures (consistent with 004 / 005). The
SUT (the strategies) does no I/O of its own; the data source is wiring.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from cmcourier.adapters.sources import TabularDataSource
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.domain.models import (
    ExcludedTrigger,
    LocalScanTrigger,
    ReasonCode,
    RvabrepRowTrigger,
)
from cmcourier.domain.ports import IDataSource, S0Strategy
from cmcourier.services.triggers import (
    CsvTriggerColumnsConfig,
    CsvTriggerStrategy,
    DirectRvabrepTriggerStrategy,
    LocalScanTriggerStrategy,
    RvabrepFilters,
    SingleDocTriggerStrategy,
)

_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "services" / "triggers"

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# CsvTriggerStrategy
# ---------------------------------------------------------------------------


class TestCsvTriggerStrategy:
    @pytest.fixture
    def source(self) -> Iterator[TabularDataSource]:
        src = TabularDataSource(_FIXTURES / "trigger_list.csv")
        yield src
        src.close()

    def test_yields_records(self, source: TabularDataSource) -> None:
        strategy = CsvTriggerStrategy(source)
        records = list(strategy.acquire())
        # 5 fixture rows minus 1 blank ShortName = 4 yielded.
        assert len(records) == 4
        assert records[0].shortname == "JUANPEREZ01"
        assert records[0].cif == "123456"
        assert records[0].system_id == "1"

    def test_yields_cif_none_when_blank(self, source: TabularDataSource) -> None:
        strategy = CsvTriggerStrategy(source)
        records = list(strategy.acquire())
        # PEPELOPEZ03 has a blank CIF.
        pepe = next(r for r in records if r.shortname == "PEPELOPEZ03")
        assert pepe.cif is None

    def test_blank_rows_skipped(self, source: TabularDataSource) -> None:
        strategy = CsvTriggerStrategy(source)
        records = list(strategy.acquire())
        # The fifth row has blank ShortName; not yielded.
        for r in records:
            assert r.shortname  # non-empty

    def test_custom_columns(self) -> None:
        src = TabularDataSource(_FIXTURES / "trigger_list_alt_columns.csv")
        try:
            cfg = CsvTriggerColumnsConfig(
                col_shortname="Cliente", col_cif="Doc", col_system_id="Sistema"
            )
            strategy = CsvTriggerStrategy(src, columns=cfg)
            records = list(strategy.acquire())
            assert len(records) == 1
            assert records[0].shortname == "JUANPEREZ01"
            assert records[0].cif == "123456"
            assert records[0].system_id == "1"
        finally:
            src.close()

    def test_missing_required_column_raises(self) -> None:
        src = TabularDataSource(_FIXTURES / "trigger_list_missing_col.csv")
        try:
            strategy = CsvTriggerStrategy(src)
            with pytest.raises(ConfigurationError) as exc:
                list(strategy.acquire())
            assert exc.value.context.get("missing_column") == "ShortName"
        finally:
            src.close()

    def test_source_descriptor_ignored(self, source: TabularDataSource) -> None:
        strategy = CsvTriggerStrategy(source)
        # Non-empty descriptor must not raise; behavior identical to empty.
        records = list(strategy.acquire("some-descriptor-ignored"))
        assert len(records) == 4

    def test_is_s0strategy(self, source: TabularDataSource) -> None:
        strategy = CsvTriggerStrategy(source)
        assert isinstance(strategy, S0Strategy)


# ---------------------------------------------------------------------------
# DirectRvabrepTriggerStrategy
# ---------------------------------------------------------------------------


class TestDirectRvabrepTriggerStrategy:
    @pytest.fixture
    def source(self) -> Iterator[TabularDataSource]:
        src = TabularDataSource(_FIXTURES / "rvabrep_export.csv")
        yield src
        src.close()

    def test_yields_one_trigger_per_row_no_dedup(self, source: TabularDataSource) -> None:
        """046: the strategy no longer deduplicates by (shortname, system_id).

        The pre-046 model collapsed N rows of a client into one ``TriggerRecord``
        and then S1 re-expanded — wasted work and the wrong semantic for
        "process THIS RVABREP row". Now: 8 fixture rows minus 1 blank = 7
        ``RvabrepRowTrigger`` instances, one per surviving row.

        148: the blank row is no longer dropped — it comes out classified
        (see ``TestDirectRvabrepCensus148``), so the total is 8.
        """
        strategy = DirectRvabrepTriggerStrategy(source)
        records = [r for r in strategy.acquire() if isinstance(r, RvabrepRowTrigger)]
        assert len(records) == 7

    def test_row_carries_full_rvabrep_payload(self, source: TabularDataSource) -> None:
        """Downstream S1 reads from the row directly, so every column must
        survive — not just the audit triple."""
        strategy = DirectRvabrepTriggerStrategy(source)
        records = list(strategy.acquire())
        first = records[0]
        assert isinstance(first, RvabrepRowTrigger)
        assert first.row["ABABCD"] == "JUANPEREZ01"
        assert first.row["ABACCD"] == "123456"
        assert first.row["ABAACD"] == "1"
        assert first.row["ABAHCD"] == "FF17"  # id_rvi column survived

    def test_filter_by_systems(self, source: TabularDataSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(source, filters=RvabrepFilters(systems=("1",)))
        records = list(strategy.acquire())
        assert all(r.row["ABAACD"] == "1" for r in records if isinstance(r, RvabrepRowTrigger))
        shortnames = {r.row["ABABCD"] for r in records if isinstance(r, RvabrepRowTrigger)}
        assert shortnames == {"JUANPEREZ01", "PEPELOPEZ03"}

    def test_filter_by_document_types(self, source: TabularDataSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(
            source, filters=RvabrepFilters(document_types=("FF17",))
        )
        records = [r for r in strategy.acquire() if isinstance(r, RvabrepRowTrigger)]
        # FF17 rows: JUANPEREZ01/1 (×2 — no dedup), MARIAGOMEZ02/5,
        # EMPRESA04/2 (blank shortname dropped).
        keys = [(r.row["ABABCD"], r.row["ABAACD"]) for r in records]
        assert sorted(keys) == sorted(
            [
                ("JUANPEREZ01", "1"),
                ("JUANPEREZ01", "1"),  # duplicate row preserved post-046
                ("MARIAGOMEZ02", "5"),
                ("EMPRESA04", "2"),
            ]
        )

    def test_filter_by_both(self, source: TabularDataSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(
            source, filters=RvabrepFilters(systems=("1",), document_types=("FF17",))
        )
        records = [r for r in strategy.acquire() if isinstance(r, RvabrepRowTrigger)]
        # JUANPEREZ01/1/FF17 appears twice in the fixture (rows 1 and 6),
        # both survive post-046.
        assert len(records) == 2
        for r in records:
            assert r.row["ABABCD"] == "JUANPEREZ01"
            assert r.row["ABAACD"] == "1"
            assert r.row["ABAHCD"] == "FF17"

    def test_blank_rows_skipped(self, source: TabularDataSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(source)
        for r in strategy.acquire():
            if not isinstance(r, RvabrepRowTrigger):
                continue  # 148: classified out, never reaches S2
            # No row with blank shortname or system_id reaches downstream.
            assert r.row["ABABCD"]
            assert r.row["ABAACD"]

    def test_cif_extracted_when_present(self, source: TabularDataSource) -> None:
        """audit_row() projects from RVABREP cols; this is the shortcut for
        the migration_log trigger_cif column."""
        strategy = DirectRvabrepTriggerStrategy(source)
        records = [r for r in strategy.acquire() if isinstance(r, RvabrepRowTrigger)]
        juan = next(r for r in records if r.row["ABABCD"] == "JUANPEREZ01")
        assert juan.audit_row()["cif"] == "123456"

    def test_cif_none_when_blank(self, source: TabularDataSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(source)
        records = [r for r in strategy.acquire() if isinstance(r, RvabrepRowTrigger)]
        pepe = next(r for r in records if r.row["ABABCD"] == "PEPELOPEZ03")
        assert pepe.audit_row()["cif"] is None

    def test_source_descriptor_ignored(self, source: TabularDataSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(source)
        records = list(strategy.acquire("any-descriptor"))
        assert len(records) == 8  # 148: 7 de trabajo + 1 clasificada

    def test_is_s0strategy(self, source: TabularDataSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(source)
        assert isinstance(strategy, S0Strategy)


# ---------------------------------------------------------------------------
# 148 REQ-001 — the RVI code never reaches the SQL, and the filtered path streams
# ---------------------------------------------------------------------------


class _RecordingSource(IDataSource):
    """Wraps a real ``IDataSource`` and records which access path was used.

    Principle VI: the inner adapter is real (``TabularDataSource`` over a
    CSV fixture); only the call log is test wiring.
    """

    def __init__(self, inner: IDataSource) -> None:
        self.inner = inner
        self.get_all_calls = 0
        self.get_by_fields_in_calls: list[tuple[str, list[Any]]] = []
        self.stream_by_fields_in_calls: list[tuple[str, list[Any]]] = []

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return self.inner.query(sql, params)

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        return self.inner.query_stream(sql, params)

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self.inner.get_by_fields(filters)

    def get_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        self.get_by_fields_in_calls.append((field, list(values)))
        return self.inner.get_by_fields_in(field, values, fixed_filters)

    def stream_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        self.stream_by_fields_in_calls.append((field, list(values)))
        yield from self.inner.stream_by_fields_in(field, values, fixed_filters)

    def get_all(self) -> Iterator[dict[str, Any]]:
        self.get_all_calls += 1
        yield from self.inner.get_all()

    def count(self) -> int:
        return self.inner.count()

    def close(self) -> None:
        self.inner.close()

    # --- helpers for the assertions ------------------------------------
    @property
    def every_queried_value(self) -> list[Any]:
        seen: list[Any] = []
        for _field, values in self.get_by_fields_in_calls + self.stream_by_fields_in_calls:
            seen.extend(values)
        return seen

    @property
    def every_queried_field(self) -> list[str]:
        return [f for f, _ in self.get_by_fields_in_calls + self.stream_by_fields_in_calls]


class TestDirectRvabrepCensus148:
    """148 REQ-001/REQ-004: the RVI code never touches SQL, and every row
    the scan rejects leaves a classified item instead of a bare ``continue``."""

    @pytest.fixture
    def source(self) -> Iterator[_RecordingSource]:
        src = _RecordingSource(TabularDataSource(_FIXTURES / "rvabrep_export.csv"))
        yield src
        src.close()

    def test_document_types_never_reach_the_query(self, source: _RecordingSource) -> None:
        """The whole point: an excluded code must COME BACK from the AS400
        so it can be counted. If the code is in the ``IN``, the row never
        comes back and the census cannot see it."""
        strategy = DirectRvabrepTriggerStrategy(
            source, filters=RvabrepFilters(systems=("1",), document_types=("FF17",))
        )
        list(strategy.acquire())
        assert "FF17" not in source.every_queried_value
        assert "ABAHCD" not in source.every_queried_field

    def test_document_types_alone_never_narrows_the_query(self, source: _RecordingSource) -> None:
        """Codes alone = no ``WHERE`` at all: the scan brings everything
        and classifies in Python."""
        strategy = DirectRvabrepTriggerStrategy(
            source, filters=RvabrepFilters(document_types=("FF17",))
        )
        list(strategy.acquire())
        assert source.get_all_calls == 1
        assert source.get_by_fields_in_calls == []
        assert source.stream_by_fields_in_calls == []

    def test_systems_filter_streams_never_materializes(self, source: _RecordingSource) -> None:
        """``filters.systems: ["1"]`` is the NORMAL config with the census
        always on; a whole system must not land in a Python list."""
        strategy = DirectRvabrepTriggerStrategy(source, filters=RvabrepFilters(systems=("1",)))
        list(strategy.acquire())
        assert source.stream_by_fields_in_calls == [("ABAACD", ["1"])]
        assert source.get_by_fields_in_calls == []

    def test_acquire_is_lazy(self, source: _RecordingSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(source, filters=RvabrepFilters(systems=("1",)))
        it = strategy.acquire()
        assert source.stream_by_fields_in_calls == []
        next(it)
        assert len(source.stream_by_fields_in_calls) == 1

    def test_code_not_in_document_types_is_excluded_by_filter(
        self, source: _RecordingSource
    ) -> None:
        strategy = DirectRvabrepTriggerStrategy(
            source, filters=RvabrepFilters(document_types=("FF17",))
        )
        excluded = [
            t
            for t in strategy.acquire()
            if isinstance(t, ExcludedTrigger) and t.reason_code is ReasonCode.EXCLUDED_BY_FILTER
        ]
        # AA01, BB02, CC03 — every non-FF17 row with a usable identity.
        assert {t.id_rvi for t in excluded} == {"AA01", "BB02", "CC03"}
        assert {t.txn_num for t in excluded} == {"TXN02", "TXN04", "TXN08"}

    def test_excluded_rows_are_not_rvabrep_row_triggers(self, source: _RecordingSource) -> None:
        """An excluded document must NOT flow into S2: it is a different
        subtype, so the S1 dispatch cannot mistake it for work."""
        strategy = DirectRvabrepTriggerStrategy(
            source, filters=RvabrepFilters(document_types=("FF17",))
        )
        work = [t for t in strategy.acquire() if isinstance(t, RvabrepRowTrigger)]
        assert all(t.row["ABAHCD"] == "FF17" for t in work)

    def test_blank_identity_is_source_row_incomplete(self, source: _RecordingSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(source)
        excluded = [t for t in strategy.acquire() if isinstance(t, ExcludedTrigger)]
        assert len(excluded) == 1
        assert excluded[0].reason_code is ReasonCode.SOURCE_ROW_INCOMPLETE
        assert excluded[0].txn_num == "TXN07"
        assert excluded[0].id_rvi == "FF17"

    def test_incomplete_row_wins_over_the_code_filter(self, source: _RecordingSource) -> None:
        """A row with no shortname is unusable no matter what its code is —
        the identity problem is the one worth reporting."""
        strategy = DirectRvabrepTriggerStrategy(
            source, filters=RvabrepFilters(document_types=("FF17",))
        )
        excluded = [t for t in strategy.acquire() if isinstance(t, ExcludedTrigger)]
        by_txn = {t.txn_num: t for t in excluded}
        assert by_txn["TXN07"].reason_code is ReasonCode.SOURCE_ROW_INCOMPLETE

    def test_every_source_row_leaves_exactly_one_trigger(self, source: _RecordingSource) -> None:
        """The census invariant at S0: nothing is dropped in silence."""
        strategy = DirectRvabrepTriggerStrategy(
            source, filters=RvabrepFilters(document_types=("FF17",))
        )
        assert len(list(strategy.acquire())) == 8  # the whole fixture

    def test_excluded_trigger_carries_the_audit_triple(self, source: _RecordingSource) -> None:
        strategy = DirectRvabrepTriggerStrategy(
            source, filters=RvabrepFilters(document_types=("FF17",))
        )
        excluded = {t.txn_num: t for t in strategy.acquire() if isinstance(t, ExcludedTrigger)}
        audit = excluded["TXN02"].audit_row()
        assert audit == {"shortname": "JUANPEREZ01", "cif": "123456", "system_id": "1"}


# ---------------------------------------------------------------------------
# LocalScanTriggerStrategy (mode local_scan)
# ---------------------------------------------------------------------------


_RVABREP_FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "pipeline" / "rvabrep.csv"


def _friendly_columns() -> RvabrepColumnsConfig:  # noqa: F821 — forward ref
    from cmcourier.services.triggers.direct_rvabrep import RvabrepColumnsConfig

    return RvabrepColumnsConfig(
        col_shortname="shortname",
        col_cif="index2",
        col_system_id="system_id",
        col_id_rvi="index7",
        file_name_column="file_name",
    )


class TestLocalScanStrategy:
    def test_yields_trigger_per_matched_file(self, tmp_path: Path) -> None:
        """046: each trigger carries the scanned file path + matched RVABREP row.
        The shortname comes from the row, not from a direct trigger field."""
        # rvabrep.csv has TESTCLIENT01 with file_name=DAAAH9X4.001.
        (tmp_path / "DAAAH9X4.001").touch()
        src = TabularDataSource(_RVABREP_FIXTURE)
        try:
            strategy = LocalScanTriggerStrategy(tmp_path, src, columns=_friendly_columns())
            triggers = list(strategy.acquire())
        finally:
            src.close()
        assert all(isinstance(t, LocalScanTrigger) for t in triggers)
        shortnames = {
            t.audit_row()["shortname"] for t in triggers if isinstance(t, LocalScanTrigger)
        }
        assert "TESTCLIENT01" in shortnames
        # File path is preserved on the trigger for downstream tracking.
        paths = {t.file_path for t in triggers if isinstance(t, LocalScanTrigger)}
        assert tmp_path / "DAAAH9X4.001" in paths

    def test_filters_non_trigger_files(self, tmp_path: Path) -> None:
        # Build a focused rvabrep with exactly one .001 match so the count
        # is unambiguous.
        rvabrep = tmp_path / "rvabrep_focused.csv"
        rvabrep.write_text(
            "shortname,system_id,index2,index7,file_name\nFOCUSED,1,123456,FF17,DZZZZ.001\n"
        )
        # .002, .tmp, .txt should be ignored; only .001 reaches RVABREP.
        for name in ("DZZZZ.001", "DZZZZ.002", "stray.txt", "DZZZZ.PDF.tmp"):
            (tmp_path / name).touch()
        src = TabularDataSource(rvabrep)
        try:
            triggers = list(
                LocalScanTriggerStrategy(tmp_path, src, columns=_friendly_columns()).acquire()
            )
        finally:
            src.close()
        # Exactly one trigger from the one matching row.
        assert len(triggers) == 1
        first = triggers[0]
        assert isinstance(first, LocalScanTrigger)
        # The "friendly" rvabrep csv uses ``shortname`` column name (not the
        # AS400 physical ``ABABCD``); audit_row() projects from the
        # configured RVABREP columns but here we hand-validate via row.
        assert first.row["shortname"] == "FOCUSED"

    def test_unmatched_file_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "STRAY.PDF").touch()
        src = TabularDataSource(_RVABREP_FIXTURE)
        try:
            import logging as _logging

            with caplog.at_level(_logging.WARNING, logger="cmcourier.services.triggers.local_scan"):
                triggers = list(
                    LocalScanTriggerStrategy(tmp_path, src, columns=_friendly_columns()).acquire()
                )
        finally:
            src.close()
        assert triggers == []
        assert any(r.__dict__.get("file_name") == "STRAY.PDF" for r in caplog.records)

    def test_missing_scan_path_raises(self) -> None:
        src = TabularDataSource(_RVABREP_FIXTURE)
        try:
            strategy = LocalScanTriggerStrategy(
                Path("/this/path/does/not/exist"),
                src,
                columns=_friendly_columns(),
            )
            with pytest.raises(ConfigurationError) as ei:
                list(strategy.acquire())
        finally:
            src.close()
        # ``Path.name`` is platform-agnostic — ``.endswith("/exist")``
        # would fail on Windows where the separator is ``\``.
        assert Path(ei.value.context["scan_path"]).name == "exist"

    def test_blank_shortname_dropped(self, tmp_path: Path) -> None:
        # Build a tiny rvabrep CSV in tmp_path with a blank shortname row.
        rvabrep = tmp_path / "rvabrep_blank.csv"
        rvabrep.write_text(
            "shortname,system_id,index2,index7,file_name\n"
            ",1,123456,FF17,DXXX1.001\n"
            "TESTOK,1,123456,FF17,DXXX2.001\n"
        )
        (tmp_path / "DXXX1.001").touch()
        (tmp_path / "DXXX2.001").touch()
        src = TabularDataSource(rvabrep)
        try:
            triggers = list(
                LocalScanTriggerStrategy(tmp_path, src, columns=_friendly_columns()).acquire()
            )
        finally:
            src.close()
        assert all(isinstance(t, LocalScanTrigger) for t in triggers)
        shortnames = {t.row["shortname"] for t in triggers if isinstance(t, LocalScanTrigger)}
        assert shortnames == {"TESTOK"}

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "NTFS is case-insensitive by default — '0AAAUI0K.PDF' and "
            "'0AAAUI0K.pdf' collapse onto a single file on Windows, so the "
            "scenario this test exercises cannot be constructed there."
        ),
    )
    def test_native_pdf_case_insensitive(self, tmp_path: Path) -> None:
        # Build a rvabrep with two PDF entries, lower- and upper-case file_name.
        rvabrep = tmp_path / "rvabrep_pdf.csv"
        rvabrep.write_text(
            "shortname,system_id,index2,index7,file_name\n"
            "PDFCLIENT1,1,123456,FF18,0AAAUI0K.PDF\n"
            "PDFCLIENT2,1,123456,FF18,0AAAUI0K.pdf\n"
        )
        # Filesystem entries.
        (tmp_path / "0AAAUI0K.PDF").touch()
        (tmp_path / "0AAAUI0K.pdf").touch()
        src = TabularDataSource(rvabrep)
        try:
            triggers = list(
                LocalScanTriggerStrategy(tmp_path, src, columns=_friendly_columns()).acquire()
            )
        finally:
            src.close()
        # Both files match (case-insensitive on .PDF extension).
        # Each filesystem entry is queried; the CSV match is exact so each
        # filesystem name finds its own row.
        shortnames = {t.row["shortname"] for t in triggers if isinstance(t, LocalScanTrigger)}
        assert shortnames == {"PDFCLIENT1", "PDFCLIENT2"}

    def test_empty_directory(self, tmp_path: Path) -> None:
        src = TabularDataSource(_RVABREP_FIXTURE)
        try:
            triggers = list(
                LocalScanTriggerStrategy(tmp_path, src, columns=_friendly_columns()).acquire()
            )
        finally:
            src.close()
        assert triggers == []

    def test_empty_cif_remains_blank_in_row(self, tmp_path: Path) -> None:
        """The blank CIF survives as ``""`` in the row dict; S3 self-healing
        (via ``_trigger_cif`` helper added in 046 Phase 3) treats whitespace
        as None and resolves via BAC_CIF lookup."""
        # Build a tiny rvabrep where one row has empty index2.
        rvabrep = tmp_path / "rvabrep_cif_blank.csv"
        rvabrep.write_text(
            "shortname,system_id,index2,index7,file_name\nNOCIFCLIENT,1,,FF17,DBLANK.001\n"
        )
        (tmp_path / "DBLANK.001").touch()
        src = TabularDataSource(rvabrep)
        try:
            triggers = list(
                LocalScanTriggerStrategy(tmp_path, src, columns=_friendly_columns()).acquire()
            )
        finally:
            src.close()
        assert len(triggers) == 1
        first = triggers[0]
        assert isinstance(first, LocalScanTrigger)
        # TabularDataSource normalizes empty cells to None at read time, so
        # the row dict carries None for the empty index2 column. The S3
        # self-heal helper added in 046 Phase 3 treats both None and
        # whitespace as "no CIF" and resolves via BAC_CIF lookup.
        assert first.row["index2"] is None

    def test_is_s0strategy(self, tmp_path: Path) -> None:
        src = TabularDataSource(_RVABREP_FIXTURE)
        try:
            strategy = LocalScanTriggerStrategy(tmp_path, src, columns=_friendly_columns())
            assert isinstance(strategy, S0Strategy)
        finally:
            src.close()

    def test_default_columns_use_physical_names(self, tmp_path: Path) -> None:
        # When columns is None, defaults are AS400 physical names (ABABCD etc.).
        # We can't query CSV friendly columns with those defaults — just assert
        # construction succeeds and returns the default columns config.
        from cmcourier.services.triggers.direct_rvabrep import RvabrepColumnsConfig

        src = TabularDataSource(_RVABREP_FIXTURE)
        try:
            strategy = LocalScanTriggerStrategy(tmp_path, src)
            assert strategy._columns == RvabrepColumnsConfig()  # type: ignore[attr-defined]
        finally:
            src.close()


# ---------------------------------------------------------------------------
# SingleDocTriggerStrategy (017)
# ---------------------------------------------------------------------------


class TestSingleDocStrategy:
    def test_yields_exactly_one_trigger(self) -> None:
        strategy = SingleDocTriggerStrategy(shortname="JUANPEREZ01", system_id="1", cif="123456")
        records = list(strategy.acquire())
        assert len(records) == 1
        assert records[0].shortname == "JUANPEREZ01"
        assert records[0].system_id == "1"
        assert records[0].cif == "123456"

    def test_cif_none_propagates(self) -> None:
        strategy = SingleDocTriggerStrategy(shortname="X", system_id="1", cif=None)
        records = list(strategy.acquire())
        assert records[0].cif is None

    def test_empty_cif_treated_as_none(self) -> None:
        strategy = SingleDocTriggerStrategy(shortname="X", system_id="1", cif="")
        records = list(strategy.acquire())
        assert records[0].cif is None

    def test_is_s0strategy(self) -> None:
        strategy = SingleDocTriggerStrategy(shortname="X", system_id="1")
        assert isinstance(strategy, S0Strategy)

    def test_empty_shortname_raises(self) -> None:
        with pytest.raises(ValueError, match="shortname"):
            SingleDocTriggerStrategy(shortname="", system_id="1")

    def test_empty_system_id_raises(self) -> None:
        with pytest.raises(ValueError, match="system_id"):
            SingleDocTriggerStrategy(shortname="X", system_id="")

    def test_acquire_ignores_source_descriptor(self) -> None:
        strategy = SingleDocTriggerStrategy(shortname="X", system_id="1")
        records_a = list(strategy.acquire(""))
        records_b = list(strategy.acquire("ignored"))
        assert records_a[0].shortname == records_b[0].shortname == "X"
