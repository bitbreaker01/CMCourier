"""146: el `format` declarativo enchufado en el resolver.

Dos ubicaciones y dos momentos distintos:

* ``SourceConfig.format`` corre ENTRE buscar el valor y validarlo — es lo
  que permite escribir un patrón estricto sin perder valores buenos;
* ``FieldSourceConfig.format`` corre sobre el valor GANADOR y sobre
  ``default_value``, justo antes de devolverlo — la garantía de salida.

Sin fuentes externas: las fuentes son `trigger` y `rvabrep`, que leen
directo del objeto, así el foco queda en el formato y no en el lookup.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from cmcourier.domain.exceptions import DefaultValidationFailedError, SourceFailedError
from cmcourier.domain.models import CMMapping, RVABREPDocument, TriggerRecord
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    PadConfig,
    SourceConfig,
    ValidationConfig,
    ValueFormat,
)

pytestmark = pytest.mark.unit

_NUEVE_DIGITOS = ValidationConfig(allowed_pattern=r"^\d{9}$")


def _trigger(cif: str | None = "123456") -> TriggerRecord:
    return TriggerRecord(shortname="SHORT", cif=cif, system_id="SYS")


def _document(**overrides: Any) -> RVABREPDocument:
    defaults: dict[str, Any] = {
        "system_code": "1",
        "txn_num": "999",
        "index1": "JUANPEREZ01",
        "index2": "1000",
        "index3": "",
        "index4": "",
        "index5": "",
        "index6": "",
        "index7": "FF17",
        "image_type": "B",
        "image_path": "x",
        "file_name": "x.001",
        "creation_date": datetime(2026, 5, 18),
        "last_view_date": None,
        "total_pages": 1,
        "delete_code": "",
    }
    defaults.update(overrides)
    return RVABREPDocument(**defaults)


def _mapping(*fields: str) -> CMMapping:
    return CMMapping(
        clase_id="01",
        id_rvi="FF17",
        id_corto="PT57",
        clase_name="Test",
        required_metadata_fields=fields,
    )


def _resolve(fsc: FieldSourceConfig, doc: RVABREPDocument | None = None) -> str:
    config = MetadataConfig(
        field_aliases={},
        field_sources={"BAC_Num_Cuenta": fsc},
        prefetch_enabled=False,
    )
    service = MetadataService(config, sources_registry={})
    result = service.resolve(_trigger(), doc or _document(), _mapping("BAC_Num_Cuenta"))
    return result.metadata.properties["BAC_Num_Cuenta"]


class TestFormatoPorFuente:
    """Corre ANTES de la validación: ese es el punto de la feature."""

    def test_el_valor_corto_pasa_el_patron_estricto_despues_de_formatear(self) -> None:
        # Pre-146: "1000" contra ^\d{9}$ no valida, la fuente se descarta
        # y se pierde un valor bueno por un tema de relleno.
        value = _resolve(
            FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index2",
                        validation=_NUEVE_DIGITOS,
                        format=ValueFormat(
                            trim=True,
                            strip_leading_zeros=True,
                            pad_left=PadConfig(width=9, char="0"),
                        ),
                    ),
                ),
            ),
            _document(index2="1000"),
        )
        assert value == "000001000"

    def test_la_misma_cuenta_con_ceros_adelante_da_lo_mismo(self) -> None:
        value = _resolve(
            FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index2",
                        validation=_NUEVE_DIGITOS,
                        format=ValueFormat(
                            strip_leading_zeros=True,
                            pad_left=PadConfig(width=9, char="0"),
                        ),
                    ),
                ),
            ),
            _document(index2="000001000"),
        )
        assert value == "000001000"

    def test_un_formato_que_rompe_el_patron_descarta_la_fuente(self) -> None:
        # El formato corre primero; si el resultado ya no matchea, la
        # fuente se descarta igual que siempre y se cae al default.
        value = _resolve(
            FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index2",
                        validation=_NUEVE_DIGITOS,
                        format=ValueFormat(pad_right=PadConfig(width=12, char=" ")),
                    ),
                ),
                default_value="999999999",
            ),
            _document(index2="000001000"),
        )
        assert value == "999999999"


class TestVacioDespuesDeFormatear:
    """Un ``CHAR(20)`` de AS400 lleno de espacios NO es un valor."""

    def test_la_fuente_que_queda_vacia_se_saltea(self) -> None:
        value = _resolve(
            FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index3",
                        format=ValueFormat(trim=True),
                    ),
                    SourceConfig(source_type="rvabrep", lookup_value_column="index2"),
                ),
            ),
            _document(index3=" " * 20, index2="SEGUNDA"),
        )
        assert value == "SEGUNDA"

    def test_sin_format_el_valor_de_espacios_sigue_viajando(self) -> None:
        # Contrato pre-146 intacto: sin `format`, nada cambia.
        value = _resolve(
            FieldSourceConfig(
                sources=(
                    SourceConfig(source_type="rvabrep", lookup_value_column="index3"),
                    SourceConfig(source_type="rvabrep", lookup_value_column="index2"),
                ),
            ),
            _document(index3="    ", index2="SEGUNDA"),
        )
        assert value == "    "

    def test_todas_las_fuentes_vacias_caen_al_default(self) -> None:
        value = _resolve(
            FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index3",
                        format=ValueFormat(trim=True),
                    ),
                ),
                default_value="SIN_DATO",
            ),
            _document(index3="   "),
        )
        assert value == "SIN_DATO"

    def test_todas_las_fuentes_vacias_sin_default_explota(self) -> None:
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_Num_Cuenta": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="rvabrep",
                            lookup_value_column="index3",
                            format=ValueFormat(trim=True),
                        ),
                    ),
                )
            },
            prefetch_enabled=False,
        )
        service = MetadataService(config, sources_registry={})
        with pytest.raises(SourceFailedError):
            service.resolve(_trigger(), _document(index3="   "), _mapping("BAC_Num_Cuenta"))


class TestFormatoPorCampo:
    """La garantía de salida: gane la fuente que gane, a CM llega el
    mismo formato."""

    def test_se_aplica_al_valor_ganador(self) -> None:
        value = _resolve(
            FieldSourceConfig(
                sources=(SourceConfig(source_type="rvabrep", lookup_value_column="index2"),),
                format=ValueFormat(pad_left=PadConfig(width=9, char="0")),
            ),
            _document(index2="1000"),
        )
        assert value == "000001000"

    def test_corre_despues_del_formato_por_fuente(self) -> None:
        value = _resolve(
            FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index2",
                        format=ValueFormat(trim=True, strip_leading_zeros=True),
                        validation=ValidationConfig(allowed_pattern=r"^\d{1,9}$"),
                    ),
                ),
                format=ValueFormat(pad_left=PadConfig(width=9, char="0")),
            ),
            _document(index2="  000001000  "),
        )
        assert value == "000001000"

    def test_corre_despues_de_la_validacion_de_la_fuente(self) -> None:
        # El formato de campo puede romper el patrón de la fuente: es
        # decisión del operador, y el resolver no la adivina.
        value = _resolve(
            FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index2",
                        validation=ValidationConfig(allowed_pattern=r"^\d{4}$"),
                    ),
                ),
                format=ValueFormat(pad_left=PadConfig(width=9, char="0")),
            ),
            _document(index2="1000"),
        )
        assert value == "000001000"

    def test_dos_fuentes_distintas_salen_con_el_mismo_largo(self) -> None:
        fsc = FieldSourceConfig(
            sources=(
                SourceConfig(source_type="rvabrep", lookup_value_column="index3"),
                SourceConfig(source_type="rvabrep", lookup_value_column="index2"),
            ),
            format=ValueFormat(strip_leading_zeros=True, pad_left=PadConfig(width=9, char="0")),
        )
        assert _resolve(fsc, _document(index3="000001000")) == "000001000"
        assert _resolve(fsc, _document(index3="", index2="1000")) == "000001000"


class TestFormatoSobreElDefault:
    """El default se FORMATEA y DESPUÉS se valida."""

    def test_el_default_cero_sobrevive_al_patron_de_nueve_digitos(self) -> None:
        # Pre-146 esto moría con DefaultValidationFailedError: el default
        # se validaba crudo contra el patrón de la PRIMERA fuente.
        value = _resolve(
            FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index3",
                        validation=_NUEVE_DIGITOS,
                    ),
                ),
                default_value="0",
                format=ValueFormat(pad_left=PadConfig(width=9, char="0")),
            ),
            _document(index3=""),
        )
        assert value == "000000000"

    def test_un_default_que_sigue_sin_validar_despues_de_formatear_explota(self) -> None:
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_Num_Cuenta": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="rvabrep",
                            lookup_value_column="index3",
                            validation=_NUEVE_DIGITOS,
                        ),
                    ),
                    default_value="ABC",
                    format=ValueFormat(pad_left=PadConfig(width=9, char="0")),
                )
            },
            prefetch_enabled=False,
        )
        service = MetadataService(config, sources_registry={})
        with pytest.raises(DefaultValidationFailedError) as exc:
            service.resolve(_trigger(), _document(index3=""), _mapping("BAC_Num_Cuenta"))
        # El valor reportado es el FORMATEADO: es el que no validó.
        assert exc.value.default_value == "000000ABC"

    def test_sin_sources_el_default_igual_se_formatea(self) -> None:
        value = _resolve(
            FieldSourceConfig(
                sources=(),
                default_value="FAC",
                format=ValueFormat(case="lower", pad_right=PadConfig(width=6, char=" ")),
            )
        )
        assert value == "fac   "


class TestSinFormatoNadaCambia:
    def test_contrato_pre_146_byte_identico(self) -> None:
        value = _resolve(
            FieldSourceConfig(
                sources=(SourceConfig(source_type="rvabrep", lookup_value_column="index2"),),
                default_value="X",
            ),
            _document(index2="  000001000  "),
        )
        assert value == "  000001000  "
