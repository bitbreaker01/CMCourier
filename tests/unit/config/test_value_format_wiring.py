"""146: el `format` del YAML llega entero a las dataclasses del servicio.

El schema valida y el servicio ejecuta; `wiring` es el único puente. Si
una clave se pierde acá, el YAML dice una cosa y el resolver hace otra —
por eso el mapeo tiene su propio test en las DOS ubicaciones.
"""

from __future__ import annotations

import pytest

from cmcourier.config.schema import MetadataConfigModel
from cmcourier.config.wiring import build_metadata_config

pytestmark = pytest.mark.unit


def _model(field: dict[str, object]) -> MetadataConfigModel:
    return MetadataConfigModel.model_validate({"field_sources": {"BAC_Num_Cuenta": field}})


class TestFormatoPorFuente:
    def test_todas_las_claves_llegan_al_source_config(self) -> None:
        config = build_metadata_config(
            _model(
                {
                    "sources": [
                        {
                            "source_type": "rvabrep",
                            "lookup_value_column": "index2",
                            "format": {
                                "trim": True,
                                "case": "upper",
                                "strip_leading_zeros": True,
                                "pad_left": {"width": 9, "char": "0"},
                                "pad_right": {"width": 12, "char": "-"},
                                "truncate": 12,
                            },
                        }
                    ]
                }
            )
        )
        fmt = config.field_sources["BAC_Num_Cuenta"].sources[0].format
        assert fmt is not None
        assert fmt.trim is True
        assert fmt.case == "upper"
        assert fmt.strip_leading_zeros is True
        assert fmt.pad_left is not None
        assert (fmt.pad_left.width, fmt.pad_left.char) == (9, "0")
        assert fmt.pad_right is not None
        assert (fmt.pad_right.width, fmt.pad_right.char) == (12, "-")
        assert fmt.truncate == 12

    def test_sin_format_queda_en_none(self) -> None:
        config = build_metadata_config(
            _model({"sources": [{"source_type": "rvabrep", "lookup_value_column": "index2"}]})
        )
        assert config.field_sources["BAC_Num_Cuenta"].sources[0].format is None


class TestFormatoPorCampo:
    def test_llega_al_field_source_config(self) -> None:
        config = build_metadata_config(
            _model(
                {
                    "sources": [{"source_type": "rvabrep", "lookup_value_column": "index2"}],
                    "default_value": "0",
                    "format": {"pad_left": {"width": 9}},
                }
            )
        )
        fsc = config.field_sources["BAC_Num_Cuenta"]
        assert fsc.format is not None
        assert fsc.format.pad_left is not None
        assert (fsc.format.pad_left.width, fsc.format.pad_left.char) == (9, "0")

    def test_pad_right_hereda_el_espacio_como_relleno(self) -> None:
        config = build_metadata_config(
            _model({"default_value": "X", "format": {"pad_right": {"width": 20}}})
        )
        fsc = config.field_sources["BAC_Num_Cuenta"]
        assert fsc.format is not None
        assert fsc.format.pad_right is not None
        assert fsc.format.pad_right.char == " "

    def test_sin_format_queda_en_none(self) -> None:
        config = build_metadata_config(_model({"default_value": "X"}))
        assert config.field_sources["BAC_Num_Cuenta"].format is None
