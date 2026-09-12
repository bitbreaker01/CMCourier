"""Unit tests for ``cmcourier.domain.models``.

Covers helpers (``parse_cymmdd``, ``is_pdf_filename``, ``compute_cm_*``), the
``StageStatus`` enum, and every dataclass: construction, validation rejection,
computed properties, frozen-ness.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from cmcourier.domain.models import (
    REASON_BUCKETS,
    CMMapping,
    MigrationRecord,
    ReasonBucket,
    ReasonCode,
    ResolvedMetadata,
    RVABREPDocument,
    StagedFile,
    StageStatus,
    TriggerRecord,
    compute_cm_folder,
    compute_cm_object_type,
    is_pdf_filename,
    parse_cymmdd,
)

# ---------------------------------------------------------------------------
# Phase 2 — StageStatus
# ---------------------------------------------------------------------------


class TestStageStatus:
    def test_value_equals_name(self) -> None:
        assert StageStatus.S1_PENDING.value == "S1_PENDING"
        assert StageStatus.S5_DONE.value == "S5_DONE"
        assert StageStatus.SKIPPED.value == "SKIPPED"

    def test_string_subclass(self) -> None:
        # str(Enum) form differs by version, but equality with .value still holds
        assert StageStatus.S1_DONE == "S1_DONE"

    def test_terminal_for_stage_returns_done_failed(self) -> None:
        assert StageStatus.terminal_for_stage(1) == (StageStatus.S1_DONE, StageStatus.S1_FAILED)
        assert StageStatus.terminal_for_stage(5) == (StageStatus.S5_DONE, StageStatus.S5_FAILED)

    def test_terminal_for_stage_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            StageStatus.terminal_for_stage(0)
        with pytest.raises(ValueError):
            StageStatus.terminal_for_stage(6)
        with pytest.raises(ValueError):
            StageStatus.terminal_for_stage(-1)

    def test_all_stage_values_present(self) -> None:
        # Every stage from S1 to S5 has PENDING / DONE / FAILED.
        for stage in range(1, 6):
            for state in ("PENDING", "DONE", "FAILED"):
                name = f"S{stage}_{state}"
                assert StageStatus[name].value == name


# ---------------------------------------------------------------------------
# Phase 3 — Helpers
# ---------------------------------------------------------------------------


class TestParseCymmdd:
    def test_canonical_example(self) -> None:
        # "1251117" = 2025-11-17 in the CYYMMDD format.
        assert parse_cymmdd("1251117") == datetime(2025, 11, 17)

    def test_century_zero_means_1900s(self) -> None:
        assert parse_cymmdd("0991231") == datetime(1999, 12, 31)
        assert parse_cymmdd("0000101") == datetime(1900, 1, 1)

    def test_too_short_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_cymmdd("125111")  # 6 chars

    def test_too_long_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_cymmdd("12511170")  # 8 chars

    def test_non_digit_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_cymmdd("12X1117")

    def test_invalid_month_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_cymmdd("1251317")  # month 13

    def test_invalid_day_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_cymmdd("1251132")  # November 32

    def test_non_string_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_cymmdd(1251117)  # type: ignore[arg-type]


class TestIsPdfFilename:
    @pytest.mark.parametrize("name", ["0AAAUI0K.PDF", "0AAAUI0K.pdf", "FOO.Pdf"])
    def test_pdf_recognized_case_insensitive(self, name: str) -> None:
        assert is_pdf_filename(name) is True

    @pytest.mark.parametrize("name", ["DAAAH9X4.001", "DAAAH9X4.540", "FOO.TIF", "NO_EXT", ""])
    def test_non_pdf_rejected(self, name: str) -> None:
        assert is_pdf_filename(name) is False


class TestComputeCmFolder:
    def test_canonical_example(self) -> None:
        # "01.02.04.01.01" -> "/$type/BAC_01_02_04_01_01"
        assert compute_cm_folder("01.02.04.01.01") == "/$type/BAC_01_02_04_01_01"

    def test_no_dots(self) -> None:
        assert compute_cm_folder("XYZ") == "/$type/BAC_XYZ"


class TestComputeCmObjectType:
    def test_canonical_example(self) -> None:
        # "01.02.04.01.01" -> "$t!-2_BAC_01_02_04_01_01v-1"
        assert compute_cm_object_type("01.02.04.01.01") == "$t!-2_BAC_01_02_04_01_01v-1"


# ---------------------------------------------------------------------------
# Phase 3 — Simple models
# ---------------------------------------------------------------------------


class TestTriggerRecord:
    def test_construction_with_valid_inputs(self) -> None:
        r = TriggerRecord(shortname="JUANPEREZ01", cif="123456", system_id="1")
        assert r.shortname == "JUANPEREZ01"
        assert r.cif == "123456"
        assert r.system_id == "1"

    def test_cif_can_be_none(self) -> None:
        r = TriggerRecord(shortname="JUANPEREZ01", cif=None, system_id="1")
        assert r.cif is None

    def test_empty_shortname_raises(self) -> None:
        with pytest.raises(ValueError):
            TriggerRecord(shortname="", cif="123456", system_id="1")

    def test_empty_system_id_raises(self) -> None:
        with pytest.raises(ValueError):
            TriggerRecord(shortname="JUANPEREZ01", cif="123456", system_id="")

    def test_is_frozen(self) -> None:
        r = TriggerRecord(shortname="JUANPEREZ01", cif=None, system_id="1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.cif = "999999"  # type: ignore[misc]


class TestStagedFile:
    def test_construction(self) -> None:
        sf = StagedFile(path=Path("/tmp/foo.pdf"), size_bytes=1024, page_count=3)
        assert sf.path == Path("/tmp/foo.pdf")
        assert sf.size_bytes == 1024
        assert sf.page_count == 3

    def test_negative_size_raises(self) -> None:
        with pytest.raises(ValueError):
            StagedFile(path=Path("/tmp/foo.pdf"), size_bytes=-1, page_count=1)

    def test_negative_page_count_raises(self) -> None:
        with pytest.raises(ValueError):
            StagedFile(path=Path("/tmp/foo.pdf"), size_bytes=1024, page_count=-1)

    def test_zero_values_allowed(self) -> None:
        # An empty PDF is technically valid; let the assembler decide.
        StagedFile(path=Path("/tmp/foo.pdf"), size_bytes=0, page_count=0)

    def test_is_frozen(self) -> None:
        sf = StagedFile(path=Path("/tmp/foo.pdf"), size_bytes=1024, page_count=3)
        with pytest.raises(dataclasses.FrozenInstanceError):
            sf.size_bytes = 2048  # type: ignore[misc]


class TestResolvedMetadata:
    def test_from_dict_constructs(self) -> None:
        rm = ResolvedMetadata.from_dict({"BAC_CIF": "123456", "BAC_Nombre_Cliente": "JUAN"})
        assert rm["BAC_CIF"] == "123456"
        assert rm["BAC_Nombre_Cliente"] == "JUAN"

    def test_contains(self) -> None:
        rm = ResolvedMetadata.from_dict({"BAC_CIF": "123456"})
        assert "BAC_CIF" in rm
        assert "BAC_OTHER" not in rm

    def test_iter_and_len(self) -> None:
        rm = ResolvedMetadata.from_dict({"a": "1", "b": "2"})
        assert len(rm) == 2
        assert sorted(iter(rm)) == ["a", "b"]

    def test_external_dict_mutation_does_not_leak(self) -> None:
        src: dict[str, str] = {"BAC_CIF": "123456"}
        rm = ResolvedMetadata.from_dict(src)
        src["BAC_CIF"] = "999999"
        # The ResolvedMetadata snapshot is unaffected.
        assert rm["BAC_CIF"] == "123456"

    def test_underlying_view_is_immutable(self) -> None:
        rm = ResolvedMetadata.from_dict({"BAC_CIF": "123456"})
        # MappingProxyType raises TypeError on item assignment.
        with pytest.raises(TypeError):
            rm.properties["BAC_CIF"] = "999999"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Phase 4 — Complex models
# ---------------------------------------------------------------------------


class TestRVABREPDocument:
    def _build(self, **overrides: object) -> RVABREPDocument:
        defaults: dict[str, object] = {
            "system_code": "1",
            "txn_num": "123456789",
            "index1": "JUANPEREZ01",
            "index2": "123456",
            "index3": "",
            "index4": "",
            "index5": "",
            "index6": "",
            "index7": "FF17",
            "image_type": "B",
            "image_path": "PROD/2025/11/17",
            "file_name": "DAAAH9X4.001",
            "creation_date": datetime(2025, 11, 17),
            "last_view_date": None,
            "total_pages": 540,
            "delete_code": "",
        }
        defaults.update(overrides)
        return RVABREPDocument(**defaults)  # type: ignore[arg-type]

    def test_construction_full(self) -> None:
        d = self._build()
        assert d.txn_num == "123456789"
        assert d.creation_date == datetime(2025, 11, 17)
        assert d.last_view_date is None
        assert d.total_pages == 540

    def test_is_pdf_true_uppercase(self) -> None:
        d = self._build(file_name="0AAAUI0K.PDF")
        assert d.is_pdf is True

    def test_is_pdf_true_lowercase(self) -> None:
        d = self._build(file_name="0AAAUI0K.pdf")
        assert d.is_pdf is True

    def test_is_pdf_false_for_paged(self) -> None:
        d = self._build(file_name="DAAAH9X4.001")
        assert d.is_pdf is False

    def test_is_deleted_true(self) -> None:
        d = self._build(delete_code="D")
        assert d.is_deleted is True

    def test_is_deleted_false_when_empty(self) -> None:
        d = self._build(delete_code="")
        assert d.is_deleted is False

    def test_is_frozen(self) -> None:
        d = self._build()
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.delete_code = "D"  # type: ignore[misc]


class TestCMMapping:
    def test_construction(self) -> None:
        m = CMMapping(
            clase_id="01.02.04.01.01",
            id_rvi="FF17",
            id_corto="PT57",
            clase_name="Autorizacion SMS",
            required_metadata_fields=("BAC_CIF", "BAC_Num_Cuenta_Tarjeta"),
        )
        assert m.clase_id == "01.02.04.01.01"
        assert m.required_metadata_fields == ("BAC_CIF", "BAC_Num_Cuenta_Tarjeta")
        # 034: cmis_type defaults to "" until 035 populates the column.
        assert m.cmis_type == ""

    def test_cmis_type_explicit(self) -> None:
        m = CMMapping(
            clase_id="01.02.04.01.01",
            id_rvi="FF17",
            id_corto="PT57",
            clase_name="Autorizacion SMS",
            required_metadata_fields=(),
            cmis_type="MyCustomCMISType",
        )
        assert m.cmis_type == "MyCustomCMISType"

    def test_cm_folder_computed(self) -> None:
        m = CMMapping(
            clase_id="01.02.04.01.01",
            id_rvi="FF17",
            id_corto="PT57",
            clase_name="Autorizacion SMS",
            required_metadata_fields=(),
        )
        assert m.cm_folder == "/$type/BAC_01_02_04_01_01"

    def test_cm_object_type_computed(self) -> None:
        m = CMMapping(
            clase_id="01.02.04.01.01",
            id_rvi="FF17",
            id_corto="PT57",
            clase_name="Autorizacion SMS",
            required_metadata_fields=(),
        )
        assert m.cm_object_type == "$t!-2_BAC_01_02_04_01_01v-1"

    def test_is_frozen(self) -> None:
        m = CMMapping(
            clase_id="01.02.04.01.01",
            id_rvi="FF17",
            id_corto="PT57",
            clase_name="Autorizacion SMS",
            required_metadata_fields=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            m.clase_id = "99.99.99.99.99"  # type: ignore[misc]


class TestMigrationRecord:
    def _build(self, **overrides: object) -> MigrationRecord:
        defaults: dict[str, object] = {
            "trigger_shortname": "JUANPEREZ01",
            "trigger_cif": "123456",
            "trigger_system_id": "1",
            "rvabrep_txn_num": "123456789",
            "rvabrep_file_name": "DAAAH9X4.001",
            "batch_id": "batch-test-001",
            "status": StageStatus.S1_PENDING,
            "created_at": datetime(2025, 11, 17, 10, 30),
        }
        defaults.update(overrides)
        return MigrationRecord(**defaults)  # type: ignore[arg-type]

    def test_construction_with_required_only(self) -> None:
        r = self._build()
        assert r.cm_object_id is None
        assert r.cm_folder is None
        assert r.error_message is None
        assert r.retry_count == 0
        assert r.batch_id == "batch-test-001"

    def test_batch_id_is_required(self) -> None:
        # MigrationRecord.batch_id has no default — omission must raise TypeError.
        with pytest.raises(TypeError):
            MigrationRecord(  # type: ignore[call-arg]
                trigger_shortname="JUANPEREZ01",
                trigger_cif="123456",
                trigger_system_id="1",
                rvabrep_txn_num="123456789",
                rvabrep_file_name="DAAAH9X4.001",
                status=StageStatus.S1_PENDING,
                created_at=datetime(2025, 11, 17, 10, 30),
            )

    def test_construction_with_optional(self) -> None:
        r = self._build(
            cm_object_id="abc-123",
            cm_folder="/$type/BAC_01_02_04_01_01",
            cm_object_type="$t!-2_BAC_01_02_04_01_01v-1",
            status=StageStatus.S5_DONE,
            page_count=540,
            file_size_bytes=12345678,
        )
        assert r.cm_object_id == "abc-123"
        assert r.status == StageStatus.S5_DONE

    def test_status_must_be_stage_status(self) -> None:
        # MigrationRecord doesn't validate type at runtime (dataclass), but
        # the type system catches misuse. We assert that StageStatus values
        # are accepted and stored.
        r = self._build(status=StageStatus.S3_FAILED)
        assert r.status == StageStatus.S3_FAILED

    def test_is_frozen(self) -> None:
        r = self._build()
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.retry_count = 99  # type: ignore[misc]

    # 148 REQ-004: the census columns default to "not said anything yet".
    def test_census_fields_default_to_none(self) -> None:
        r = self._build()
        assert r.reason_code is None
        assert r.id_rvi is None

    def test_census_fields_round_trip(self) -> None:
        r = self._build(reason_code=ReasonCode.CODE_NOT_MAPPED, id_rvi="0123")
        assert r.reason_code is ReasonCode.CODE_NOT_MAPPED
        assert r.id_rvi == "0123"


# ---------------------------------------------------------------------------
# 148 — Reason taxonomy (REQ-002) and its orthogonality to StageStatus (REQ-003)
# ---------------------------------------------------------------------------


# The spec's REQ-002 table, transcribed. Kept as a literal here on purpose:
# if production code and this table ever disagree, the test is the contract.
_SPEC_TABLE: dict[str, tuple[str, ...]] = {
    "EXCLUIDO": (
        "EXCLUDED_BY_FILTER",
        "DELETED_AT_SOURCE",
        "ALREADY_UPLOADED",
        "OUT_OF_SCOPE_RESUME",
        # 150 REQ-004: decisión de negocio (el cliente no tiene producto
        # activo), no un error ni una falta de configuración.
        "CLIENT_NOT_ACTIVE",
    ),
    "BLOQUEADO": (
        "CODE_NOT_MAPPED",
        "TYPE_NOT_IN_MANIFEST",
        "IDENTITY_UNRESOLVED",
        "METADATA_UNRESOLVED",
        "SOURCE_ROW_INCOMPLETE",
    ),
    "FALLO": (
        "SOURCE_FILE_MISSING",
        "ASSEMBLY_FAILED",
        "CM_REJECTED_4XX",
        "CM_ERROR_5XX",
        "CM_TIMEOUT",
        "CM_TRANSPORT",
        "CLAIM_LOST",
        "CRASHED",
        "CANCELLED",
        "INDEXING_FAILED",
        "SOURCE_ROW_NOT_FOUND",
    ),
}


class TestReasonBucket148:
    def test_exactly_three_buckets(self) -> None:
        assert {b.value for b in ReasonBucket} == set(_SPEC_TABLE)

    def test_value_equals_name(self) -> None:
        for bucket in ReasonBucket:
            assert bucket.value == bucket.name
            assert bucket == bucket.name


class TestReasonCode148:
    def test_membership_matches_the_spec_table(self) -> None:
        expected = {code for codes in _SPEC_TABLE.values() for code in codes}
        assert {c.value for c in ReasonCode} == expected

    def test_value_equals_name(self) -> None:
        for code in ReasonCode:
            assert code.value == code.name
            assert code == code.name

    def test_every_code_has_a_bucket(self) -> None:
        """The guard: adding a code without mapping it must break the suite."""
        for code in ReasonCode:
            assert isinstance(REASON_BUCKETS[code], ReasonBucket)
        assert set(REASON_BUCKETS) == set(ReasonCode)

    def test_bucket_property_matches_the_spec_table(self) -> None:
        for bucket_name, codes in _SPEC_TABLE.items():
            for code in codes:
                assert ReasonCode(code).bucket == ReasonBucket(bucket_name)

    def test_bucket_of_accepts_raw_strings(self) -> None:
        assert ReasonCode.bucket_of("CRASHED") is ReasonBucket.FALLO
        assert ReasonCode.bucket_of(ReasonCode.ALREADY_UPLOADED) is ReasonBucket.EXCLUIDO

    def test_bucket_of_returns_none_for_unknown(self) -> None:
        """Legacy / hand-edited rows must not blow up a read projection."""
        assert ReasonCode.bucket_of("NOT_A_REAL_CODE") is None
        assert ReasonCode.bucket_of("") is None


class TestReasonCodeIsOrthogonal148:
    def test_no_reason_code_leaked_into_stage_status(self) -> None:
        """REQ-003: the taxonomy adds ZERO members to ``StageStatus``."""
        status_names = {s.name for s in StageStatus}
        assert status_names.isdisjoint({c.name for c in ReasonCode})

    def test_stage_status_membership_is_unchanged(self) -> None:
        assert {s.value for s in StageStatus} == {
            "S1_PENDING",
            "S1_DONE",
            "S1_FAILED",
            "S1_FILTERED",
            "S1_SKIPPED",
            "S2_PENDING",
            "S2_DONE",
            "S2_FAILED",
            "S3_PENDING",
            "S3_DONE",
            "S3_FAILED",
            "S4_PENDING",
            "S4_DONE",
            "S4_FAILED",
            "S5_PENDING",
            "S5_DONE",
            "S5_FAILED",
            "SKIPPED",
        }
