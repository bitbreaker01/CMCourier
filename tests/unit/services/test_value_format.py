"""146: ``apply_format`` — la normalización pura del valor de un metadato.

Función pura, sin fuentes ni servicio: entra un string y una
:class:`ValueFormat`, sale un string. Se cubre cada transformación por
separado, el ORDEN FIJO en que se aplican y las reglas de borde, que son
las que evitan que la feature borre datos buenos en silencio.
"""

from __future__ import annotations

import pytest

from cmcourier.services.metadata import PadConfig, ValueFormat, apply_format

pytestmark = pytest.mark.unit


class TestSinFormato:
    def test_format_none_devuelve_el_valor_intacto(self) -> None:
        assert apply_format("  0012 ", None) == "  0012 "

    def test_format_vacio_es_no_op(self) -> None:
        assert apply_format("  0012 ", ValueFormat()) == "  0012 "


class TestTransformaciones:
    def test_trim(self) -> None:
        assert apply_format("  1000  ", ValueFormat(trim=True)) == "1000"

    def test_case_upper(self) -> None:
        assert apply_format("abc", ValueFormat(case="upper")) == "ABC"

    def test_case_lower(self) -> None:
        assert apply_format("ABC", ValueFormat(case="lower")) == "abc"

    def test_strip_leading_zeros(self) -> None:
        assert apply_format("000001000", ValueFormat(strip_leading_zeros=True)) == "1000"

    def test_pad_left(self) -> None:
        fmt = ValueFormat(pad_left=PadConfig(width=9, char="0"))
        assert apply_format("1000", fmt) == "000001000"

    def test_pad_right(self) -> None:
        fmt = ValueFormat(pad_right=PadConfig(width=6, char=" "))
        assert apply_format("AB", fmt) == "AB    "

    def test_truncate_se_queda_con_los_primeros_n(self) -> None:
        assert apply_format("ABCDEFG", ValueFormat(truncate=3)) == "ABC"

    def test_truncate_no_toca_un_valor_mas_corto(self) -> None:
        assert apply_format("AB", ValueFormat(truncate=5)) == "AB"


class TestOrdenFijo:
    """1 trim → 2 case → 3 strip_leading_zeros → 4 pad_left → 5 pad_right
    → 6 truncate. El orden no es configurable, y estos tests son los que
    lo clavan."""

    def test_trim_corre_antes_de_strip_leading_zeros(self) -> None:
        # Sin `trim` primero, el `lstrip("0")` no vería los ceros: el
        # valor arranca con espacio.
        fmt = ValueFormat(trim=True, strip_leading_zeros=True)
        assert apply_format("  000123  ", fmt) == "123"

    def test_case_corre_antes_de_pad(self) -> None:
        fmt = ValueFormat(case="upper", pad_left=PadConfig(width=5, char="x"))
        # Si el pad corriera antes, la 'x' del relleno saldría en mayúscula.
        assert apply_format("ab", fmt) == "xxxAB"

    def test_strip_leading_zeros_corre_antes_de_pad_left(self) -> None:
        # El caso real: la misma cuenta llega como "1000" o como
        # "000001000" y sale igual en los dos casos.
        fmt = ValueFormat(strip_leading_zeros=True, pad_left=PadConfig(width=9, char="0"))
        assert apply_format("1000", fmt) == "000001000"
        assert apply_format("000001000", fmt) == "000001000"

    def test_pad_left_corre_antes_de_pad_right(self) -> None:
        fmt = ValueFormat(
            pad_left=PadConfig(width=4, char="0"),
            pad_right=PadConfig(width=6, char="-"),
        )
        assert apply_format("12", fmt) == "0012--"

    def test_truncate_corre_ultimo(self) -> None:
        fmt = ValueFormat(pad_right=PadConfig(width=8, char="."), truncate=8)
        assert apply_format("AB", fmt) == "AB......"

    def test_cadena_completa(self) -> None:
        fmt = ValueFormat(
            trim=True,
            case="upper",
            strip_leading_zeros=True,
            pad_left=PadConfig(width=6, char="0"),
            pad_right=PadConfig(width=8, char="_"),
            truncate=8,
        )
        assert apply_format("  00ab  ", fmt) == "0000AB__"


class TestReglasDeBorde:
    def test_strip_leading_zeros_nunca_devuelve_vacio(self) -> None:
        # Un vacío significa "esta fuente no dio" y borraría un valor
        # legítimo: "00000" es el cero, no la nada.
        assert apply_format("00000", ValueFormat(strip_leading_zeros=True)) == "0"

    def test_strip_leading_zeros_sobre_un_solo_cero(self) -> None:
        assert apply_format("0", ValueFormat(strip_leading_zeros=True)) == "0"

    def test_strip_leading_zeros_sobre_vacio_deja_vacio(self) -> None:
        assert apply_format("", ValueFormat(strip_leading_zeros=True)) == ""

    def test_pad_left_no_recorta(self) -> None:
        fmt = ValueFormat(pad_left=PadConfig(width=3, char="0"))
        assert apply_format("1234567", fmt) == "1234567"

    def test_pad_right_no_recorta(self) -> None:
        fmt = ValueFormat(pad_right=PadConfig(width=3, char=" "))
        assert apply_format("1234567", fmt) == "1234567"

    def test_pad_sobre_valor_del_largo_exacto_es_no_op(self) -> None:
        fmt = ValueFormat(pad_left=PadConfig(width=4, char="0"))
        assert apply_format("1234", fmt) == "1234"

    def test_char_de_as400_todo_espacios_queda_vacio(self) -> None:
        # Un CHAR(20) sin dato entrega 20 espacios; con `trim` queda "" y
        # el resolver lo lee como "esta fuente no dio".
        assert apply_format(" " * 20, ValueFormat(trim=True)) == ""


class TestPadConfigDefaults:
    def test_pad_config_rellena_con_cero_por_default(self) -> None:
        assert PadConfig(width=3).char == "0"

    def test_value_format_es_inmutable(self) -> None:
        fmt = ValueFormat(trim=True)
        with pytest.raises(AttributeError):
            fmt.trim = False  # type: ignore[misc]
