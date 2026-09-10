"""147 REQ-002: bloque top-level ``identity:``.

Reemplaza el hardcode de ``"BAC_CIF"`` por tres slots declarativos
(``shortname`` / ``cif`` / ``system_id``), todos opcionales. Slot ausente ⇒
comportamiento pre-147 (se lee del trigger, sin cadena). ``max_digits`` es la
precisión REAL de ``CTENUM`` y sólo tiene sentido sobre el CIF.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cmcourier.config.schema import IdentityConfigModel, IdentitySlotModel, PipelineConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def root_data(tmp_path: Path) -> dict[str, Any]:
    """Un ``PipelineConfig`` mínimo válido, sin bloque ``identity``."""
    trigger = tmp_path / "triggers.csv"
    trigger.write_text("ShortName,CIF,SystemID\n")
    rvabrep = tmp_path / "rvabrep.csv"
    rvabrep.write_text("shortname,system_id,txn_num\n")
    modelo = tmp_path / "modelo.csv"
    modelo.write_text("ID RVI,ID CLASE DOCUMENTAL\n")
    assembly_root = tmp_path / "assembly"
    assembly_root.mkdir()
    return {
        "trigger": {"kind": "csv", "csv_path": str(trigger)},
        "indexing": {"source": {"kind": "csv", "csv_path": str(rvabrep)}},
        "mapping": {"csv_path": str(modelo)},
        "metadata": {
            "field_sources": {
                "BAC_CIF": {"sources": [{"source_type": "trigger", "lookup_value_column": "cif"}]},
                "BAC_Shortname": {
                    "sources": [{"source_type": "trigger", "lookup_value_column": "shortname"}]
                },
                "BAC_Sistema": {
                    "sources": [{"source_type": "trigger", "lookup_value_column": "system_id"}]
                },
            },
        },
        "assembly": {"source_root": str(assembly_root), "temp_dir": str(tmp_path / "stg")},
        "cmis": {"base_url": "http://cmis.test:9080/cmis", "repo_id": "$x!test"},
        "tracking": {"db_path": str(tmp_path / "tracking.db")},
    }


_FULL_BLOCK: dict[str, Any] = {
    "shortname": {"field": "BAC_Shortname", "on_missing": "fail"},
    "cif": {
        "field": "BAC_CIF",
        "on_missing": "fail",
        "max_digits": 9,
        "default_value": None,
    },
    "system_id": {"field": "BAC_Sistema", "on_missing": "warn"},
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBloqueOpcional:
    def test_sin_bloque_los_tres_slots_estan_ausentes(self, root_data: dict[str, Any]) -> None:
        config = PipelineConfig.model_validate(root_data)
        assert config.identity.shortname is None
        assert config.identity.cif is None
        assert config.identity.system_id is None

    def test_bloque_vacio_es_valido(self, root_data: dict[str, Any]) -> None:
        config = PipelineConfig.model_validate({**root_data, "identity": {}})
        assert config.identity == IdentityConfigModel()

    def test_un_solo_slot_declarado(self, root_data: dict[str, Any]) -> None:
        data = {**root_data, "identity": {"cif": {"field": "BAC_CIF"}}}
        config = PipelineConfig.model_validate(data)
        assert config.identity.cif is not None
        assert config.identity.cif.field == "BAC_CIF"
        assert config.identity.shortname is None

    def test_bloque_completo(self, root_data: dict[str, Any]) -> None:
        config = PipelineConfig.model_validate({**root_data, "identity": _FULL_BLOCK})
        assert config.identity.shortname is not None
        assert config.identity.shortname.field == "BAC_Shortname"
        assert config.identity.cif is not None
        assert config.identity.cif.max_digits == 9
        assert config.identity.system_id is not None
        assert config.identity.system_id.on_missing == "warn"


class TestSlot:
    def test_on_missing_default_es_fail(self) -> None:
        assert IdentitySlotModel(field="BAC_CIF").on_missing == "fail"

    def test_default_value_default_es_none(self) -> None:
        assert IdentitySlotModel(field="BAC_CIF").default_value is None

    @pytest.mark.parametrize("on_missing", ["fail", "warn"])
    def test_on_missing_admitidos_sin_default_value(self, on_missing: str) -> None:
        slot = IdentitySlotModel(field="BAC_CIF", on_missing=on_missing)  # type: ignore[arg-type]
        assert slot.on_missing == on_missing

    def test_on_missing_desconocido_rechazado(self) -> None:
        with pytest.raises(ValidationError):
            IdentitySlotModel(field="BAC_CIF", on_missing="explode")  # type: ignore[arg-type]

    def test_default_sin_default_value_es_error(self) -> None:
        with pytest.raises(ValidationError) as exc:
            IdentitySlotModel(field="BAC_CIF", on_missing="default")
        assert "default_value" in str(exc.value)

    def test_default_con_default_value_es_valido(self) -> None:
        slot = IdentitySlotModel(field="BAC_CIF", on_missing="default", default_value="0")
        assert slot.default_value == "0"

    def test_field_es_obligatorio(self) -> None:
        with pytest.raises(ValidationError):
            IdentitySlotModel()  # type: ignore[call-arg]

    def test_clave_desconocida_rechazada(self) -> None:
        with pytest.raises(ValidationError):
            IdentitySlotModel(field="BAC_CIF", max_dgits=9)  # type: ignore[call-arg]

    @pytest.mark.parametrize("max_digits", [0, -1])
    def test_max_digits_debe_ser_positivo(self, max_digits: int) -> None:
        with pytest.raises(ValidationError):
            IdentitySlotModel(field="BAC_CIF", max_digits=max_digits)


class TestMaxDigitsSoloEnCif:
    def test_max_digits_en_cif_es_valido(self) -> None:
        model = IdentityConfigModel(cif=IdentitySlotModel(field="BAC_CIF", max_digits=9))
        assert model.cif is not None
        assert model.cif.max_digits == 9

    @pytest.mark.parametrize("slot", ["shortname", "system_id"])
    def test_max_digits_fuera_de_cif_es_error(self, slot: str) -> None:
        with pytest.raises(ValidationError) as exc:
            IdentityConfigModel.model_validate({slot: {"field": "BAC_X", "max_digits": 9}})
        message = str(exc.value)
        assert "max_digits" in message
        assert slot in message


class TestFieldDebeExistirEnFieldSources:
    def test_field_desconocido_falla_al_cargar(self, root_data: dict[str, Any]) -> None:
        data = {**root_data, "identity": {"cif": {"field": "BAC_NO_EXISTE"}}}
        with pytest.raises(ValidationError) as exc:
            PipelineConfig.model_validate(data)
        message = str(exc.value)
        assert "BAC_NO_EXISTE" in message
        assert "cif" in message

    def test_field_conocido_pasa(self, root_data: dict[str, Any]) -> None:
        data = {**root_data, "identity": {"cif": {"field": "BAC_CIF"}}}
        config = PipelineConfig.model_validate(data)
        assert config.identity.cif is not None
