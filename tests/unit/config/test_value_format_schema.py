"""146: bloque `format:` declarativo en `field_sources` (schema).

El modelo vive en dos lugares — por fuente (`FieldSourceItem.format`) y
por campo (`FieldConfig.format`) — y es el MISMO modelo. Acá se cubre la
superficie declarativa: defaults, restricciones de cada clave, el
validador de `truncate` contra los `pad_*` y el `extra: forbid` que
convierte un typo del operador en un error al cargar el YAML.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cmcourier.config.schema import (
    FieldConfig,
    FieldSourceItem,
    PadLeftModel,
    PadRightModel,
    ValueFormatModel,
)

pytestmark = pytest.mark.unit


class TestDefaults:
    def test_todo_vacio_es_valido_y_es_no_op(self) -> None:
        fmt = ValueFormatModel()
        assert fmt.trim is False
        assert fmt.case is None
        assert fmt.strip_leading_zeros is False
        assert fmt.pad_left is None
        assert fmt.pad_right is None
        assert fmt.truncate is None

    def test_pad_left_rellena_con_cero_por_default(self) -> None:
        assert PadLeftModel(width=9).char == "0"

    def test_pad_right_rellena_con_espacio_por_default(self) -> None:
        assert PadRightModel(width=20).char == " "


class TestRestricciones:
    @pytest.mark.parametrize("width", [0, -1])
    def test_width_debe_ser_positivo(self, width: int) -> None:
        with pytest.raises(ValidationError):
            PadLeftModel(width=width)

    @pytest.mark.parametrize("char", ["", "ab"])
    def test_char_es_exactamente_un_caracter(self, char: str) -> None:
        with pytest.raises(ValidationError):
            PadRightModel(width=5, char=char)

    @pytest.mark.parametrize("truncate", [0, -3])
    def test_truncate_debe_ser_positivo(self, truncate: int) -> None:
        with pytest.raises(ValidationError):
            ValueFormatModel(truncate=truncate)

    def test_case_solo_admite_upper_o_lower(self) -> None:
        with pytest.raises(ValidationError):
            ValueFormatModel(case="Title")  # type: ignore[arg-type]


class TestTruncateContraPad:
    """Rellenar para después cortar no tiene lectura sensata: es un error
    de config y falla al cargar el YAML, no en runtime."""

    def test_truncate_menor_que_pad_left_falla(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ValueFormatModel(truncate=5, pad_left=PadLeftModel(width=9))
        assert "truncate" in str(exc.value)
        assert "pad_left" in str(exc.value)

    def test_truncate_menor_que_pad_right_falla(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ValueFormatModel(truncate=5, pad_right=PadRightModel(width=20))
        assert "pad_right" in str(exc.value)

    def test_truncate_igual_al_pad_es_valido(self) -> None:
        fmt = ValueFormatModel(truncate=9, pad_left=PadLeftModel(width=9))
        assert fmt.truncate == 9

    def test_truncate_mayor_que_el_pad_es_valido(self) -> None:
        fmt = ValueFormatModel(truncate=12, pad_left=PadLeftModel(width=9))
        assert fmt.truncate == 12

    def test_truncate_sin_pad_es_valido(self) -> None:
        assert ValueFormatModel(truncate=3).truncate == 3


class TestExtraForbid:
    def test_clave_desconocida_en_format_falla(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ValueFormatModel.model_validate({"trimm": True})
        assert "trimm" in str(exc.value)

    def test_clave_desconocida_en_pad_falla(self) -> None:
        with pytest.raises(ValidationError):
            PadLeftModel.model_validate({"width": 9, "fill": "0"})

    def test_format_es_frozen(self) -> None:
        fmt = ValueFormatModel(trim=True)
        with pytest.raises(ValidationError):
            fmt.trim = False  # type: ignore[misc]


class TestDosUbicaciones:
    """El mismo modelo, dos momentos: antes de validar (por fuente) y
    antes del wire (por campo)."""

    def test_por_fuente(self) -> None:
        item = FieldSourceItem.model_validate(
            {
                "source_type": "rvabrep",
                "lookup_value_column": "index2",
                "format": {
                    "trim": True,
                    "strip_leading_zeros": True,
                    "pad_left": {"width": 9, "char": "0"},
                },
            }
        )
        assert item.format is not None
        assert item.format.pad_left is not None
        assert item.format.pad_left.width == 9

    def test_por_campo(self) -> None:
        fc = FieldConfig.model_validate(
            {
                "sources": [{"source_type": "rvabrep", "lookup_value_column": "index2"}],
                "default_value": "0",
                "format": {"pad_left": {"width": 9}},
            }
        )
        assert fc.format is not None
        assert fc.format.pad_left is not None
        assert fc.format.pad_left.char == "0"

    def test_ausente_en_ambos_lados_por_default(self) -> None:
        fc = FieldConfig.model_validate(
            {"sources": [{"source_type": "rvabrep", "lookup_value_column": "index2"}]}
        )
        assert fc.format is None
        assert fc.sources[0].format is None
