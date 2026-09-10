"""147 REQ-003 + REQ-004: la identidad se resuelve al INICIO de S2.

S2 deja de ser sólo "buscá el mapeo": primero resuelve la identidad del
cliente con el motor de cadenas (que puede ir a la red: hijo → padre →
shortname → CIF) y recién después pide el mapeo. La
:class:`~cmcourier.services.identity.ResolvedIdentity` se cuelga del
``_StageItem`` y viaja con el documento hasta las tres escrituras.

Dos garantías que se testean acá porque son el punto del cambio:

* **S3 no repite el trabajo.** Lo que S2 resolvió entra como semilla del
  dict de resolución de metadata; la cuenta de ``get_by_fields`` del fake
  no se mueve.
* **Un slot no declarado se comporta EXACTAMENTE como antes.** La clave
  del mapeo sigue saliendo de ``trigger_system_id`` y el registro de
  tracking del ``audit_row()``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.domain.cm_types import CmTypeEntry, CmTypeManifest
from cmcourier.domain.models import (
    CMMapping,
    RVABREPDocument,
    RvabrepRowTrigger,
    StageStatus,
    Trigger,
)
from cmcourier.domain.ports import IDataSource
from cmcourier.orchestrators.staged import StagedPipeline, _StageItem
from cmcourier.services.identity import IdentityConfig, IdentityResolver, IdentitySlotConfig
from cmcourier.services.mapping import MappingService
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSource(IDataSource):
    """``IDataSource`` en memoria que cuenta cada ``get_by_fields``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.get_by_fields_calls = 0

    def get_all(self) -> Iterator[dict[str, Any]]:
        yield from self.rows

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.get_by_fields_calls += 1
        return [row for row in self.rows if all(row.get(k) == v for k, v in filters.items())]

    def get_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get(field) in values]

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def count(self) -> int:
        return len(self.rows)

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _document(**overrides: object) -> RVABREPDocument:
    defaults: dict[str, object] = {
        "system_code": "1",
        "txn_num": "TXN_ID",
        "index1": "HIJO-1",
        "index2": "",
        "index3": "",
        "index4": "",
        "index5": "",
        "index6": "",
        "index7": "0001",
        "image_type": "O",
        "image_path": "x",
        "file_name": "DOC.pdf",
        "creation_date": datetime(2026, 1, 1),  # noqa: DTZ001
        "last_view_date": None,
        "total_pages": 1,
        "delete_code": "",
    }
    defaults.update(overrides)
    return RVABREPDocument(**defaults)  # type: ignore[arg-type]


def _entry(id_corto: str) -> CmTypeEntry:
    return CmTypeEntry(
        id_corto=id_corto,
        type_id=f"$t!-2_BAC_{id_corto}v-1",
        local_name=f"BAC_{id_corto}",
        display_name=f"{id_corto} - Clase",
        folder=f"/$type/BAC_{id_corto}",
    )


def _mapping_service() -> MappingService:
    """``("", "0001") → DC01`` (comodín) y ``("RVI2", "0001") → DC02``."""
    manifest = CmTypeManifest(
        service_url="http://cm.test/browser",
        repository_id="repo",
        discovered_at="2026-01-01T00:00:00Z",
        types={e.id_corto: e for e in (_entry("DC01"), _entry("DC02"))},
    )
    rows: list[dict[str, Any]] = [
        {"IDSistema": "", "IDRVI": "0001", "IDCM": "DC01"},
        {"IDSistema": "RVI2", "IDRVI": "0001", "IDCM": "DC02"},
    ]
    return MappingService(_FakeSource(rows), type_manifest=manifest)


def _metadata_config() -> MetadataConfig:
    """La cadena de tres saltos más un campo de sistema por lookup."""
    return MetadataConfig(
        field_aliases={},
        field_sources={
            "BAC_Afiliado_Hijo": FieldSourceConfig(
                sources=(SourceConfig(source_type="rvabrep", lookup_value_column="index1"),),
            ),
            "BAC_Afiliado_Padre": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="as400:afiliados",
                        lookup_value_column="AFIPAD",
                        lookup_key_column="AFIHIJ",
                        lookup_value_source="field.BAC_Afiliado_Hijo",
                    ),
                ),
            ),
            "BAC_Shortname": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="as400:clientes",
                        lookup_value_column="CUSSHN",
                        lookup_key_column="CUSAFI",
                        lookup_value_source="field.BAC_Afiliado_Padre",
                    ),
                ),
            ),
            "BAC_CIF": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="as400:clientes",
                        lookup_value_column="CUSCIF",
                        lookup_key_column="CUSSHN",
                        lookup_value_source="field.BAC_Shortname",
                    ),
                ),
            ),
            "BAC_Sistema": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="as400:clientes",
                        lookup_value_column="CUSSIS",
                        lookup_key_column="CUSSHN",
                        lookup_value_source="field.BAC_Shortname",
                    ),
                ),
            ),
        },
        prefetch_enabled=False,
    )


