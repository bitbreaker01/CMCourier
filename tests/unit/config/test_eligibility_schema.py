"""150 REQ-001: bloque top-level ``eligibility:`` con perilla.

Sólo se migran los documentos de clientes **con producto activo**. La lista
la trae el banco en un CSV, y el bloque declara contra qué fuente y por qué
columnas se la consulta.

Dos cosas se testean acá porque son el contrato:

* **La perilla apagada es byte-equivalente a que el bloque no exista.**
  ``enabled: false`` (el default) no valida nada, no exige nada y no cambia
  ningún otro bloque.
* **Prendida, el YAML no puede mentir.** Sin ``source``, con un alias no
  declarado, con ``match_any`` vacío o con un ``field`` que no está en
  ``metadata.field_sources``, el error sale al CARGAR — nunca en runtime,
  donde ya sería tarde.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cmcourier.config.schema import EligibilityConfigModel, PipelineConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def root_data(tmp_path: Path) -> dict[str, Any]:
    """Un ``PipelineConfig`` mínimo válido, sin bloque ``eligibility``."""
    trigger = tmp_path / "triggers.csv"
    trigger.write_text("ShortName,CIF,SystemID\n")
    rvabrep = tmp_path / "rvabrep.csv"
    rvabrep.write_text("shortname,system_id,txn_num\n")
    modelo = tmp_path / "modelo.csv"
    modelo.write_text("ID RVI,ID CLASE DOCUMENTAL\n")
    activos = tmp_path / "clientes-activos.csv"
    activos.write_text("Shortname,CIF\nACMESA01,123456789\n")
    assembly_root = tmp_path / "assembly"
    assembly_root.mkdir()
    return {
        "trigger": {"kind": "csv", "csv_path": str(trigger)},
        "indexing": {"source": {"kind": "csv", "csv_path": str(rvabrep)}},
        "mapping": {"csv_path": str(modelo)},
        "metadata": {
            "sources": [
                {"kind": "csv", "alias": "clientes_activos", "csv_path": str(activos)},
            ],
            "field_sources": {
                "BAC_CIF": {"sources": [{"source_type": "trigger", "lookup_value_column": "cif"}]},
                "BAC_Shortname": {
                    "sources": [{"source_type": "trigger", "lookup_value_column": "shortname"}]
                },
            },
        },
        "assembly": {"source_root": str(assembly_root), "temp_dir": str(tmp_path / "stg")},
        "cmis": {"base_url": "http://cmis.test:9080/cmis", "repo_id": "$x!test"},
        "tracking": {"db_path": str(tmp_path / "tracking.db")},
    }


_FULL_BLOCK: dict[str, Any] = {
    "enabled": True,
    "source": "csv:clientes_activos",
    "match_any": [
        {"field": "BAC_Shortname", "column": "Shortname"},
        {"field": "BAC_CIF", "column": "CIF"},
    ],
}


# ---------------------------------------------------------------------------
# La perilla
# ---------------------------------------------------------------------------


class TestPerilla150:
    def test_sin_bloque_la_perilla_esta_apagada(self, root_data: dict[str, Any]) -> None:
        config = PipelineConfig.model_validate(root_data)
        assert config.eligibility.enabled is False
        assert config.eligibility.source is None
        assert config.eligibility.match_any == ()

    def test_bloque_ausente_y_bloque_apagado_son_lo_mismo(self, root_data: dict[str, Any]) -> None:
        """REQ-001: la perilla apagada es byte-equivalente a que el bloque
        no exista."""
        sin_bloque = PipelineConfig.model_validate(root_data)
        apagado = PipelineConfig.model_validate({**root_data, "eligibility": {"enabled": False}})
        assert sin_bloque.eligibility == apagado.eligibility
        assert apagado.eligibility == EligibilityConfigModel()

    def test_apagada_no_valida_nada(self, root_data: dict[str, Any]) -> None:
        """Con la perilla abajo no hay lista que verificar: un bloque a medio
        escribir no rompe una corrida que ni lo va a mirar."""
        data = {
            **root_data,
            "eligibility": {"enabled": False, "source": "csv:no_declarado", "match_any": []},
        }
        config = PipelineConfig.model_validate(data)
        assert config.eligibility.enabled is False

    def test_bloque_completo(self, root_data: dict[str, Any]) -> None:
        config = PipelineConfig.model_validate({**root_data, "eligibility": _FULL_BLOCK})
        assert config.eligibility.enabled is True
        assert config.eligibility.source == "csv:clientes_activos"
        assert [m.column for m in config.eligibility.match_any] == ["Shortname", "CIF"]
        assert [m.field for m in config.eligibility.match_any] == ["BAC_Shortname", "BAC_CIF"]


# ---------------------------------------------------------------------------
# REQ-001 — validación al cargar el YAML
# ---------------------------------------------------------------------------


class TestValidacionAlCargar150:
    def test_enabled_sin_source_falla(self, root_data: dict[str, Any]) -> None:
        block = {"enabled": True, "match_any": _FULL_BLOCK["match_any"]}
        data = {**root_data, "eligibility": block}
        with pytest.raises(ValidationError, match="source"):
            PipelineConfig.model_validate(data)

    def test_match_any_vacio_falla(self, root_data: dict[str, Any]) -> None:
        data = {
            **root_data,
            "eligibility": {"enabled": True, "source": "csv:clientes_activos", "match_any": []},
        }
        with pytest.raises(ValidationError, match="match_any"):
            PipelineConfig.model_validate(data)

    def test_source_mal_formado_falla(self, root_data: dict[str, Any]) -> None:
        data = {**root_data, "eligibility": {**_FULL_BLOCK, "source": "clientes_activos"}}
        with pytest.raises(ValidationError, match="source"):
            PipelineConfig.model_validate(data)

    def test_alias_no_declarado_falla(self, root_data: dict[str, Any]) -> None:
        data = {**root_data, "eligibility": {**_FULL_BLOCK, "source": "csv:no_declarado"}}
        with pytest.raises(ValidationError, match="no_declarado"):
            PipelineConfig.model_validate(data)

    def test_kind_que_miente_falla(self, root_data: dict[str, Any]) -> None:
        """El validador de kind que ya existe (130) aplica igual acá."""
        data = {**root_data, "eligibility": {**_FULL_BLOCK, "source": "mssql:clientes_activos"}}
        with pytest.raises(ValidationError, match="csv"):
            PipelineConfig.model_validate(data)

    def test_field_que_no_esta_en_field_sources_falla(self, root_data: dict[str, Any]) -> None:
        data = {
            **root_data,
            "eligibility": {
                **_FULL_BLOCK,
                "match_any": [{"field": "BAC_Inexistente", "column": "Shortname"}],
            },
        }
        with pytest.raises(ValidationError, match="BAC_Inexistente"):
            PipelineConfig.model_validate(data)

    def test_clave_desconocida_falla(self, root_data: dict[str, Any]) -> None:
        """El modelo es estricto como todos los demás del schema."""
        data = {**root_data, "eligibility": {**_FULL_BLOCK, "unknown_knob": 1}}
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)
