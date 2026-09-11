"""148 — the census closes: uploaded + every bucket == the source total.

This is the property the whole spec exists for. The operator's question is
not "what did I process?" but "what was in the source, and what happened
to each thing?". If the numbers don't add up, the report is worse than no
report at all — so this suite drives a real run through several distinct
failure paths at once and asserts the arithmetic.

Every adapter and service is real (Constitution Principle VI); only the
CMIS HTTP layer is stubbed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import respx

from cmcourier.domain.models import ReasonBucket, ReasonCode

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _write_trigger_csv(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    path = tmp_path / "triggers.csv"
    lines = ["ShortName,CIF,SystemID"]
    lines.extend(",".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n")
    return path


def _rows_without_reason(db_path: Path, batch_id: str) -> list[tuple[str, str]]:
    """``(txn, status)`` de las filas que NO subieron y NO dejaron razón."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT rvabrep_txn_num, status FROM migration_log "
            "WHERE batch_id = ? AND status != 'S5_DONE' "
            "AND (reason_code IS NULL OR reason_code = '')",
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()


# Un trigger por camino distinto: uno sube, uno no tiene código mapeado, a
# uno le falta el archivo, a uno no le resuelve la metadata, y uno no
# matchea ninguna fila de RVABREP.
_TRIGGERS = [
    ("TESTCLIENT01", "123456", "1"),  # sube
    ("TESTUNMAPPED", "123456", "1"),  # CODE_NOT_MAPPED
    ("TESTMISSFILES", "123456", "1"),  # SOURCE_FILE_MISSING
    ("TESTMETAFAIL", "999999", "1"),  # METADATA_UNRESOLVED
    ("NOSUCHCLIENT", "111111", "1"),  # SOURCE_ROW_NOT_FOUND
]


class TestCensusAddsUp148:
    @respx.mock
    def test_uploaded_plus_every_bucket_equals_the_source_total(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, _TRIGGERS)

        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        details = pipeline_harness.tracking_store.get_batch_details(report.batch_id)

        assert details is not None
        uploaded = details.stage_counts["S5"]["DONE"]
        explained = sum(rc.count for rc in details.reason_counts)
        assert uploaded == 1
        assert details.info.total_records == len(_TRIGGERS)
        assert uploaded + explained == details.info.total_records

    @respx.mock
    def test_no_document_ends_without_a_reason(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """El invariante que hace imposible perder un documento en silencio."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, _TRIGGERS)

        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()

        assert _rows_without_reason(pipeline_harness.db_path, report.batch_id) == []

    @respx.mock
    def test_the_reasons_are_the_expected_ones_with_their_buckets(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """Cada falla con SU código: el balde dice quién la arregla, y no es
        la misma persona para las cuatro."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, _TRIGGERS)

        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        details = pipeline_harness.tracking_store.get_batch_details(report.batch_id)

        assert details is not None
        got = {rc.reason_code: rc.bucket for rc in details.reason_counts}
        assert got == {
            ReasonCode.CODE_NOT_MAPPED.value: ReasonBucket.BLOQUEADO.value,
            ReasonCode.METADATA_UNRESOLVED.value: ReasonBucket.BLOQUEADO.value,
            ReasonCode.SOURCE_FILE_MISSING.value: ReasonBucket.FALLO.value,
            ReasonCode.SOURCE_ROW_NOT_FOUND.value: ReasonBucket.FALLO.value,
        }

    @respx.mock
    def test_the_denominator_is_the_source_count_not_the_batch_size(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """REQ-005: ``batch_size`` es una perilla de memoria. Pre-148
        ``total_records`` guardaba ESE número y nadie avisaba."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [_TRIGGERS[0]])

        report = pipeline_harness.build_pipeline(triggers).run(
            source_descriptor=str(triggers), batch_size=500
        )
        pipeline_harness.tracking_store.flush()
        details = pipeline_harness.tracking_store.get_batch_details(report.batch_id)

        assert details is not None
        assert details.info.total_records == 1

    @respx.mock
    def test_the_census_groups_by_rvi_code(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """``id_rvi`` se llena SIEMPRE — sin él el censo no responde la
        pregunta que el operador realmente hace."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path, [_TRIGGERS[0], _TRIGGERS[1]])

        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        docs = {
            d.txn_num: d
            for d in pipeline_harness.tracking_store.list_docs_for_batch(report.batch_id)
        }

        assert docs["TXN_PIPE_001"].id_rvi == "CC03"  # el que subió también
        assert docs["TXN_PIPE_003"].id_rvi == "ZZ99"
        unmapped = next(
            rc
            for rc in _reason_counts(pipeline_harness, report.batch_id)
            if rc.reason_code == ReasonCode.CODE_NOT_MAPPED.value
        )
        assert unmapped.id_rvi == "ZZ99"


def _reason_counts(harness, batch_id: str):  # type: ignore[no-untyped-def]
    details = harness.tracking_store.get_batch_details(batch_id)
    assert details is not None
    return details.reason_counts
