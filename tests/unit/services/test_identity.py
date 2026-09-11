"""147 REQ-002: ``IdentityResolver`` — los tres slots de identidad.

El resolver resuelve ``shortname`` / ``cif`` / ``system_id`` con el MISMO
motor de ``field_sources`` (así el CIF puede llegar por una cadena de tres
saltos) y aplica ``on_missing`` por slot. Un slot no declarado cae a
``trigger.audit_row()``: exactamente el comportamiento pre-147.

El punto del error de ``fail`` es que sea DIAGNÓSTICO: nombra el slot y la
cadena completa que se intentó, fuente por fuente. Y ``max_digits`` mata el
``int(cif) if cif.isdigit() else 0``: un CIF no numérico NUNCA se convierte
en ``0`` silencioso.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

import pytest

from cmcourier.domain.exceptions import IdentityResolutionError
from cmcourier.domain.models import ClientTrigger, RVABREPDocument
from cmcourier.domain.ports import IDataSource
from cmcourier.services.identity import (
    IdentityConfig,
    IdentityResolver,
    IdentitySlotConfig,
    ResolvedIdentity,
)
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
    ValidationConfig,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSource(IDataSource):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def get_all(self) -> Iterator[dict[str, Any]]:
        yield from self.rows

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [row for row in self.rows if all(row.get(k) == v for k, v in filters.items())]

    def get_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get(field) in values]

    def stream_by_fields_in(  # 148 REQ-001
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        yield from self.get_by_fields_in(field, values, fixed_filters)

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def count(self) -> int:
        return len(self.rows)

    def close(self) -> None:
        return None


def _document(**overrides: object) -> RVABREPDocument:
    defaults: dict[str, object] = {
        "system_code": "1",
        "txn_num": "999",
        "index1": "",
        "index2": "",
        "index3": "",
        "index4": "",
        "index5": "",
        "index6": "",
        "index7": "FF17",
        "image_type": "B",
        "image_path": "x",
        "file_name": "x.001",
        "creation_date": datetime(2025, 11, 17),
        "last_view_date": None,
        "total_pages": 1,
        "delete_code": "",
    }
    defaults.update(overrides)
    return RVABREPDocument(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def metadata_service() -> MetadataService:
    clientes = _FakeSource(
        [
            {"CUSSHN": "ACMESA01", "CUSCIF": "123456789"},
            {"CUSSHN": "LARGOSA01", "CUSCIF": "1234567890"},  # 10 dígitos: no entra en CTENUM
            {"CUSSHN": "RAROSA01", "CUSCIF": "AB123"},  # no numérico
        ]
    )
    config = MetadataConfig(
        field_aliases={},
        field_sources={
            "BAC_Shortname": FieldSourceConfig(
                sources=(SourceConfig(source_type="trigger", lookup_value_column="shortname"),),
            ),
            "BAC_Sistema": FieldSourceConfig(
                sources=(SourceConfig(source_type="trigger", lookup_value_column="system_id"),),
            ),
            "BAC_CIF": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index2",
                        validation=ValidationConfig(allowed_pattern=r"^\d+$"),
                    ),
                    SourceConfig(
                        source_type="as400:clientes",
                        lookup_value_column="CUSCIF",
                        lookup_key_column="CUSSHN",
                        lookup_value_source="field.BAC_Shortname",
                    ),
                ),
            ),
        },
        prefetch_enabled=False,
    )
    return MetadataService(config, {"clientes": _FakeSource(clientes.rows)})


def _full_config(**cif_overrides: object) -> IdentityConfig:
    cif_kwargs: dict[str, Any] = {"field": "BAC_CIF"}
    cif_kwargs.update(cif_overrides)
    return IdentityConfig(
        shortname=IdentitySlotConfig(field="BAC_Shortname"),
        cif=IdentitySlotConfig(**cif_kwargs),
        system_id=IdentitySlotConfig(field="BAC_Sistema", on_missing="warn"),
    )


def _trigger(shortname: str = "ACMESA01", cif: str | None = None) -> ClientTrigger:
    return ClientTrigger(shortname=shortname, cif=cif, system_id="1")


# ---------------------------------------------------------------------------
# Resolución de los slots
# ---------------------------------------------------------------------------


class TestResolucionDeSlots:
    def test_los_tres_slots_resuelven(self, metadata_service: MetadataService) -> None:
        resolver = IdentityResolver(_full_config(), metadata_service)
        identity = resolver.resolve(_trigger(), _document())
        assert identity == ResolvedIdentity(shortname="ACMESA01", cif="123456789", system_id="1")

    def test_el_cif_llega_por_la_cadena_no_por_el_trigger(
        self, metadata_service: MetadataService
    ) -> None:
        # El trigger NO trae CIF; sale del lookup encadenado sobre el shortname.
        resolver = IdentityResolver(_full_config(), metadata_service)
        identity = resolver.resolve(_trigger(cif=None), _document())
        assert identity.cif == "123456789"

    def test_la_primera_fuente_de_la_cadena_gana(self, metadata_service: MetadataService) -> None:
        resolver = IdentityResolver(_full_config(), metadata_service)
        identity = resolver.resolve(_trigger(), _document(index2="777"))
        assert identity.cif == "777"

    def test_resolved_identity_es_frozen(self, metadata_service: MetadataService) -> None:
        identity = IdentityResolver(_full_config(), metadata_service).resolve(
            _trigger(), _document()
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            identity.cif = "otro"  # type: ignore[misc]


class TestSlotAusente:
    def test_bloque_vacio_cae_al_audit_row(self, metadata_service: MetadataService) -> None:
        resolver = IdentityResolver(IdentityConfig(), metadata_service)
        identity = resolver.resolve(_trigger(cif="999999"), _document())
        assert identity == ResolvedIdentity(shortname="ACMESA01", cif="999999", system_id="1")

    def test_slot_ausente_sin_dato_en_el_trigger_queda_vacio(
        self, metadata_service: MetadataService
    ) -> None:
        resolver = IdentityResolver(IdentityConfig(), metadata_service)
        identity = resolver.resolve(_trigger(cif=None), _document())
        assert identity.cif == ""

    def test_slot_ausente_no_dispara_la_cadena(self, metadata_service: MetadataService) -> None:
        # Sólo `cif` declarado: shortname y system_id vienen del audit_row.
        config = IdentityConfig(cif=IdentitySlotConfig(field="BAC_CIF"))
        identity = IdentityResolver(config, metadata_service).resolve(_trigger(), _document())
        assert identity.shortname == "ACMESA01"
        assert identity.system_id == "1"
        assert identity.cif == "123456789"


# ---------------------------------------------------------------------------
# on_missing
# ---------------------------------------------------------------------------


class TestOnMissing:
    def test_fail_levanta_error_de_dominio(self, metadata_service: MetadataService) -> None:
        resolver = IdentityResolver(_full_config(), metadata_service)
        with pytest.raises(IdentityResolutionError) as exc:
            resolver.resolve(_trigger(shortname="NOEXISTE"), _document())
        assert exc.value.slot == "cif"

    def test_el_mensaje_de_fail_nombra_el_slot_y_el_campo(
        self, metadata_service: MetadataService
    ) -> None:
        resolver = IdentityResolver(_full_config(), metadata_service)
        with pytest.raises(IdentityResolutionError) as exc:
            resolver.resolve(_trigger(shortname="NOEXISTE"), _document())
        message = str(exc.value)
        assert "cif" in message
        assert "BAC_CIF" in message

    def test_el_mensaje_de_fail_nombra_toda_la_cadena_intentada(
        self, metadata_service: MetadataService
    ) -> None:
        resolver = IdentityResolver(_full_config(), metadata_service)
        with pytest.raises(IdentityResolutionError) as exc:
            resolver.resolve(_trigger(shortname="NOEXISTE"), _document())
        message = str(exc.value)
        # Las DOS fuentes de BAC_CIF, en orden, más el salto previo.
        assert "rvabrep" in message
        assert "as400:clientes" in message
        assert "BAC_Shortname" in message
        assert message.index("rvabrep") < message.index("as400:clientes")

    def test_warn_deja_el_valor_vacio_y_sigue(
        self, metadata_service: MetadataService, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = IdentityConfig(cif=IdentitySlotConfig(field="BAC_CIF", on_missing="warn"))
        with caplog.at_level(logging.WARNING):
            identity = IdentityResolver(config, metadata_service).resolve(
                _trigger(shortname="NOEXISTE"), _document()
            )
        assert identity.cif == ""
        assert any("cif" in record.getMessage() for record in caplog.records)

    def test_default_usa_el_default_value(self, metadata_service: MetadataService) -> None:
        config = IdentityConfig(
            cif=IdentitySlotConfig(field="BAC_CIF", on_missing="default", default_value="000000000")
        )
        identity = IdentityResolver(config, metadata_service).resolve(
            _trigger(shortname="NOEXISTE"), _document()
        )
        assert identity.cif == "000000000"


# ---------------------------------------------------------------------------
# max_digits — la precisión REAL de CTENUM
# ---------------------------------------------------------------------------


class TestMaxDigits:
    def test_dentro_del_limite_pasa_intacto(self, metadata_service: MetadataService) -> None:
        resolver = IdentityResolver(_full_config(max_digits=9), metadata_service)
        assert resolver.resolve(_trigger(), _document()).cif == "123456789"

    def test_pasado_el_limite_aplica_on_missing_fail(
        self, metadata_service: MetadataService
    ) -> None:
        resolver = IdentityResolver(_full_config(max_digits=9), metadata_service)
        with pytest.raises(IdentityResolutionError) as exc:
            resolver.resolve(_trigger(shortname="LARGOSA01"), _document())
        assert exc.value.slot == "cif"
        assert "max_digits" in str(exc.value)

    def test_pasado_el_limite_aplica_on_missing_default(
        self, metadata_service: MetadataService
    ) -> None:
        config = IdentityConfig(
            cif=IdentitySlotConfig(
                field="BAC_CIF",
                on_missing="default",
                default_value="000000000",
                max_digits=9,
            )
        )
        identity = IdentityResolver(config, metadata_service).resolve(
            _trigger(shortname="LARGOSA01"), _document()
        )
        assert identity.cif == "000000000"

    def test_sin_max_digits_no_hay_chequeo(self, metadata_service: MetadataService) -> None:
        resolver = IdentityResolver(_full_config(), metadata_service)
        assert resolver.resolve(_trigger(shortname="LARGOSA01"), _document()).cif == "1234567890"

    def test_un_cif_no_numerico_no_se_convierte_en_cero(
        self, metadata_service: MetadataService
    ) -> None:
        # Ésta es la razón de ser de la spec: `int(cif) if cif.isdigit() else 0`
        # escribía 0 en el log del banco sin decir nada.
        resolver = IdentityResolver(_full_config(max_digits=9), metadata_service)
        identity = resolver.resolve(_trigger(shortname="RAROSA01"), _document())
        assert identity.cif == "AB123"
        assert identity.cif != "0"


# ---------------------------------------------------------------------------
# 147 REQ-003 — la semilla para S3 y el saber qué slots están declarados
# ---------------------------------------------------------------------------


class TestSemillaParaS3:
    """``resolve_outcome`` devuelve, además de la identidad, el mapa
    ``campo canónico → valor`` que S3 usa como semilla. Sin eso, S3 tendría
    que volver a recorrer el grafo para llegar a lo mismo."""

    def test_la_identidad_es_la_misma_que_devuelve_resolve(
        self, metadata_service: MetadataService
    ) -> None:
        resolver = IdentityResolver(_full_config(), metadata_service)
        outcome = resolver.resolve_outcome(_trigger(), _document())
        assert outcome.identity == resolver.resolve(_trigger(), _document())

    def test_los_campos_resueltos_viajan_por_nombre_canonico(
        self, metadata_service: MetadataService
    ) -> None:
        outcome = IdentityResolver(_full_config(), metadata_service).resolve_outcome(
            _trigger(), _document()
        )
        assert outcome.fields == {
            "BAC_Shortname": "ACMESA01",
            "BAC_CIF": "123456789",
            "BAC_Sistema": "1",
        }

    def test_un_campo_que_no_resolvio_no_se_siembra(
        self, metadata_service: MetadataService
    ) -> None:
        # `BAC_CIF` con on_missing warn y un shortname que no está en la tabla:
        # el campo no resolvió, así que no puede sembrar nada en S3.
        config = IdentityConfig(cif=IdentitySlotConfig(field="BAC_CIF", on_missing="warn"))
        outcome = IdentityResolver(config, metadata_service).resolve_outcome(
            _trigger(shortname="NO_EXISTE"), _document()
        )
        assert outcome.fields == {}

    def test_sin_slots_declarados_no_hay_semilla(self, metadata_service: MetadataService) -> None:
        outcome = IdentityResolver(IdentityConfig(), metadata_service).resolve_outcome(
            _trigger(cif="999999"), _document()
        )
        assert outcome.fields == {}


class TestSlotsDeclarados:
    """S2 necesita saber si ``identity.system_id`` está declarado: si no lo
    está, la clave del mapping sigue saliendo de ``trigger_system_id``."""

    def test_declara_lo_que_el_yaml_declaro(self, metadata_service: MetadataService) -> None:
        resolver = IdentityResolver(_full_config(), metadata_service)
        assert resolver.declares("system_id") is True
        assert resolver.declares("cif") is True

    def test_no_declara_lo_ausente(self, metadata_service: MetadataService) -> None:
        resolver = IdentityResolver(
            IdentityConfig(cif=IdentitySlotConfig(field="BAC_CIF")), metadata_service
        )
        assert resolver.declares("system_id") is False
        assert resolver.declares("shortname") is False
