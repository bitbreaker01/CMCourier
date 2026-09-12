"""150 REQ-001/005: ``EligibilityConfigModel`` → el servicio del dominio.

El wiring es la única traducción entre el schema Pydantic y las dataclasses
del servicio (Principio I): ni el servicio ni el orchestrator conocen
Pydantic.

La garantía que se testea acá: **con la perilla apagada el builder devuelve
``None``**, así que el orchestrator no tiene a quién preguntarle y la fuente
NO SE TOCA. Es lo que vuelve literal el "byte-equivalente a que el bloque no
exista" de REQ-001.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from cmcourier.config.schema import (
    CsvMetadataSourceConfig,
    EligibilityConfigModel,
    EligibilityMatchModel,
)
from cmcourier.config.wiring import build_eligibility_config, build_eligibility_service
from cmcourier.domain.ports import IDataSource
from cmcourier.services.eligibility import EligibilityConfig, EligibilityMatch
from cmcourier.services.metadata import MetadataConfig, MetadataService

pytestmark = pytest.mark.unit


class _SpySource(IDataSource):
    """Cuenta CUALQUIER lectura: con la perilla apagada tiene que quedar en 0."""

    def __init__(self) -> None:
        self.reads = 0

    def get_all(self) -> Iterator[dict[str, Any]]:
        self.reads += 1
        return iter(())

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.reads += 1
        return []

    def get_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        self.reads += 1
        return []

    def stream_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        self.reads += 1
        return iter(())

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def count(self) -> int:
        self.reads += 1
        return 0

    def close(self) -> None:
        return None


_MATCH_ANY = (
    EligibilityMatchModel(field="BAC_Shortname", column="Shortname"),
    EligibilityMatchModel(field="BAC_CIF", column="CIF"),
)


def _metadata_service(source: IDataSource) -> MetadataService:
    return MetadataService(
        MetadataConfig(field_aliases={}, field_sources={}, prefetch_enabled=False),
        {"clientes_activos": source},
    )


class TestBuildEligibilityConfig150:
    def test_bloque_apagado_da_la_config_apagada(self) -> None:
        assert build_eligibility_config(EligibilityConfigModel(), ()) == EligibilityConfig()

    def test_traduce_source_y_match_any(self, tmp_path: Path) -> None:
        lista = tmp_path / "clientes-activos.csv"
        lista.write_text("Shortname,CIF\nACMESA01,1\n")
        model = EligibilityConfigModel(
            enabled=True, source="csv:clientes_activos", match_any=_MATCH_ANY
        )
        sources = (CsvMetadataSourceConfig(alias="clientes_activos", csv_path=lista),)
        assert build_eligibility_config(model, sources) == EligibilityConfig(
            enabled=True,
            source="csv:clientes_activos",
            match_any=(
                EligibilityMatch(field="BAC_Shortname", column="Shortname"),
                EligibilityMatch(field="BAC_CIF", column="CIF"),
            ),
            source_path=str(lista),
        )

    def test_la_ruta_de_la_fuente_viaja_para_la_auditoria(self, tmp_path: Path) -> None:
        """REQ-005: sin la ruta, el censo no puede decir "activo según qué
        lista"."""
        lista = tmp_path / "activos.csv"
        lista.write_text("Shortname,CIF\nACMESA01,1\n")
        model = EligibilityConfigModel(
            enabled=True, source="csv:clientes_activos", match_any=_MATCH_ANY
        )
        sources = (CsvMetadataSourceConfig(alias="clientes_activos", csv_path=lista),)
        assert build_eligibility_config(model, sources).source_path == str(lista)


class TestBuildEligibilityService150:
    def test_perilla_apagada_no_construye_servicio_ni_toca_la_fuente(self) -> None:
        spy = _SpySource()
        service = build_eligibility_service(EligibilityConfigModel(), (), _metadata_service(spy))
        assert service is None
        assert spy.reads == 0

    def test_perilla_prendida_construye_el_servicio(self, tmp_path: Path) -> None:
        lista = tmp_path / "activos.csv"
        lista.write_text("Shortname,CIF\nACMESA01,1\n")
        spy = _SpySource()
        model = EligibilityConfigModel(
            enabled=True, source="csv:clientes_activos", match_any=_MATCH_ANY
        )
        sources = (CsvMetadataSourceConfig(alias="clientes_activos", csv_path=lista),)
        service = build_eligibility_service(model, sources, _metadata_service(spy))
        assert service is not None
        assert service.enabled is True
        # Construir no consulta: la lista recién se toca en el preflight.
        assert spy.reads == 0
