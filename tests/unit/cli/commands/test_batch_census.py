"""148 REQ-006: el censo en ``batch show`` y ``batch export-report``.

El operador no puede responder "qué había en el origen y qué pasó con
cada cosa" si el reporte no lleva el denominador real, el desglose por
balde y —sobre todo— un aviso cuando los números NO cierran. Un reporte
que no cuadra y no avisa es peor que no tener reporte.

Estos tests son de renderizado puro: el store se mockea, así que lo que
se ejercita es exactamente la superficie que ve el operador.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import cmcourier.cli.commands.batch as batch_mod
from cmcourier.cli.commands.batch import batch_group
from cmcourier.domain.models import BatchDetails, BatchInfo, FailedRecord, ReasonCount

pytestmark = pytest.mark.unit

_OUTCOMES = ("DONE", "FAILED", "PENDING")


def _stage_counts(
    overrides: dict[str, dict[str, int]] | None = None,
    outcomes: tuple[str, ...] = _OUTCOMES,
) -> dict[str, dict[str, int]]:
    """Pivot rectangular S0..S5 × *outcomes*, con ceros salvo lo pisado."""
    pivot = {stage: dict.fromkeys(outcomes, 0) for stage in ("S0", "S1", "S2", "S3", "S4", "S5")}
    for stage, cells in (overrides or {}).items():
        pivot[stage].update(cells)
    return pivot


def _details(
    *,
    total_records: int = 0,
    stage_counts: dict[str, dict[str, int]] | None = None,
    reason_counts: tuple[ReasonCount, ...] = (),
    failed_records: tuple[FailedRecord, ...] = (),
) -> BatchDetails:
    return BatchDetails(
        info=BatchInfo(
            batch_id="B-148",
            started_at=datetime(2026, 1, 1, 10, 0),
            completed_at=datetime(2026, 1, 1, 11, 0),
            total_records=total_records,
        ),
        stage_counts=stage_counts if stage_counts is not None else _stage_counts(),
        failed_records=failed_records,
        reason_counts=reason_counts,
    )


def _invoke(args: list[str], details: BatchDetails | None, tmp_path: Path) -> Any:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("x: 1\n")
    store = MagicMock()
    store.get_batch_details.return_value = details
    with (
        patch.object(batch_mod, "_load", return_value=MagicMock()),
        patch.object(batch_mod, "configure_observability"),
        patch.object(batch_mod, "SQLiteTrackingStore", return_value=store),
    ):
        return CliRunner().invoke(batch_group, [*args, "-c", str(cfg)])


# Un censo con los tres baldes. Deliberadamente desordenado en la entrada:
# el store agrupa por (reason_code, id_rvi) alfabético, no por conteo.
_MIXED_CENSUS = (
    ReasonCount(bucket="FALLO", reason_code="CM_TIMEOUT", id_rvi="CC03", count=3),
    ReasonCount(bucket="BLOQUEADO", reason_code="CODE_NOT_MAPPED", id_rvi="ZZ99", count=5),
    ReasonCount(bucket="EXCLUIDO", reason_code="DELETED_AT_SOURCE", id_rvi="FF17", count=2),
    ReasonCount(bucket="EXCLUIDO", reason_code="EXCLUDED_BY_FILTER", id_rvi="AA01", count=4),
    ReasonCount(bucket="EXCLUIDO", reason_code="EXCLUDED_BY_FILTER", id_rvi="CC03", count=6),
)
_MIXED_TOTAL = 22  # 12 excluidos + 5 bloqueados + 3 fallos + 2 migrados


class TestOrdenDelDesglose148:
    """El orden del censo es semántico: nadie actúa → actuá → investigá."""

    def test_buckets_render_in_semantic_order(self) -> None:
        rows = batch_mod._census_rows(_MIXED_CENSUS)
        assert [r.bucket for r in rows] == [
            "EXCLUIDO",
            "EXCLUIDO",
            "EXCLUIDO",
            "BLOQUEADO",
            "FALLO",
        ]

    def test_codes_sort_by_descending_count_within_bucket(self) -> None:
        rows = batch_mod._census_rows(_MIXED_CENSUS)
        excluded = [r for r in rows if r.bucket == "EXCLUIDO"]
        # EXCLUDED_BY_FILTER suma 10 y DELETED_AT_SOURCE 2: primero el grande.
        assert [r.reason_code for r in excluded] == [
            "EXCLUDED_BY_FILTER",
            "EXCLUDED_BY_FILTER",
            "DELETED_AT_SOURCE",
        ]
        # Dentro de un código, el id_rvi más numeroso arriba.
        assert [r.id_rvi for r in excluded[:2]] == ["CC03", "AA01"]

    def test_unknown_bucket_goes_last_and_is_not_dropped(self) -> None:
        legacy = ReasonCount(bucket="", reason_code="RAZON_VIEJA", id_rvi="XX00", count=7)
        rows = batch_mod._census_rows((legacy, *_MIXED_CENSUS))
        assert rows[-1] is legacy


class TestCuadreDelCenso148:
    """Los números tienen que SUMAR, o el reporte lo tiene que decir."""

    def test_summary_adds_migrated_plus_census(self) -> None:
        details = _details(
            total_records=_MIXED_TOTAL,
            stage_counts=_stage_counts({"S5": {"DONE": 2}}),
            reason_counts=_MIXED_CENSUS,
        )
        summary = batch_mod._census_summary(details)
        assert (summary.source_total, summary.migrated, summary.accounted) == (22, 2, 20)
        assert summary.delta == 0
        assert summary.reconciles

    def test_reconciling_batch_says_it_closes_and_does_not_warn(self, tmp_path: Path) -> None:
        details = _details(
            total_records=_MIXED_TOTAL,
            stage_counts=_stage_counts({"S5": {"DONE": 2}}),
            reason_counts=_MIXED_CENSUS,
        )
        result = _invoke(["show", "B-148"], details, tmp_path)
        assert result.exit_code == 0, result.output
        assert "DESCUADRE" not in result.stdout
        cuadre = "Cuadre OK: migrados 2 + censados 20 = 22, igual al total en origen (22)."
        assert cuadre in result.stdout

    def test_missing_documents_produce_an_explicit_discrepancy_line(self, tmp_path: Path) -> None:
        details = _details(
            total_records=25,  # tres documentos que nadie explica
            stage_counts=_stage_counts({"S5": {"DONE": 2}}),
            reason_counts=_MIXED_CENSUS,
        )
        result = _invoke(["show", "B-148"], details, tmp_path)
        assert result.exit_code == 0, result.output
        line = next(ln for ln in result.stdout.splitlines() if "DESCUADRE" in ln)
        # La línea nombra AMBOS números y el delta, no sólo "no cuadra".
        assert "25" in line and "22" in line and "3 documentos sin explicar" in line

    def test_surplus_is_reported_too(self, tmp_path: Path) -> None:
        details = _details(
            total_records=12,
            stage_counts=_stage_counts({"S5": {"DONE": 2}}),
            reason_counts=_MIXED_CENSUS,
        )
        result = _invoke(["show", "B-148"], details, tmp_path)
        assert "10 documentos contados de más" in result.stdout


class TestSalidasNuevasEnLaTabla148:
    """WP1 hizo honesto el pivot: FILTERED/SKIPPED tienen que llegar."""

    def test_filtered_and_skipped_reach_the_stage_table(self, tmp_path: Path) -> None:
        counts = _stage_counts(
            {"S1": {"FILTERED": 4, "SKIPPED": 9}},
            outcomes=("DONE", "FAILED", "PENDING", "FILTERED", "SKIPPED"),
        )
        result = _invoke(["show", "B-148"], _details(stage_counts=counts), tmp_path)
        assert result.exit_code == 0, result.output
        header = next(ln for ln in result.stdout.splitlines() if ln.startswith("STAGE"))
        assert header.split() == ["STAGE", "DONE", "FAILED", "PENDING", "FILTERED", "SKIPPED"]
        s1_row = next(ln for ln in result.stdout.splitlines() if ln.startswith("S1 "))
        assert s1_row.split() == ["S1", "0", "0", "0", "4", "9"]


class TestDegradacionElegante148:
    """Un batch vacío y uno anterior a 148 tienen que renderizar igual."""

    def test_empty_batch_renders_without_census_table(self, tmp_path: Path) -> None:
        result = _invoke(["show", "B-148"], _details(), tmp_path)
        assert result.exit_code == 0, result.output
        assert "Total en origen: 0" in result.stdout
        assert "Sin razones registradas" in result.stdout
        assert "DESCUADRE" not in result.stdout

    def test_pre_148_batch_renders_and_flags_the_gap(self, tmp_path: Path) -> None:
        # Base vieja: total_records era el batch_size configurado y no hay
        # ni reason_code ni id_rvi en ninguna fila.
        details = _details(
            total_records=100,
            stage_counts=_stage_counts({"S5": {"DONE": 1}}),
            failed_records=(FailedRecord(txn_num="T1", status="S5_FAILED", error_message="boom"),),
        )
        result = _invoke(["show", "B-148"], details, tmp_path)
        assert result.exit_code == 0, result.output
        assert "DESCUADRE" in result.stdout
        assert "anterior a 148" in result.stdout
        assert "T1" in result.stdout


class TestExportacionDelCenso148:
    """CSV y JSON exportan lo mismo que ve el operador en pantalla."""

    def _details_mixed(self) -> BatchDetails:
        return _details(
            total_records=25,
            stage_counts=_stage_counts({"S5": {"DONE": 2}}),
            reason_counts=_MIXED_CENSUS,
        )

    def test_csv_carries_the_census_block(self, tmp_path: Path) -> None:
        result = _invoke(
            ["export-report", "--batch", "B-148", "--format", "csv"],
            self._details_mixed(),
            tmp_path,
        )
        assert result.exit_code == 0, result.output
        lines = result.stdout.splitlines()
        # Bloque 1 intacto para quien ya parsea el reporte.
        assert lines[0] == (
            "batch_id,status,started_at,completed_at,total_records,stage,done,failed,pending"
        )
        assert "bucket,reason_code,id_rvi,count" in lines
        assert "EXCLUIDO,EXCLUDED_BY_FILTER,CC03,6" in lines
        # Bloque de cuadre, con el descuadre explícito.
        assert "metric,value" in lines
        assert "total_en_origen,25" in lines
        assert "migrados,2" in lines
        assert "censados,20" in lines
        assert "sin_explicar,3" in lines
        assert "cuadra,no" in lines

    def test_json_carries_the_census_object(self, tmp_path: Path) -> None:
        result = _invoke(
            ["export-report", "--batch", "B-148", "--format", "json"],
            self._details_mixed(),
            tmp_path,
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        census = payload["census"]
        assert census["source_total"] == 25
        assert census["migrated"] == 2
        assert census["accounted"] == 20
        assert census["unexplained"] == 3
        assert census["reconciles"] is False
        assert census["by_bucket"] == {"EXCLUIDO": 12, "BLOQUEADO": 5, "FALLO": 3}
        assert census["reasons"][0] == {
            "bucket": "EXCLUIDO",
            "reason_code": "EXCLUDED_BY_FILTER",
            "id_rvi": "CC03",
            "count": 6,
        }

    def test_csv_stage_block_widens_with_new_outcomes(self, tmp_path: Path) -> None:
        counts = _stage_counts(
            {"S1": {"FILTERED": 4}},
            outcomes=("DONE", "FAILED", "PENDING", "FILTERED"),
        )
        result = _invoke(
            ["export-report", "--batch", "B-148", "--format", "csv"],
            _details(stage_counts=counts),
            tmp_path,
        )
        assert result.exit_code == 0, result.output
        lines = result.stdout.splitlines()
        assert lines[0].endswith(",done,failed,pending,filtered")
        assert next(ln for ln in lines if ",S1," in ln).endswith(",0,0,0,4")
