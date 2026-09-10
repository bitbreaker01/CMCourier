"""084: ``FieldSourceItem.lookup_value_source`` permite especificar
de dónde sale el valor a buscar en CSV/AS400 lookup sources."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cmcourier.config.schema import FieldSourceItem

pytestmark = pytest.mark.unit


class TestDefault:
    def test_default_is_trigger_cif(self) -> None:
        item = FieldSourceItem(source_type="csv:clients", lookup_value_column="Nombre")
        assert item.lookup_value_source == "trigger.cif"


class TestAcceptedScopes:
    @pytest.mark.parametrize(
        "spec",
        [
            "trigger.cif",
            "trigger.shortname",
            "trigger.system_id",
            "rvabrep.txn_num",
            "rvabrep.index1",
            "rvabrep.index7",
            "rvabrep.image_type",
        ],
    )
    def test_known_scopes_accepted(self, spec: str) -> None:
        item = FieldSourceItem(
            source_type="csv:clients",
            lookup_value_column="Nombre",
            lookup_value_source=spec,
        )
        assert item.lookup_value_source == spec


class TestScopeField:
    """147 REQ-001: tercer scope ``field.<CANONICAL_NAME>`` — la clave de
    búsqueda es el valor YA RESUELTO de otro campo."""

    @pytest.mark.parametrize(
        "spec",
        ["field.BAC_CIF", "field.BAC_Shortname", "field.BAC_Afiliado_Padre"],
    )
    def test_field_scope_accepted(self, spec: str) -> None:
        item = FieldSourceItem(
            source_type="as400:clientes",
            lookup_value_column="CUSSHN",
            lookup_key_column="CUSAFI",
            lookup_value_source=spec,
        )
        assert item.lookup_value_source == spec

    def test_field_scope_still_requires_the_dotted_shape(self) -> None:
        with pytest.raises(ValidationError) as exc:
            FieldSourceItem(
                source_type="as400:clientes",
                lookup_value_column="CUSSHN",
                lookup_value_source="field",
            )
        assert "<scope>.<attr>" in str(exc.value)

    def test_error_text_lists_field_as_a_valid_scope(self) -> None:
        with pytest.raises(ValidationError) as exc:
            FieldSourceItem(
                source_type="csv:clients",
                lookup_value_column="Nombre",
                lookup_value_source="campo.BAC_CIF",
            )
        assert "field" in str(exc.value)


class TestRejected:
    def test_missing_dot_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            FieldSourceItem(
                source_type="csv:clients",
                lookup_value_column="Nombre",
                lookup_value_source="cif",
            )
        assert "<scope>.<attr>" in str(exc.value)

    def test_unknown_scope_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            FieldSourceItem(
                source_type="csv:clients",
                lookup_value_column="Nombre",
                lookup_value_source="trigger2.cif",
            )
        assert "scope" in str(exc.value)
