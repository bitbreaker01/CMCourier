"""084: ``MetadataService`` soporta ``source_type: "as400:<alias>"``
y ``lookup_value_source`` configurable (vs CIF hardcoded pre-084).

Estos tests usan stubs ``IDataSource`` (no conexión real AS400) —
el `port` ``IDataSource`` ya es el contrato unificado, el adapter
real ``As400DataSource`` se cubre en otros tests de integración.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

import pytest

from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.domain.models import CMMapping, RVABREPDocument, TriggerRecord
from cmcourier.domain.ports import IDataSource
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
)

pytestmark = pytest.mark.unit


class _InMemorySource(IDataSource):
    """Stub mínimo de IDataSource — sirve para CSV y AS400 indistintamente."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError("_InMemorySource no ejecuta SQL crudo")

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        raise NotImplementedError("_InMemorySource no ejecuta SQL crudo")

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [r for r in self._rows if all(r.get(k) == v for k, v in filters.items())]

    def get_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            r
            for r in self._rows
            if r.get(field) in values and all(r.get(k) == v for k, v in fixed_filters.items())
        ]

    def stream_by_fields_in(  # 148 REQ-001
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        yield from self.get_by_fields_in(field, values, fixed_filters)

    def get_all(self) -> Iterator[dict[str, Any]]:
        return iter(self._rows)

    def count(self) -> int:
        return len(self._rows)

    def close(self) -> None:
        pass


def _trigger(cif: str | None = "123456", shortname: str = "ACME") -> TriggerRecord:
    return TriggerRecord(shortname=shortname, cif=cif, system_id="SYS")


def _document(**overrides: Any) -> RVABREPDocument:
    defaults: dict[str, Any] = {
        "system_code": "1",
        "txn_num": "TXN999",
        "index1": "DOC",
        "index2": "123456",
        "index3": "EXTRA",
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


# ---------------------------------------------------------------------------
# AS400 lookup contra CIF del trigger (mismo contrato que CSV pre-084)
# ---------------------------------------------------------------------------


class TestAS400LookupByCif:
    def test_as400_resolves_with_default_lookup_value_source(self) -> None:
        as400_source = _InMemorySource(
            [
                {"CIF": "123456", "NOMBRE": "Juan Pérez"},
                {"CIF": "999999", "NOMBRE": "Otro Cliente"},
            ]
        )
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_Nombre": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="as400:clientes_as400",
                            lookup_key_column="CIF",
                            lookup_value_column="NOMBRE",
                            # lookup_value_source default: "trigger.cif"
                        ),
                    ),
                ),
            },
            prefetch_enabled=True,
        )
        service = MetadataService(config, sources_registry={"clientes_as400": as400_source})
        result = service.resolve(_trigger("123456"), _document(), _mapping("BAC_Nombre"))
        assert result.metadata.properties["BAC_Nombre"] == "Juan Pérez"

    def test_as400_misses_when_no_match(self) -> None:
        as400_source = _InMemorySource([{"CIF": "999999", "NOMBRE": "Otro"}])
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_Nombre": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="as400:clientes_as400",
                            lookup_key_column="CIF",
                            lookup_value_column="NOMBRE",
                        ),
                    ),
                    default_value="UNKNOWN",
                ),
            },
            prefetch_enabled=True,
        )
        service = MetadataService(config, sources_registry={"clientes_as400": as400_source})
        result = service.resolve(_trigger("123456"), _document(), _mapping("BAC_Nombre"))
        assert result.metadata.properties["BAC_Nombre"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# lookup_value_source configurable — RVABREP column
# ---------------------------------------------------------------------------


class TestLookupValueSourceFromRvabrep:
    def test_lookup_by_rvabrep_txn_num_against_as400(self) -> None:
        as400_source = _InMemorySource(
            [
                {"TXN": "TXN999", "DESC": "Pago de servicios"},
                {"TXN": "TXN001", "DESC": "Otro"},
            ]
        )
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_Descripcion": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="as400:operaciones",
                            lookup_key_column="TXN",
                            lookup_value_column="DESC",
                            lookup_value_source="rvabrep.txn_num",
                        ),
                    ),
                ),
            },
            prefetch_enabled=True,
        )
        service = MetadataService(config, sources_registry={"operaciones": as400_source})
        result = service.resolve(
            _trigger("123456"),
            _document(txn_num="TXN999"),
            _mapping("BAC_Descripcion"),
        )
        assert result.metadata.properties["BAC_Descripcion"] == "Pago de servicios"

    def test_lookup_by_rvabrep_index1_against_csv(self) -> None:
        # Verifica que CSV también honra lookup_value_source (no es
        # exclusivo de AS400).
        csv_source = _InMemorySource(
            [
                {"DOC_KEY": "JUANPEREZ01", "TIPO": "FAC"},
                {"DOC_KEY": "OTRO", "TIPO": "REC"},
            ]
        )
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_Tipo": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:tipos",
                            lookup_key_column="DOC_KEY",
                            lookup_value_column="TIPO",
                            lookup_value_source="rvabrep.index1",
                        ),
                    ),
                ),
            },
            prefetch_enabled=True,
        )
        service = MetadataService(config, sources_registry={"tipos": csv_source})
        result = service.resolve(
            _trigger("123456"),
            _document(index1="JUANPEREZ01"),
            _mapping("BAC_Tipo"),
        )
        assert result.metadata.properties["BAC_Tipo"] == "FAC"


class TestLookupValueSourceFromTriggerAttr:
    def test_lookup_by_trigger_shortname(self) -> None:
        csv_source = _InMemorySource(
            [
                {"SHORT": "ACME", "REGION": "NORTE"},
                {"SHORT": "OTRO", "REGION": "SUR"},
            ]
        )
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_Region": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:regiones",
                            lookup_key_column="SHORT",
                            lookup_value_column="REGION",
                            lookup_value_source="trigger.shortname",
                        ),
                    ),
                ),
            },
            prefetch_enabled=True,
        )
        service = MetadataService(config, sources_registry={"regiones": csv_source})
        result = service.resolve(
            _trigger("123456", shortname="ACME"),
            _document(),
            _mapping("BAC_Region"),
        )
        assert result.metadata.properties["BAC_Region"] == "NORTE"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    def test_unknown_alias_at_construction(self) -> None:
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="as400:no_existe",
                            lookup_key_column="CIF",
                            lookup_value_column="X",
                        ),
                    ),
                ),
            },
            prefetch_enabled=True,
        )
        with pytest.raises(ConfigurationError):
            MetadataService(config, sources_registry={})

    def test_unknown_attribute_in_rvabrep_scope_raises(self) -> None:
        as400_source = _InMemorySource([{"K": "v", "V": "x"}])
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="as400:s",
                            lookup_key_column="K",
                            lookup_value_column="V",
                            lookup_value_source="rvabrep.no_existe",
                        ),
                    ),
                ),
            },
            prefetch_enabled=True,
        )
        service = MetadataService(config, sources_registry={"s": as400_source})
        with pytest.raises(ConfigurationError):
            service.resolve(_trigger(), _document(), _mapping("BAC_X"))
