"""083: ``MetadataService`` resuelve campos cuyo ``FieldSourceConfig``
tiene ``sources=()`` cayendo directo al ``default_value``.

El motor ya soportaba esto (el loop ``for sc in fsc.sources`` no
itera con sources vacíos y cae al check del default), pero hasta
083 el schema no lo permitía. Estos tests verifican el path desde
el dataclass público ``FieldSourceConfig`` directo.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from cmcourier.domain.exceptions import SourceFailedError
from cmcourier.domain.models import CMMapping, RVABREPDocument, TriggerRecord
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
    ValidationConfig,
)

pytestmark = pytest.mark.unit


def _trigger(cif: str | None = "123456") -> TriggerRecord:
    return TriggerRecord(shortname="SHORT", cif=cif, system_id="SYS")


def _document(**overrides: Any) -> RVABREPDocument:
    defaults: dict[str, Any] = {
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


class TestDefaultOnlyResolution:
    def test_resolves_to_default_when_sources_empty(self) -> None:
        config = MetadataConfig(
            field_aliases={"TIPO": "BAC_TIPO"},
            field_sources={
                "BAC_TIPO": FieldSourceConfig(sources=(), default_value="FAC"),
            },
            prefetch_enabled=False,
        )
        service = MetadataService(config, sources_registry={})
        result = service.resolve(_trigger(), _document(), _mapping("BAC_TIPO"))
        assert result.metadata.properties["BAC_TIPO"] == "FAC"

    def test_default_only_applies_per_document(self) -> None:
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_BANCO": FieldSourceConfig(sources=(), default_value="BAC"),
            },
            prefetch_enabled=False,
        )
        service = MetadataService(config, sources_registry={})
        for cif in ("100000", "200000"):
            result = service.resolve(_trigger(cif), _document(index2=cif), _mapping("BAC_BANCO"))
            assert result.metadata.properties["BAC_BANCO"] == "BAC"

    def test_default_only_no_validation_applied(self) -> None:
        # Sin sources no hay `first_validation` — el default pasa tal cual.
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_TIPO": FieldSourceConfig(sources=(), default_value="anything-goes"),
            },
            prefetch_enabled=False,
        )
        service = MetadataService(config, sources_registry={})
        result = service.resolve(_trigger(), _document(), _mapping("BAC_TIPO"))
        assert result.metadata.properties["BAC_TIPO"] == "anything-goes"


class TestPrefetchSkipsEmptySources:
    def test_no_crash_with_field_having_only_default(self) -> None:
        # Prefetch itera sobre ``fsc.sources`` — con sources vacíos no
        # debe explotar ni intentar resolver aliases inexistentes.
        config = MetadataConfig(
            field_aliases={},
            field_sources={
                "BAC_CONST": FieldSourceConfig(sources=(), default_value="K"),
            },
            prefetch_enabled=True,
        )
        service = MetadataService(config, sources_registry={})
        result = service.resolve(_trigger(), _document(), _mapping("BAC_CONST"))
        assert result.metadata.properties["BAC_CONST"] == "K"


class TestStillRaisesWhenSourcesFailAndNoDefault:
    def test_no_default_at_all_still_raises_source_failed(self) -> None:
        # 149 no toca este camino: sin `default_value` no hay nada que
        # devolver y el documento tiene que fallar.
        config = MetadataConfig(
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
                    default_value=None,
                ),
            },
            prefetch_enabled=False,
        )
        service = MetadataService(config, sources_registry={})
        with pytest.raises(SourceFailedError) as exc:
            service.resolve(_trigger(cif=None), _document(), _mapping("BAC_X"))
        assert exc.value.field_name == "BAC_X"


class TestElDefaultNoSeValida149:
    """149 REQ-001: el default deja de validarse contra la PRIMERA fuente."""

    def test_invalid_default_with_first_source_validation_is_returned(self) -> None:
        config = MetadataConfig(
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
                    default_value="notdigits",
                ),
            },
            prefetch_enabled=False,
        )
        service = MetadataService(config, sources_registry={})
        # El trigger sin CIF hace que el source no devuelva valor; el motor
        # cae al default. Pre-149 "notdigits" moría contra ^\d{6}$ —una
        # validación que nadie escribió en el YAML y que dependía de en qué
        # POSICIÓN quedó esa fuente—; ahora se devuelve tal cual y la red
        # de seguridad vive en `types check`.
        result = service.resolve(_trigger(cif=None), _document(), _mapping("BAC_X"))
        assert result.metadata.properties["BAC_X"] == "notdigits"
