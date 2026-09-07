"""130: ``MetadataService`` soporta ``source_type: "mssql:<alias>"`` por el
mismo camino de lookup/prefetch que ``csv:`` / ``as400:``."""

from __future__ import annotations

import pytest

from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
)
from tests.unit.services.test_metadata_as400_lookup import (
    _document,
    _InMemorySource,
    _mapping,
    _trigger,
)

pytestmark = pytest.mark.unit


def _config(alias: str, *, prefetch: bool) -> MetadataConfig:
    return MetadataConfig(
        field_aliases={},
        field_sources={
            "BAC_Nombre_Cliente": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type=f"mssql:{alias}",
                        lookup_key_column="CIF",
                        lookup_value_column="Nombre_Cliente",
                    ),
                ),
            ),
        },
        prefetch_enabled=prefetch,
    )


class TestMssqlLookup:
    @pytest.mark.parametrize("prefetch", [True, False])
    def test_resolves_through_registry_alias(self, prefetch: bool) -> None:
        source = _InMemorySource([{"CIF": "396302", "Nombre_Cliente": "MARIA13"}])
        service = MetadataService(_config("clientes", prefetch=prefetch), {"clientes": source})
        result = service.resolve(_trigger("396302"), _document(), _mapping("BAC_Nombre_Cliente"))
        assert result.metadata.properties["BAC_Nombre_Cliente"] == "MARIA13"

    def test_unknown_mssql_alias_at_construction(self) -> None:
        """E4 — ``mssql:nadie`` falla nombrando el kind."""
        with pytest.raises(ConfigurationError, match="unknown mssql alias"):
            MetadataService(_config("nadie", prefetch=True), {})
