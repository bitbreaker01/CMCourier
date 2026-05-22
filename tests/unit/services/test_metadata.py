"""Unit tests for ``cmcourier.services.metadata.MetadataService``.

Real ``TabularDataSource`` instances over CSV fixtures (no IDataSource
mocks for the SUT itself). A small ``_CountingSource`` wrapper is used
ONLY to verify pre-fetch behavior — it delegates every
call to a real adapter and increments counters; it is not a stub.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cmcourier.adapters.sources import TabularDataSource
from cmcourier.domain.exceptions import (
    ConfigurationError,
    DefaultValidationFailedError,
    SourceFailedError,
)
from cmcourier.domain.models import CMMapping, RVABREPDocument, TriggerRecord
from cmcourier.domain.ports import IDataSource
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
    ValidationConfig,
)

_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "services" / "metadata"

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _CountingSource(IDataSource):
    """Delegating wrapper that counts get_all and get_by_fields invocations."""

    def __init__(self, inner: IDataSource) -> None:
        self.inner = inner
        self.get_all_calls = 0
        self.get_by_fields_calls = 0

    def get_all(self) -> Iterator[dict[str, Any]]:
        self.get_all_calls += 1
        yield from self.inner.get_all()

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.get_by_fields_calls += 1
        return self.inner.get_by_fields(filters)

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return self.inner.query(sql, params)

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        return self.inner.query_stream(sql, params)

    def get_by_fields_in(
        self,
        field: str,
        values: list[Any],
        fixed_filters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        return self.inner.get_by_fields_in(field, values, fixed_filters)

    def count(self) -> int:
        return self.inner.count()

    def close(self) -> None:
        self.inner.close()


def _make_document(**overrides: object) -> RVABREPDocument:
    defaults: dict[str, object] = {
        "system_code": "1",
        "txn_num": "999",
        "index1": "JUANPEREZ01",
        "index2": "123456",
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


def _make_mapping(*fields: str) -> CMMapping:
    return CMMapping(
        clase_id="01.02.04.01.01",
        id_rvi="FF17",
        id_corto="PT57",
        clase_name="Test",
        required_metadata_fields=fields,
    )


def _make_mapping_with_catalog(*fields: str, catalog: Mapping[str, str] | None) -> CMMapping:
    """038: ``CMMapping`` with a ``cmis_property_ids`` catalog wired in."""
    return CMMapping(
        clase_id="01.02.04.01.01",
        id_rvi="FF17",
        id_corto="PT57",
        clase_name="Test",
        required_metadata_fields=fields,
        cmis_property_ids=catalog,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sources_registry() -> Iterator[dict[str, IDataSource]]:
    clients = TabularDataSource(_FIXTURES / "clients.csv")
    accounts = TabularDataSource(_FIXTURES / "accounts.csv")
    cards = TabularDataSource(_FIXTURES / "cards.csv")
    yield {"clients": clients, "accounts": accounts, "cards": cards}
    clients.close()
    accounts.close()
    cards.close()


def _basic_config() -> MetadataConfig:
    six_digits = ValidationConfig(allowed_pattern=r"^\d{6}$")
    return MetadataConfig(
        field_aliases={
            "CIF": "BAC_CIF",
            "Nombre_Cliente": "BAC_Nombre_Cliente",
            "ShortName": "BAC_Shortname",
            "Num_Cuenta": "BAC_Num_Cuenta",
            "Num_Cuenta_Tarjeta": "BAC_Num_Cuenta_Tarjeta",
        },
        field_sources={
            "BAC_CIF": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="index2",
                        validation=six_digits,
                    ),
                    SourceConfig(
                        source_type="trigger",
                        lookup_value_column="cif",
                        validation=six_digits,
                    ),
                ),
                default_value="000000",
            ),
            "BAC_Nombre_Cliente": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="csv:clients",
                        lookup_value_column="Nombre_Cliente",
                        lookup_key_column="CIF",
                    ),
                ),
            ),
            "BAC_Shortname": FieldSourceConfig(
                sources=(SourceConfig(source_type="trigger", lookup_value_column="shortname"),),
            ),
            "BAC_Num_Cuenta": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="csv:accounts",
                        lookup_value_column="Num_Cuenta",
                        lookup_key_column="CIF",
                    ),
                ),
            ),
            "BAC_Num_Cuenta_Tarjeta": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="csv:cards",
                        lookup_value_column="Num_Cuenta_Tarjeta",
                        lookup_key_column="CIF",
                    ),
                ),
            ),
        },
        prefetch_enabled=True,
    )


@pytest.fixture
def basic_config() -> MetadataConfig:
    return _basic_config()


@pytest.fixture
def service(
    basic_config: MetadataConfig,
    sources_registry: dict[str, IDataSource],
) -> MetadataService:
    return MetadataService(basic_config, sources_registry)


# ---------------------------------------------------------------------------
# Construction + pre-fetch
# ---------------------------------------------------------------------------


class TestConstructionAndPrefetch:
    def test_pre_fetch_loads_at_construction(
        self, basic_config: MetadataConfig, sources_registry: dict[str, IDataSource]
    ) -> None:
        wrapped = {alias: _CountingSource(inner) for alias, inner in sources_registry.items()}
        MetadataService(basic_config, wrapped)
        # Each csv source should have had get_all called exactly once during prefetch.
        for alias, source in wrapped.items():
            assert source.get_all_calls == 1, f"alias={alias} get_all_calls={source.get_all_calls}"

    def test_missing_csv_alias_raises(self, sources_registry: dict[str, IDataSource]) -> None:
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:nonexistent",
                            lookup_value_column="V",
                            lookup_key_column="K",
                        ),
                    ),
                ),
            },
        )
        with pytest.raises(ConfigurationError) as exc:
            MetadataService(cfg, sources_registry)
        assert exc.value.context.get("alias") == "nonexistent"

    def test_prefetch_disabled_uses_get_by_fields_per_call(
        self, basic_config: MetadataConfig, sources_registry: dict[str, IDataSource]
    ) -> None:
        # Wrap accounts with counter; disable prefetch.
        wrapped_accounts = _CountingSource(sources_registry["accounts"])
        registry = {**sources_registry, "accounts": wrapped_accounts}
        cfg = dataclasses.replace(basic_config, prefetch_enabled=False)
        svc = MetadataService(cfg, registry)
        assert wrapped_accounts.get_all_calls == 0  # no prefetch

        # Trigger a Num_Cuenta resolution.
        trigger = TriggerRecord(shortname="JUANPEREZ01", cif="123456", system_id="1")
        document = _make_document()
        mapping = _make_mapping("Num_Cuenta")
        svc.resolve(trigger, document, mapping)
        assert wrapped_accounts.get_by_fields_calls == 1


# ---------------------------------------------------------------------------
# Vanilla resolution per source type
# ---------------------------------------------------------------------------


class TestVanillaResolution:
    def test_trigger_source(self, service: MetadataService) -> None:
        trigger = TriggerRecord(shortname="JUANPEREZ01", cif="123456", system_id="1")
        document = _make_document()
        mapping = _make_mapping("ShortName")
        result = service.resolve(trigger, document, mapping)
        assert result.metadata["BAC_Shortname"] == "JUANPEREZ01"

    def test_rvabrep_source(self, service: MetadataService) -> None:
        trigger = TriggerRecord(shortname="JUANPEREZ01", cif="123456", system_id="1")
        document = _make_document(index2="234567")
        mapping = _make_mapping("CIF")
        result = service.resolve(trigger, document, mapping)
        # RVABREP comes first in the BAC_CIF chain.
        assert result.metadata["BAC_CIF"] == "234567"

    def test_csv_source(self, service: MetadataService) -> None:
        trigger = TriggerRecord(shortname="X", cif="123456", system_id="1")
        document = _make_document()
        mapping = _make_mapping("Nombre_Cliente")
        result = service.resolve(trigger, document, mapping)
        assert result.metadata["BAC_Nombre_Cliente"] == "JUAN PEREZ TEST"


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


class TestFallbackChain:
    def test_first_source_fails_validation_second_succeeds(self, service: MetadataService) -> None:
        # rvabrep.index2="ABC" fails ^\d{6}$; trigger.cif="123456" passes.
        trigger = TriggerRecord(shortname="X", cif="123456", system_id="1")
        document = _make_document(index2="ABC")
        mapping = _make_mapping("CIF")
        result = service.resolve(trigger, document, mapping)
        assert result.metadata["BAC_CIF"] == "123456"

    def test_first_source_returns_none_second_succeeds(self, service: MetadataService) -> None:
        # rvabrep.index2 empty → skip; trigger.cif=123456 used.
        trigger = TriggerRecord(shortname="X", cif="123456", system_id="1")
        document = _make_document(index2="")
        mapping = _make_mapping("CIF")
        result = service.resolve(trigger, document, mapping)
        assert result.metadata["BAC_CIF"] == "123456"

    def test_all_sources_fail_default_used(self, service: MetadataService) -> None:
        # Both rvabrep and trigger CIF are invalid → default "000000" used.
        trigger = TriggerRecord(shortname="X", cif="abc", system_id="1")
        document = _make_document(index2="xyz")
        mapping = _make_mapping("CIF")
        result = service.resolve(trigger, document, mapping)
        assert result.metadata["BAC_CIF"] == "000000"

    def test_all_sources_fail_no_default_raises(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(SourceConfig(source_type="trigger", lookup_value_column="cif"),),
                    default_value=None,
                ),
            },
        )
        svc = MetadataService(cfg, sources_registry)
        trigger = TriggerRecord(shortname="X", cif=None, system_id="1")
        with pytest.raises(SourceFailedError) as exc:
            svc.resolve(trigger, _make_document(), _make_mapping("BAC_X"))
        assert exc.value.field_name == "BAC_X"
        assert exc.value.source == "<all>"

    def test_default_validation_fails_raises(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        # Default "abc" fails ^\d{6}$.
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="trigger",
                            lookup_value_column="cif",
                            validation=ValidationConfig(allowed_pattern=r"^\d{6}$"),
                        ),
                    ),
                    default_value="abc",
                ),
            },
        )
        svc = MetadataService(cfg, sources_registry)
        trigger = TriggerRecord(shortname="X", cif=None, system_id="1")
        with pytest.raises(DefaultValidationFailedError) as exc:
            svc.resolve(trigger, _make_document(), _make_mapping("BAC_X"))
        assert exc.value.field_name == "BAC_X"
        assert exc.value.default_value == "abc"


# ---------------------------------------------------------------------------
# CIF self-healing
# ---------------------------------------------------------------------------


class TestCifSelfHealing:
    def test_happy_path(self, service: MetadataService) -> None:
        # trigger.cif=None, BAC_CIF in mapping → resolve from rvabrep.index2 first.
        trigger = TriggerRecord(shortname="JUANPEREZ01", cif=None, system_id="1")
        document = _make_document(index2="123456")
        mapping = _make_mapping("CIF", "Nombre_Cliente")
        result = service.resolve(trigger, document, mapping)
        assert result.healed_trigger.cif == "123456"
        assert result.healed_trigger.shortname == "JUANPEREZ01"  # preserved
        assert result.metadata["BAC_CIF"] == "123456"

    def test_failure_propagates(self, service: MetadataService) -> None:
        # trigger.cif=None, rvabrep invalid, default "000000" matches → "000000".
        # Make sure default of BAC_CIF is reached. Healed trigger gets default value.
        trigger = TriggerRecord(shortname="X", cif=None, system_id="1")
        document = _make_document(index2="xxx")
        mapping = _make_mapping("CIF")
        result = service.resolve(trigger, document, mapping)
        # default "000000" is valid against ^\d{6}$
        assert result.metadata["BAC_CIF"] == "000000"
        assert result.healed_trigger.cif == "000000"

    def test_no_self_healing_when_cif_present(self, service: MetadataService) -> None:
        # trigger.cif already set → no self-heal, healed_trigger preserves the original.
        # Mapping kept minimal (only CIF) so we don't depend on csv lookups
        # for a CIF that may or may not exist in fixtures.
        trigger = TriggerRecord(shortname="X", cif="999999", system_id="1")
        document = _make_document(index2="123456")
        mapping = _make_mapping("CIF")
        result = service.resolve(trigger, document, mapping)
        # rvabrep is the first source for BAC_CIF, so its value wins,
        # but healed_trigger.cif must be the ORIGINAL trigger.cif (no self-heal).
        assert result.metadata["BAC_CIF"] == "123456"
        assert result.healed_trigger.cif == "999999"

    def test_self_healed_cif_used_for_subsequent_csv_lookups(
        self, service: MetadataService
    ) -> None:
        # trigger.cif=None, document has CIF=123456. After self-heal, csv:clients
        # lookup by CIF=123456 returns "JUAN PEREZ TEST".
        trigger = TriggerRecord(shortname="X", cif=None, system_id="1")
        document = _make_document(index2="123456")
        mapping = _make_mapping("CIF", "Nombre_Cliente")
        result = service.resolve(trigger, document, mapping)
        assert result.metadata["BAC_Nombre_Cliente"] == "JUAN PEREZ TEST"


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


class TestAliases:
    def test_alias_normalization_case_insensitive(self, service: MetadataService) -> None:
        # mapping uses "cif" lowercase — should normalize to BAC_CIF.
        trigger = TriggerRecord(shortname="X", cif="123456", system_id="1")
        document = _make_document(index2="123456")
        mapping = _make_mapping("cif")
        result = service.resolve(trigger, document, mapping)
        assert "BAC_CIF" in result.metadata.properties

    def test_canonical_already_used_directly(self, service: MetadataService) -> None:
        # mapping uses BAC_CIF directly — must NOT double-alias.
        trigger = TriggerRecord(shortname="X", cif="123456", system_id="1")
        document = _make_document(index2="123456")
        mapping = _make_mapping("BAC_CIF")
        result = service.resolve(trigger, document, mapping)
        assert result.metadata["BAC_CIF"] == "123456"

    def test_unknown_field_raises(self, service: MetadataService) -> None:
        trigger = TriggerRecord(shortname="X", cif="123456", system_id="1")
        document = _make_document()
        mapping = _make_mapping("UNKNOWN_FIELD")
        with pytest.raises(ConfigurationError) as exc:
            service.resolve(trigger, document, mapping)
        assert exc.value.context.get("field") == "UNKNOWN_FIELD"


# ---------------------------------------------------------------------------
# Source dispatch
# ---------------------------------------------------------------------------


class TestSourceDispatch:
    # 084 implementó los sources ``as400:<alias>`` — el test previo que
    # afirmaba "as400 raises NotImplementedError" quedó obsoleto y se
    # eliminó. El comportamiento real de as400 lo cubre
    # ``test_metadata_as400_lookup.py``.

    def test_unknown_source_type_raises_configuration(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(SourceConfig(source_type="weird_type", lookup_value_column="V"),),
                ),
            },
        )
        svc = MetadataService(cfg, sources_registry)
        with pytest.raises(ConfigurationError) as exc:
            svc.resolve(
                TriggerRecord(shortname="X", cif="123", system_id="1"),
                _make_document(),
                _make_mapping("BAC_X"),
            )
        assert exc.value.context.get("source_type") == "weird_type"

    def test_csv_source_alias_unknown_at_resolution_time(self) -> None:
        # Build registry that has the alias at construction (so prefetch passes)
        # then drop it before resolve. Easiest: prefetch_enabled=False so we
        # don't trip the constructor's alias check.
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:gone",
                            lookup_value_column="V",
                            lookup_key_column="K",
                        ),
                    ),
                ),
            },
            prefetch_enabled=False,
        )
        svc = MetadataService(cfg, {})
        with pytest.raises(ConfigurationError) as exc:
            svc.resolve(
                TriggerRecord(shortname="X", cif="123", system_id="1"),
                _make_document(),
                _make_mapping("BAC_X"),
            )
        assert exc.value.context.get("alias") == "gone"


# ---------------------------------------------------------------------------
# Type immutability
# ---------------------------------------------------------------------------


class TestTypeImmutability:
    def test_metadata_resolution_is_frozen(self, service: MetadataService) -> None:
        trigger = TriggerRecord(shortname="X", cif="123456", system_id="1")
        result = service.resolve(trigger, _make_document(index2="123456"), _make_mapping("CIF"))
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.healed_trigger = trigger  # type: ignore[misc]

    def test_metadata_config_is_frozen(self) -> None:
        cfg = MetadataConfig(field_aliases={}, field_sources={})
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.prefetch_enabled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Edge cases (coverage hardening for spec REQ-034 ≥95% target)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_csv_missing_lookup_key_col_at_prefetch_raises(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:clients",
                            lookup_value_column="Nombre_Cliente",
                            lookup_key_column=None,  # missing!
                        ),
                    ),
                ),
            },
        )
        with pytest.raises(ConfigurationError) as exc:
            MetadataService(cfg, sources_registry)
        assert exc.value.context.get("source_type") == "csv:clients"

    def test_csv_repeated_triple_loaded_once(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        # Two fields use the SAME (alias, key_col, value_col) → cache must
        # only iterate the source once (the dedup branch fires).
        wrapped = _CountingSource(sources_registry["clients"])
        registry = {**sources_registry, "clients": wrapped}
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_Name1": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:clients",
                            lookup_value_column="Nombre_Cliente",
                            lookup_key_column="CIF",
                        ),
                    ),
                ),
                "BAC_Name2": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:clients",
                            lookup_value_column="Nombre_Cliente",
                            lookup_key_column="CIF",
                        ),
                    ),
                ),
            },
        )
        MetadataService(cfg, registry)
        # Despite two fields referencing the same triple, source iterated once.
        assert wrapped.get_all_calls == 1

    def test_aliased_field_to_unconfigured_canonical_raises(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        # Alias maps "X" → "BAC_NOT_CONFIGURED", which has no field_sources entry.
        cfg = MetadataConfig(
            field_aliases={"X": "BAC_NOT_CONFIGURED"},
            field_sources={},
        )
        svc = MetadataService(cfg, sources_registry)
        with pytest.raises(ConfigurationError) as exc:
            svc.resolve(
                TriggerRecord(shortname="X", cif="123456", system_id="1"),
                _make_document(),
                _make_mapping("X"),
            )
        assert exc.value.context.get("field") == "BAC_NOT_CONFIGURED"

    def test_trigger_unknown_attribute_raises(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(source_type="trigger", lookup_value_column="bogus_attr"),
                    ),
                ),
            },
        )
        svc = MetadataService(cfg, sources_registry)
        with pytest.raises(ConfigurationError) as exc:
            svc.resolve(
                TriggerRecord(shortname="X", cif="123456", system_id="1"),
                _make_document(),
                _make_mapping("BAC_X"),
            )
        assert exc.value.context.get("attribute") == "bogus_attr"

    def test_rvabrep_unknown_attribute_raises(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(source_type="rvabrep", lookup_value_column="bogus_attr"),
                    ),
                ),
            },
        )
        svc = MetadataService(cfg, sources_registry)
        with pytest.raises(ConfigurationError) as exc:
            svc.resolve(
                TriggerRecord(shortname="X", cif="123456", system_id="1"),
                _make_document(),
                _make_mapping("BAC_X"),
            )
        assert exc.value.context.get("attribute") == "bogus_attr"

    def test_csv_lookup_with_no_trigger_cif_returns_none(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        # Field that ONLY has csv source + default. trigger.cif=None →
        # csv lookup returns None → falls through to default (no validation here).
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:clients",
                            lookup_value_column="Nombre_Cliente",
                            lookup_key_column="CIF",
                        ),
                    ),
                    default_value="UNKNOWN",
                ),
            },
        )
        svc = MetadataService(cfg, sources_registry)
        result = svc.resolve(
            TriggerRecord(shortname="X", cif=None, system_id="1"),
            _make_document(),
            _make_mapping("BAC_X"),
        )
        assert result.metadata["BAC_X"] == "UNKNOWN"

    def test_prefetch_disabled_csv_no_match_returns_none(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        # prefetch_enabled=False → goes through get_by_fields path.
        # CIF=999999 does not exist → empty result → falls through to default.
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:clients",
                            lookup_value_column="Nombre_Cliente",
                            lookup_key_column="CIF",
                        ),
                    ),
                    default_value="MISSING",
                ),
            },
            prefetch_enabled=False,
        )
        svc = MetadataService(cfg, sources_registry)
        result = svc.resolve(
            TriggerRecord(shortname="X", cif="999999", system_id="1"),
            _make_document(),
            _make_mapping("BAC_X"),
        )
        assert result.metadata["BAC_X"] == "MISSING"

    def test_prefetch_disabled_csv_match_returns_value(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        # prefetch_enabled=False + CIF that exists → get_by_fields returns hit.
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:clients",
                            lookup_value_column="Nombre_Cliente",
                            lookup_key_column="CIF",
                        ),
                    ),
                ),
            },
            prefetch_enabled=False,
        )
        svc = MetadataService(cfg, sources_registry)
        result = svc.resolve(
            TriggerRecord(shortname="X", cif="123456", system_id="1"),
            _make_document(),
            _make_mapping("BAC_X"),
        )
        assert result.metadata["BAC_X"] == "JUAN PEREZ TEST"

    def test_fetch_csv_at_resolution_with_disabled_prefetch_and_missing_key_col(
        self, sources_registry: dict[str, IDataSource]
    ) -> None:
        # prefetch_enabled=False bypasses constructor validation. The check now
        # fires inside _fetch_csv at resolution time.
        cfg = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_X": FieldSourceConfig(
                    sources=(
                        SourceConfig(
                            source_type="csv:clients",
                            lookup_value_column="Nombre_Cliente",
                            lookup_key_column=None,
                        ),
                    ),
                ),
            },
            prefetch_enabled=False,
        )
        svc = MetadataService(cfg, sources_registry)
        with pytest.raises(ConfigurationError) as exc:
            svc.resolve(
                TriggerRecord(shortname="X", cif="123456", system_id="1"),
                _make_document(),
                _make_mapping("BAC_X"),
            )
        assert exc.value.context.get("source_type") == "csv:clients"


# ---------------------------------------------------------------------------
# 038 — CMISPropertyId translation
# ---------------------------------------------------------------------------


class TestCmisPropertyIdTranslation:
    """``mapping.cmis_property_ids`` (038) rewrites resolution keys.

    Without a catalog, ``MetadataResolution.metadata`` keeps the canonical
    field names (``BAC_CIF`` etc.) — pre-038 behavior. With a catalog,
    each key whose friendly name is in the catalog is replaced by the
    catalogued CMIS property id. Keys not in the catalog pass through
    unchanged (partial catalogs are valid).
    """

    def test_none_catalog_keeps_canonical_keys(self, service: MetadataService) -> None:
        trigger = TriggerRecord(shortname="JUANPEREZ01", cif="123456", system_id="1")
        result = service.resolve(trigger, _make_document(), _make_mapping("CIF", "ShortName"))
        assert "BAC_CIF" in result.metadata
        assert "BAC_Shortname" in result.metadata
        assert "cmcourier:BAC_CIF" not in result.metadata

    def test_full_catalog_translates_every_key(self, service: MetadataService) -> None:
        trigger = TriggerRecord(shortname="JUANPEREZ01", cif="123456", system_id="1")
        catalog = {
            "CIF": "cmcourier:BAC_CIF",
            "ShortName": "cmcourier:Short_Name",
        }
        mapping = _make_mapping_with_catalog("CIF", "ShortName", catalog=catalog)
        result = service.resolve(trigger, _make_document(), mapping)
        assert result.metadata["cmcourier:BAC_CIF"] == "123456"
        assert result.metadata["cmcourier:Short_Name"] == "JUANPEREZ01"
        assert "BAC_CIF" not in result.metadata
        assert "BAC_Shortname" not in result.metadata

    def test_partial_catalog_keeps_uncatalogued_canonical(self, service: MetadataService) -> None:
        trigger = TriggerRecord(shortname="JUANPEREZ01", cif="123456", system_id="1")
        catalog = {"CIF": "cmcourier:BAC_CIF"}  # ShortName intentionally not in catalog
        mapping = _make_mapping_with_catalog("CIF", "ShortName", catalog=catalog)
        result = service.resolve(trigger, _make_document(), mapping)
        assert result.metadata["cmcourier:BAC_CIF"] == "123456"
        assert result.metadata["BAC_Shortname"] == "JUANPEREZ01"

    def test_empty_catalog_keeps_canonical(self, service: MetadataService) -> None:
        trigger = TriggerRecord(shortname="JUANPEREZ01", cif="123456", system_id="1")
        mapping = _make_mapping_with_catalog("CIF", catalog={})
        result = service.resolve(trigger, _make_document(), mapping)
        # Empty catalog is treated as "no catalog" (falsy) — keys stay canonical.
        assert "BAC_CIF" in result.metadata
