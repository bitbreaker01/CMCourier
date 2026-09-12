"""150 — la elegibilidad de punta a punta: el censo sigue cuadrando.

Un documento que no se migra por la directiva de negocio ("sólo clientes
con producto activo") no desaparece: aparece en el censo con todas las
letras, ``CLIENT_NOT_ACTIVE`` en el balde ``EXCLUIDO``, y los números
siguen sumando — subidos + censados == total en origen.

Y la garantía inversa, que es la que justifica la spec: con una lista
ROTA la corrida **no arranca**. Si arrancara, el mismo censo impecable
diría que todos los documentos se excluyeron por cliente inactivo.

Cada adapter y servicio es real (Principio VI de la Constitución); sólo se
stubea la capa HTTP de CMIS.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import respx

from cmcourier.domain.exceptions import EligibilityListError
from cmcourier.domain.models import ReasonBucket, ReasonCode

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# TESTCLIENT01 (CIF 123456) y TESTCLIENT02 (CIF 234567) subirían los dos:
# los separa ÚNICAMENTE la lista de activos.
_TRIGGERS = [
    ("TESTCLIENT01", "123456", "1"),
    ("TESTCLIENT02", "234567", "1"),
]


def _write_trigger_csv(tmp_path: Path) -> Path:
    path = tmp_path / "triggers.csv"
    lines = ["ShortName,CIF,SystemID", *(",".join(row) for row in _TRIGGERS)]
    path.write_text("\n".join(lines) + "\n")
    return path


def _write_activos(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "clientes-activos.csv"
    path.write_text(body)
    return path


def _rows_without_reason(db_path: Path, batch_id: str) -> list[tuple[str, str]]:
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


class TestElegibilidadEnUnaCorridaReal150:
    @respx.mock
    def test_solo_sube_el_cliente_activo(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path)
        activos = _write_activos(tmp_path, "Shortname,CIF\nTESTCLIENT01,123456\n")

        pipeline = pipeline_harness.build_pipeline(triggers, eligibility_csv=activos)
        report = pipeline.run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        details = pipeline_harness.tracking_store.get_batch_details(report.batch_id)

        assert details is not None
        assert details.stage_counts["S5"]["DONE"] == 1
        reasons = {rc.reason_code: rc.count for rc in details.reason_counts}
        assert reasons == {ReasonCode.CLIENT_NOT_ACTIVE.value: 1}

    @respx.mock
    def test_el_inactivo_cae_en_el_balde_excluido_y_el_censo_cuadra(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path)
        activos = _write_activos(tmp_path, "Shortname,CIF\nTESTCLIENT01,123456\n")

        pipeline = pipeline_harness.build_pipeline(triggers, eligibility_csv=activos)
        report = pipeline.run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        details = pipeline_harness.tracking_store.get_batch_details(report.batch_id)

        assert details is not None
        assert {rc.bucket for rc in details.reason_counts} == {ReasonBucket.EXCLUIDO.value}
        uploaded = details.stage_counts["S5"]["DONE"]
        explained = sum(rc.count for rc in details.reason_counts)
        assert details.info.total_records == len(_TRIGGERS)
        assert uploaded + explained == details.info.total_records

    @respx.mock
    def test_ningun_documento_termina_sin_razon(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path)
        activos = _write_activos(tmp_path, "Shortname,CIF\nTESTCLIENT01,123456\n")

        pipeline = pipeline_harness.build_pipeline(triggers, eligibility_csv=activos)
        report = pipeline.run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()

        assert _rows_without_reason(pipeline_harness.db_path, report.batch_id) == []

    @respx.mock
    def test_la_perilla_apagada_sube_a_los_dos(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """Byte-equivalente al pre-150: sin lista, nadie queda afuera."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001", "TXN_PIPE_002"])
        triggers = _write_trigger_csv(tmp_path)

        report = pipeline_harness.build_pipeline(triggers).run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        details = pipeline_harness.tracking_store.get_batch_details(report.batch_id)

        assert details is not None
        assert details.stage_counts["S5"]["DONE"] == 2
        assert details.reason_counts == ()

    @respx.mock
    def test_la_lista_usada_queda_auditada(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """REQ-005: "¿activo según qué lista?" se responde desde el batch."""
        pipeline_harness.register_cmis_for_docs(["TXN_PIPE_001"])
        triggers = _write_trigger_csv(tmp_path)
        activos = _write_activos(tmp_path, "Shortname,CIF\nTESTCLIENT01,123456\n")

        pipeline = pipeline_harness.build_pipeline(triggers, eligibility_csv=activos)
        report = pipeline.run(source_descriptor=str(triggers))
        pipeline_harness.tracking_store.flush()
        audit = pipeline_harness.tracking_store.batch_audit(report.batch_id)

        assert audit["eligibility_source_path"] == str(activos)
        assert audit["eligibility_rows"] == "1"
        assert audit["eligibility_modified_at"] != ""


class TestUnaListaRotaNoArranca150:
    @respx.mock
    def test_una_lista_vacia_aborta_antes_del_primer_documento(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """Lo central de la spec: si la corrida siguiera, el censo diría que
        TODOS los documentos se excluyeron por cliente inactivo — un reporte
        prolijo y completamente falso."""
        triggers = _write_trigger_csv(tmp_path)
        activos = _write_activos(tmp_path, "Shortname,CIF\n")

        pipeline = pipeline_harness.build_pipeline(triggers, eligibility_csv=activos)
        with pytest.raises(EligibilityListError, match="zero rows"):
            pipeline.run(source_descriptor=str(triggers))

        # Ni un batch a medio empezar: la corrida no arrancó.
        assert pipeline_harness.tracking_store.list_batches() == []

    @respx.mock
    def test_una_columna_faltante_aborta_nombrandola(
        self,
        pipeline_harness,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        triggers = _write_trigger_csv(tmp_path)
        activos = _write_activos(tmp_path, "Shortname\nTESTCLIENT01\n")

        pipeline = pipeline_harness.build_pipeline(triggers, eligibility_csv=activos)
        with pytest.raises(EligibilityListError, match="CIF"):
            pipeline.run(source_descriptor=str(triggers))
