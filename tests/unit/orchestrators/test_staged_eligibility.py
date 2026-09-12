"""150 REQ-003/004: la elegibilidad se evalúa en S2, después de la identidad.

Para saber si el cliente está activo hay que saber primero quién es el
cliente: el chequeo va **inmediatamente después de resolver la identidad**
(147) y **antes de ``get_mapping``**.

Tres garantías se testean acá porque son el contrato del spec:

* **La perilla apagada no cambia nada.** Sin servicio cableado, S2 se
  comporta byte-idéntico al pre-150 y la lista no se abre ni una vez.
* **La precedencia de REQ-004.** Un documento cuya identidad no resuelve
  NUNCA llega a evaluarse contra la lista: sale ``IDENTITY_UNRESOLVED``
  (BLOQUEADO), no ``CLIENT_NOT_ACTIVE``. Es correcto — no sabemos si su
  cliente está activo porque no sabemos quién es.
* **El documento se registra y no avanza.** ``CLIENT_NOT_ACTIVE`` en el
  balde ``EXCLUIDO``, con fila terminal (no cuenta como falla) y sin
  ``mapping``, así que no llega a S3.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from cmcourier.domain.exceptions import EligibilityListError
from cmcourier.domain.models import (
    ReasonBucket,
    ReasonCode,
    RVABREPDocument,
    RvabrepRowTrigger,
    StageStatus,
    Trigger,
)
from cmcourier.domain.ports import IDataSource
from cmcourier.orchestrators.staged import StagedPipeline, _StageItem
from cmcourier.services.eligibility import (
    EligibilityConfig,
    EligibilityMatch,
    EligibilityService,
)
from cmcourier.services.identity import IdentityConfig, IdentityResolver, IdentitySlotConfig
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
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.reads = 0

    def get_all(self) -> Iterator[dict[str, Any]]:
        self.reads += 1
        yield from self.rows

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.reads += 1
        return [row for row in self.rows if all(row.get(k) == v for k, v in filters.items())]

    def get_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get(field) in values]

    def stream_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        yield from self.get_by_fields_in(field, values, fixed_filters)

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def count(self) -> int:
        self.reads += 1
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
        "index1": "",
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


def _trigger(**row: str) -> Trigger:
    base = {"ABABCD": "", "ABACCD": "", "ABAACD": ""}
    base.update(row)
    return RvabrepRowTrigger(row=base)


def _metadata_config() -> MetadataConfig:
    """``BAC_Shortname`` sale del trigger; ``BAC_CIF``, de la tabla de clientes."""
    return MetadataConfig(
        field_aliases={},
        field_sources={
            "BAC_Shortname": FieldSourceConfig(
                sources=(SourceConfig(source_type="trigger", lookup_value_column="shortname"),),
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
        },
        prefetch_enabled=False,
    )


@pytest.fixture
def activos() -> _FakeSource:
    return _FakeSource([{"Shortname": "ACMESA01", "CIF": "123456789"}])


@pytest.fixture
def clientes() -> _FakeSource:
    return _FakeSource([{"CUSSHN": "ACMESA01", "CUSCIF": "123456789"}])


@pytest.fixture
def metadata_service(activos: _FakeSource, clientes: _FakeSource) -> MetadataService:
    return MetadataService(_metadata_config(), {"clientes_activos": activos, "clientes": clientes})


def _eligibility(metadata_service: MetadataService) -> EligibilityService:
    return EligibilityService(
        EligibilityConfig(
            enabled=True,
            source="csv:clientes_activos",
            match_any=(
                EligibilityMatch(field="BAC_Shortname", column="Shortname"),
                EligibilityMatch(field="BAC_CIF", column="CIF"),
            ),
        ),
        metadata_service,
    )


def _pipeline(
    metadata_service: MetadataService,
    *,
    eligibility: EligibilityService | None,
    identity: IdentityConfig | None = None,
    mapping_service: object | None = None,
    stage_done: bool = True,
) -> StagedPipeline:
    tracking = MagicMock()
    tracking.is_stage_done.return_value = stage_done
    mapping = mapping_service or MagicMock()
    if mapping_service is None:
        mapping.missing_from_manifest = None
    return StagedPipeline(
        trigger_strategy=MagicMock(),
        indexing_service=MagicMock(),
        mapping_service=mapping,  # type: ignore[arg-type]
        metadata_service=metadata_service,
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=tracking,
        workers=1,
        identity_resolver=(
            IdentityResolver(
                identity
                if identity is not None
                else IdentityConfig(
                    shortname=IdentitySlotConfig(field="BAC_Shortname", on_missing="warn"),
                    cif=IdentitySlotConfig(field="BAC_CIF", on_missing="warn"),
                ),
                metadata_service,
            )
        ),
        eligibility_service=eligibility,
    )


def _run_s2(pipeline: StagedPipeline, item: _StageItem) -> tuple[_StageItem | None, bool]:
    return pipeline._s2_one(item, "B1", pipeline._metrics)


def _terminal_calls(pipeline: StagedPipeline) -> list[Any]:
    return pipeline._tracking_store.mark_stage_terminal.call_args_list  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# La perilla apagada
# ---------------------------------------------------------------------------


class TestPerillaApagada150:
    def test_sin_servicio_el_documento_pasa_y_la_lista_no_se_toca(
        self, metadata_service: MetadataService, activos: _FakeSource
    ) -> None:
        pipeline = _pipeline(metadata_service, eligibility=None)
        item = _StageItem(trigger=_trigger(ABABCD="NOESTA01"), document=_document())
        survivor, counted = _run_s2(pipeline, item)
        assert survivor is not None
        assert counted is False
        assert activos.reads == 0
        assert _terminal_calls(pipeline) == []


# ---------------------------------------------------------------------------
# REQ-004 — CLIENT_NOT_ACTIVE
# ---------------------------------------------------------------------------


class TestClienteInactivo150:
    def test_un_cliente_activo_sigue_camino(self, metadata_service: MetadataService) -> None:
        pipeline = _pipeline(metadata_service, eligibility=_eligibility(metadata_service))
        item = _StageItem(trigger=_trigger(ABABCD="ACMESA01"), document=_document())
        survivor, counted = _run_s2(pipeline, item)
        assert survivor is not None
        assert counted is False
        assert survivor.mapping is not None

    def test_un_cliente_inactivo_no_avanza_y_deja_su_razon(
        self, metadata_service: MetadataService
    ) -> None:
        pipeline = _pipeline(metadata_service, eligibility=_eligibility(metadata_service))
        item = _StageItem(trigger=_trigger(ABABCD="NOESTA01"), document=_document())
        survivor, counted = _run_s2(pipeline, item)
        assert survivor is None
        # No es una falla: es una decisión de negocio.
        assert counted is False
        call = _terminal_calls(pipeline)[0]
        assert call.args[0] == "TXN_ID"
        assert call.args[2] is StageStatus.S2_FAILED
        assert call.kwargs["reason_code"] is ReasonCode.CLIENT_NOT_ACTIVE

    def test_la_razon_vive_en_el_balde_excluido(self) -> None:
        """Decisión de negocio, no un error ni una falta de configuración."""
        assert ReasonCode.CLIENT_NOT_ACTIVE.bucket is ReasonBucket.EXCLUIDO

    def test_el_documento_no_llega_a_s3(self, metadata_service: MetadataService) -> None:
        pipeline = _pipeline(metadata_service, eligibility=_eligibility(metadata_service))
        item = _StageItem(trigger=_trigger(ABABCD="NOESTA01"), document=_document())
        survivor, _ = _run_s2(pipeline, item)
        assert survivor is None
        assert item.mapping is None

    def test_matchea_por_el_cif_cuando_el_shortname_no_esta_en_la_lista(
        self, metadata_service: MetadataService, activos: _FakeSource
    ) -> None:
        """``match_any``: el CIF llega después del salto contra ``clientes``."""
        activos.rows = [{"Shortname": "OTRO", "CIF": "123456789"}]
        pipeline = _pipeline(metadata_service, eligibility=_eligibility(metadata_service))
        item = _StageItem(trigger=_trigger(ABABCD="ACMESA01"), document=_document())
        survivor, _ = _run_s2(pipeline, item)
        assert survivor is not None


# ---------------------------------------------------------------------------
# REQ-003/004 — el orden dentro de S2
# ---------------------------------------------------------------------------


class TestOrdenDentroDeS2150:
    def test_identity_unresolved_le_gana_a_client_not_active(
        self, metadata_service: MetadataService, activos: _FakeSource
    ) -> None:
        """REQ-004 renglón 4: sin identidad no se pregunta por la lista — no
        sabemos si su cliente está activo porque no sabemos quién es."""
        identity = IdentityConfig(shortname=IdentitySlotConfig(field="BAC_Shortname"))
        pipeline = _pipeline(
            metadata_service,
            eligibility=_eligibility(metadata_service),
            identity=identity,
            stage_done=False,
        )
        item = _StageItem(trigger=_trigger(ABABCD=""), document=_document())
        survivor, counted = _run_s2(pipeline, item)
        assert survivor is None
        assert counted is True
        failed = pipeline._tracking_store.mark_stage_failed  # type: ignore[attr-defined]
        assert failed.call_args.kwargs["reason_code"] is ReasonCode.IDENTITY_UNRESOLVED
        assert activos.reads == 0

    def test_la_elegibilidad_corre_antes_de_get_mapping(
        self, metadata_service: MetadataService
    ) -> None:
        """REQ-003: primero quién es el cliente, después el mapeo del código."""
        mapping = MagicMock()
        mapping.missing_from_manifest = None
        pipeline = _pipeline(
            metadata_service,
            eligibility=_eligibility(metadata_service),
            mapping_service=mapping,
        )
        item = _StageItem(trigger=_trigger(ABABCD="NOESTA01"), document=_document())
        survivor, _ = _run_s2(pipeline, item)
        assert survivor is None
        mapping.get_mapping.assert_not_called()


# ---------------------------------------------------------------------------
# REQ-002 — el preflight del pipeline
# ---------------------------------------------------------------------------


class TestPreflightDelPipeline150:
    def test_sin_servicio_el_preflight_es_no_op(
        self, metadata_service: MetadataService, activos: _FakeSource
    ) -> None:
        pipeline = _pipeline(metadata_service, eligibility=None)
        assert pipeline.preflight() is None
        assert activos.reads == 0

    def test_una_lista_sana_no_aborta(self, metadata_service: MetadataService) -> None:
        pipeline = _pipeline(metadata_service, eligibility=_eligibility(metadata_service))
        snapshot = pipeline.preflight()
        assert snapshot is not None
        assert snapshot.row_count == 1

    def test_una_lista_vacia_aborta_la_corrida(
        self, metadata_service: MetadataService, activos: _FakeSource
    ) -> None:
        activos.rows = []
        pipeline = _pipeline(metadata_service, eligibility=_eligibility(metadata_service))
        with pytest.raises(EligibilityListError, match="clientes_activos"):
            pipeline.preflight()