@pytest.fixture
def clientes() -> _FakeSource:
    return _FakeSource(
        [{"CUSAFI": "PADRE-7", "CUSSHN": "ACMESA01", "CUSCIF": "123456789", "CUSSIS": "RVI2"}]
    )


@pytest.fixture
def afiliados() -> _FakeSource:
    return _FakeSource([{"AFIHIJ": "HIJO-1", "AFIPAD": "PADRE-7"}])


@pytest.fixture
def metadata_service(clientes: _FakeSource, afiliados: _FakeSource) -> MetadataService:
    return MetadataService(_metadata_config(), {"clientes": clientes, "afiliados": afiliados})


def _identity_config(**slots: IdentitySlotConfig | None) -> IdentityConfig:
    base: dict[str, IdentitySlotConfig | None] = {
        "shortname": IdentitySlotConfig(field="BAC_Shortname"),
        "cif": IdentitySlotConfig(field="BAC_CIF", max_digits=9),
        "system_id": None,
    }
    base.update(slots)
    return IdentityConfig(**base)  # type: ignore[arg-type]


def _pipeline(
    metadata_service: MetadataService,
    identity: IdentityConfig | None = None,
    *,
    mapping_service: object | None = None,
    stage_done: bool = True,
) -> StagedPipeline:
    tracking = MagicMock()
    tracking.is_stage_done.return_value = stage_done
    return StagedPipeline(
        trigger_strategy=MagicMock(),
        indexing_service=MagicMock(),
        mapping_service=mapping_service or _mapping_service(),  # type: ignore[arg-type]
        metadata_service=metadata_service,
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=tracking,
        workers=1,
        identity_resolver=(
            IdentityResolver(identity, metadata_service) if identity is not None else None
        ),
    )


def _trigger(**row: str) -> Trigger:
    """El trigger de PRODUCCIÓN: fila RVABREP inmutable (146/046)."""
    base = {"ABABCD": "", "ABACCD": "", "ABAACD": ""}
    base.update(row)
    return RvabrepRowTrigger(row=base)


def _run_s2(pipeline: StagedPipeline, item: _StageItem) -> tuple[_StageItem | None, bool]:
    return pipeline._s2_one(item, "B1", pipeline._metrics)


# ---------------------------------------------------------------------------
# REQ-003 — la identidad se resuelve en S2 y viaja con el item
# ---------------------------------------------------------------------------


class TestIdentidadEnS2:
    def test_la_identidad_se_cuelga_del_item(self, metadata_service: MetadataService) -> None:
        pipeline = _pipeline(metadata_service, _identity_config())
        item = _StageItem(trigger=_trigger(), document=_document())
        survivor, counted = _run_s2(pipeline, item)
        assert survivor is not None
        assert counted is False
        assert survivor.identity is not None
        # La fila RVABREP no trae ni shortname ni CIF: los tres saltos los dieron.
        assert survivor.identity.shortname == "ACMESA01"
        assert survivor.identity.cif == "123456789"

    def test_sin_resolver_no_hay_identidad_y_el_mapeo_no_cambia(
        self, metadata_service: MetadataService
    ) -> None:
        pipeline = _pipeline(metadata_service, None)
        item = _StageItem(trigger=_trigger(ABAACD="RVI2"), document=_document())
        survivor, _ = _run_s2(pipeline, item)
        assert survivor is not None
        assert survivor.identity is None
        assert survivor.mapping is not None
        assert survivor.mapping.id_corto == "DC02"


class TestClaveDelMapeo:
    def test_con_slot_declarado_la_clave_sale_de_la_identidad(
        self, metadata_service: MetadataService
    ) -> None:
        # La fila RVABREP NO trae sistema; la cadena lo resuelve en "RVI2".
        identity = _identity_config(system_id=IdentitySlotConfig(field="BAC_Sistema"))
        pipeline = _pipeline(metadata_service, identity)
        item = _StageItem(trigger=_trigger(), document=_document())
        survivor, _ = _run_s2(pipeline, item)
        assert survivor is not None
        assert survivor.mapping is not None
        assert survivor.mapping.id_corto == "DC02"

    def test_sin_slot_declarado_la_clave_sigue_saliendo_del_trigger(
        self, metadata_service: MetadataService
    ) -> None:
        # `system_id` sin declarar: aunque la cadena podría resolver "RVI2",
        # la clave sale del trigger, que viene vacío → comodín DC01.
        pipeline = _pipeline(metadata_service, _identity_config())
        item = _StageItem(trigger=_trigger(), document=_document())
        survivor, _ = _run_s2(pipeline, item)
        assert survivor is not None
        assert survivor.mapping is not None
        assert survivor.mapping.id_corto == "DC01"


