"""150 REQ-002: el check ``eligibility_list`` del ``doctor``.

El mismo veredicto que el preflight de la corrida, pero el lunes a la
mañana en vez del viernes con el batch a medio subir. Grupo ``mapping``
(es offline: YAML + CSV, cero red) y **SKIP con la perilla apagada**, así
no cambia el veredicto de ninguna config pre-150.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cmcourier.cli.doctor import CHECK_NAMES, CheckStatus, group_of, run_doctor
from cmcourier.config.loader import Credential, Secrets, load_config

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_TESTS_ROOT = Path(__file__).parent.parent.parent
_PIPELINE_FIXTURES = _TESTS_ROOT / "fixtures" / "pipeline"
_SERVICES_FIXTURES = _TESTS_ROOT / "fixtures" / "services"
_ASSEMBLY_FIXTURES = _TESTS_ROOT / "fixtures" / "assembly"

_ACTIVOS_OK = "Shortname,CIF\nTESTCLIENT01,123456\n"
_ACTIVOS_VACIA = "Shortname,CIF\n"
_ACTIVOS_SIN_CIF = "Shortname\nTESTCLIENT01\n"


def _write_yaml(tmp_path: Path, *, activos: str | None) -> Path:
    triggers = tmp_path / "triggers.csv"
    triggers.write_text("ShortName,CIF,SystemID\nTESTCLIENT01,123456,1\n")
    lista = tmp_path / "clientes-activos.csv"
    lista.write_text(activos if activos is not None else _ACTIVOS_OK)
    block = (
        ""
        if activos is None
        else dedent(
            """\
            eligibility:
              enabled: true
              source: "csv:clientes_activos"
              match_any:
                - {field: BAC_Shortname, column: Shortname}
                - {field: BAC_CIF, column: CIF}
            """
        )
    )
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
                BAC_Shortname:
                  sources:
                    - source_type: trigger
                      lookup_value_column: shortname
              sources:
                - alias: clientes_activos
                  csv_path: {lista}
            assembly:
              source_root: {_ASSEMBLY_FIXTURES}
              temp_dir: {tmp_path / "stg"}
            cmis:
              base_url: http://cmis.example.test:9080/opencmcmis/browser
              repo_id: "$x!testrepo"
            tracking:
              db_path: {tmp_path / "tracking.db"}
            """
        )
        + block
    )
    return yaml_path


def _run(tmp_path: Path, *, activos: str | None):  # type: ignore[no-untyped-def]
    config = load_config(_write_yaml(tmp_path, activos=activos))
    secrets = Secrets({"cmis": Credential("tester", "secret-not-real")})
    report = run_doctor(config, secrets, selected="eligibility_list")
    return next(r for r in report.results if r.name == "eligibility_list")


class TestCheckEligibilityList150:
    def test_esta_registrado_en_el_grupo_mapping(self) -> None:
        assert "eligibility_list" in CHECK_NAMES
        assert group_of("eligibility_list") == "mapping"

    def test_skip_con_la_perilla_apagada(self, tmp_path: Path) -> None:
        """Sin bloque ``eligibility`` el veredicto de la config no cambia."""
        result = _run(tmp_path, activos=None)
        assert result.status is CheckStatus.SKIP

    def test_pass_con_una_lista_sana(self, tmp_path: Path) -> None:
        result = _run(tmp_path, activos=_ACTIVOS_OK)
        assert result.status is CheckStatus.PASS
        assert "1" in result.message

    def test_fail_con_una_lista_vacia(self, tmp_path: Path) -> None:
        """Cero filas no es "nadie está activo": es una lista rota."""
        result = _run(tmp_path, activos=_ACTIVOS_VACIA)
        assert result.status is CheckStatus.FAIL
        assert "zero rows" in result.message

    def test_fail_con_una_columna_faltante(self, tmp_path: Path) -> None:
        result = _run(tmp_path, activos=_ACTIVOS_SIN_CIF)
        assert result.status is CheckStatus.FAIL
        assert "CIF" in result.message
