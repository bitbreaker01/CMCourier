"""147 REQ-001: el grafo de dependencias entre campos se valida AL CARGAR.

``lookup_value_source: "field.<NAME>"`` crea una arista campo → campo. Una
referencia a un campo inexistente y un ciclo son errores de CONFIG: explotan
al validar el YAML, con el ciclo completo en el mensaje, y nunca en runtime
(en runtime ya sería tarde: el operador se entera el viernes con el pipeline
corriendo en vez del lunes en el preflight).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cmcourier.config.schema import MetadataConfigModel

pytestmark = pytest.mark.unit


def _lookup(from_field: str) -> dict[str, Any]:
    return {
        "source_type": "as400:clientes",
        "lookup_value_column": "CUSSHN",
        "lookup_key_column": "CUSAFI",
        "lookup_value_source": f"field.{from_field}",
    }


def _trigger_source(column: str = "cif") -> dict[str, Any]:
    return {"source_type": "trigger", "lookup_value_column": column}


class TestGrafoValido:
    def test_cadena_de_tres_saltos_valida(self) -> None:
        model = MetadataConfigModel.model_validate(
            {
                "field_sources": {
                    "BAC_Afiliado_Padre": {"sources": [_trigger_source("shortname")]},
                    "BAC_Shortname": {"sources": [_lookup("BAC_Afiliado_Padre")]},
                    "BAC_CIF": {"sources": [_lookup("BAC_Shortname")]},
                },
            }
        )
        assert set(model.field_sources) == {"BAC_Afiliado_Padre", "BAC_Shortname", "BAC_CIF"}

    def test_sin_referencias_field_no_hay_grafo(self) -> None:
        model = MetadataConfigModel.model_validate(
            {"field_sources": {"BAC_CIF": {"sources": [_trigger_source()]}}}
        )
        assert model.field_sources["BAC_CIF"].sources[0].lookup_value_source == "trigger.cif"

    def test_dos_campos_pueden_depender_del_mismo_padre(self) -> None:
        model = MetadataConfigModel.model_validate(
            {
                "field_sources": {
                    "BAC_CIF": {"sources": [_trigger_source()]},
                    "BAC_Nombre": {"sources": [_lookup("BAC_CIF")]},
                    "BAC_Cuenta": {"sources": [_lookup("BAC_CIF")]},
                },
            }
        )
        assert len(model.field_sources) == 3


class TestReferenciaInexistente:
    def test_referencia_a_campo_no_declarado_falla_al_cargar(self) -> None:
        with pytest.raises(ValidationError) as exc:
            MetadataConfigModel.model_validate(
                {"field_sources": {"BAC_CIF": {"sources": [_lookup("BAC_NO_EXISTE")]}}}
            )
        message = str(exc.value)
        assert "BAC_CIF" in message
        assert "BAC_NO_EXISTE" in message


class TestCiclos:
    def test_autociclo_imprime_el_ciclo_completo(self) -> None:
        with pytest.raises(ValidationError) as exc:
            MetadataConfigModel.model_validate(
                {"field_sources": {"A": {"sources": [_lookup("A")]}}}
            )
        assert "A -> A" in str(exc.value)

    def test_ciclo_de_tres_imprime_el_ciclo_completo_en_orden(self) -> None:
        with pytest.raises(ValidationError) as exc:
            MetadataConfigModel.model_validate(
                {
                    "field_sources": {
                        "A": {"sources": [_lookup("B")]},
                        "B": {"sources": [_lookup("C")]},
                        "C": {"sources": [_lookup("A")]},
                    },
                }
            )
        assert "A -> B -> C -> A" in str(exc.value)

    def test_ciclo_no_dice_solamente_cycle_detected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            MetadataConfigModel.model_validate(
                {
                    "field_sources": {
                        "A": {"sources": [_lookup("B")]},
                        "B": {"sources": [_lookup("A")]},
                    },
                }
            )
        message = str(exc.value)
        assert "A -> B -> A" in message