class TestFallaDeIdentidad:
    def test_falla_el_documento_como_s2_failed_con_el_motivo(
        self, metadata_service: MetadataService
    ) -> None:
        # Un hijo que no está en la tabla de afiliados corta la cadena entera.
        identity = _identity_config()
        pipeline = _pipeline(metadata_service, identity, stage_done=False)
        item = _StageItem(trigger=_trigger(), document=_document(index1="NO_EXISTE"))
        survivor, counted = _run_s2(pipeline, item)
        assert survivor is None
        assert counted is True
        tracking = pipeline._tracking_store
        tracking.mark_stage_failed.assert_called_once()  # type: ignore[attr-defined]
        args = tracking.mark_stage_failed.call_args.args  # type: ignore[attr-defined]
        assert args[2] is StageStatus.S2_FAILED
        assert "identity.shortname" in args[3]
        # La cadena completa que se intentó es el punto del mensaje.
        assert "BAC_Afiliado_Padre" in args[3]

    def test_no_se_pide_el_mapeo_si_la_identidad_no_resolvio(
        self, metadata_service: MetadataService
    ) -> None:
        mapping_service = MagicMock()
        pipeline = _pipeline(
            metadata_service,
            _identity_config(),
            mapping_service=mapping_service,
            stage_done=False,
        )
        item = _StageItem(trigger=_trigger(), document=_document(index1="NO_EXISTE"))
        _run_s2(pipeline, item)
        mapping_service.get_mapping.assert_not_called()


class TestS3NoRepiteElTrabajo:
    def test_un_campo_resuelto_en_s2_cuesta_cero_consultas_en_s3(
        self,
        metadata_service: MetadataService,
        clientes: _FakeSource,
        afiliados: _FakeSource,
    ) -> None:
        mapping = CMMapping(
            clase_id="C",
            id_rvi="0001",
            id_corto="DC01",
            clase_name="Clase",
            required_metadata_fields=("BAC_CIF", "BAC_Shortname"),
        )
        mapping_service = MagicMock()
        mapping_service.get_mapping.return_value = mapping
        pipeline = _pipeline(metadata_service, _identity_config(), mapping_service=mapping_service)
        item = _StageItem(trigger=_trigger(), document=_document())
        survivor, _ = _run_s2(pipeline, item)
        assert survivor is not None
        calls_after_s2 = (clientes.get_by_fields_calls, afiliados.get_by_fields_calls)
        assert calls_after_s2 == (2, 1), "S2 debería dar los tres saltos"

        survivor, counted = pipeline._s3_one(survivor, "B1", pipeline._metrics)

        assert survivor is not None
        assert counted is False
        assert (clientes.get_by_fields_calls, afiliados.get_by_fields_calls) == calls_after_s2
        assert survivor.metadata is not None
        assert survivor.metadata["BAC_CIF"] == "123456789"


# ---------------------------------------------------------------------------
# REQ-004 — un solo origen para el registro de tracking
# ---------------------------------------------------------------------------


class TestRegistroDeTracking:
    def test_el_registro_sale_de_la_identidad_resuelta(
        self, metadata_service: MetadataService
    ) -> None:
        pipeline = _pipeline(metadata_service, _identity_config())
        item = _StageItem(trigger=_trigger(), document=_document())
        survivor, _ = _run_s2(pipeline, item)
        assert survivor is not None
        record = pipeline._build_record(survivor, "B1", StageStatus.S5_PENDING)
        assert record.trigger_shortname == "ACMESA01"
        assert record.trigger_cif == "123456789"

    def test_sin_identidad_el_registro_sigue_saliendo_del_audit_row(
        self, metadata_service: MetadataService
    ) -> None:
        pipeline = _pipeline(metadata_service, None)
        item = _StageItem(
            trigger=_trigger(ABABCD="CRUDOSA01", ABACCD="777", ABAACD="9"),
            document=_document(),
        )
        record = pipeline._build_record(item, "B1", StageStatus.S1_PENDING)
        assert record.trigger_shortname == "CRUDOSA01"
        assert record.trigger_cif == "777"
        assert record.trigger_system_id == "9"
