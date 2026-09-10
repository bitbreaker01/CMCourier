"""`Fixtures` compartidos para los tests de integración del `pipeline` con
trigger CSV.

Arma el grafo de adapters de vida larga (Principio VI de la Constitución:
sin `mocks`) más una `factory` para armar un :class:`StagedPipeline`
contra un CSV de trigger por test. Cada test compone su escenario así:

1. Escribe su CSV de trigger bajo ``tmp_path``.
2. Llama a ``harness.build_pipeline(triggers_csv_path)`` para obtener un
   `pipeline` cableado contra ese CSV.
3. Registra los `stubs` de CMIS vía ``harness.register_cmis_for_docs(...)``.
4. Llama a ``pipeline.run(...)``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import respx

from cmcourier.adapters.assembly import AssemblerConfig, PdfAssembler
from cmcourier.adapters.sources import TabularDataSource
from cmcourier.adapters.tracking import SQLiteTrackingStore
from cmcourier.adapters.upload.cmis_uploader import CmisConfig, CmisUploader
from cmcourier.orchestrators.staged import StagedPipeline
from cmcourier.services.indexing import IndexingColumnsConfig, IndexingService
from cmcourier.services.mapping import MappingColumnsConfig, MappingService
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
)
from cmcourier.services.triggers.csv import CsvTriggerColumnsConfig, CsvTriggerStrategy

_TESTS_ROOT = Path(__file__).parent.parent.parent
_PIPELINE_FIXTURES = _TESTS_ROOT / "fixtures" / "pipeline"
_SERVICES_FIXTURES = _TESTS_ROOT / "fixtures" / "services"
_ASSEMBLY_FIXTURES = _TESTS_ROOT / "fixtures" / "assembly"

_CMIS_BASE_URL = "http://cmis.example.test:9080/opencmcmis/browser"
_CMIS_REPO_ID = "$x!testrepo"


@dataclass
class PipelineHarness:
    """Conjunto de adapters de vida larga + `factory` de `pipeline` por test."""

    build_pipeline: Callable[..., StagedPipeline]
    tracking_store: SQLiteTrackingStore
    register_cmis_for_docs: Callable[..., None]
    db_path: Path
    _opened_sources: list[TabularDataSource] = field(default_factory=list)

    def close(self) -> None:
        for src in self._opened_sources:
            src.close()
        self.tracking_store.close()


def _friendly_indexing_config() -> IndexingColumnsConfig:
    return IndexingColumnsConfig(
        shortname_column="shortname",
        system_id_column="system_id",
        delete_code_column="delete_code",
        txn_num_column="txn_num",
        index2_column="index2",
        index3_column="index3",
        index4_column="index4",
        index5_column="index5",
        index6_column="index6",
        index7_column="index7",
        image_type_column="image_type",
        image_path_column="image_path",
        file_name_column="file_name",
        creation_date_column="creation_date",
        last_view_date_column="last_view_date",
        total_pages_column="total_pages",
    )


def _build_metadata_config() -> MetadataConfig:
    """Resuelve BAC_CIF (trigger → fallback a rvabrep.index2) + BAC_Nombre_Cliente."""
    return MetadataConfig(
        field_aliases={"CIF": "BAC_CIF", "Nombre_Cliente": "BAC_Nombre_Cliente"},
        field_sources={
            "BAC_CIF": FieldSourceConfig(
                sources=(
                    SourceConfig(source_type="trigger", lookup_value_column="cif"),
                    SourceConfig(source_type="rvabrep", lookup_value_column="index2"),
                ),
            ),
            "BAC_Nombre_Cliente": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="csv:clients",
                        lookup_value_column="Nombre_Cliente",
                        lookup_key_column="CIF",
                        # 147 REQ-001: la dependencia se DECLARA. Pre-147 esto
                        # decía `trigger.cif` (el default) y andaba sólo por el
                        # self-healing hardcodeado de `BAC_CIF`, que ya no está.
                        lookup_value_source="field.BAC_CIF",
                    ),
                ),
            ),
        },
    )


@pytest.fixture
def pipeline_harness(tmp_path: Path) -> Iterator[PipelineHarness]:
    """Cablea los adapters de vida larga; expone una `factory` para `pipelines` por test."""
    modelo_src = TabularDataSource(_SERVICES_FIXTURES / "modelo_documental.csv")
    rvabrep_src = TabularDataSource(_PIPELINE_FIXTURES / "rvabrep.csv")
    clients_src = TabularDataSource(_SERVICES_FIXTURES / "metadata" / "clients.csv")
    opened: list[TabularDataSource] = [modelo_src, rvabrep_src, clients_src]

    indexing_service = IndexingService(rvabrep_src, _friendly_indexing_config())
    mapping_service = MappingService(modelo_src, MappingColumnsConfig())
    metadata_service = MetadataService(
        config=_build_metadata_config(),
        sources_registry={"clients": clients_src},
    )
    assembler = PdfAssembler(
        AssemblerConfig(source_root=_ASSEMBLY_FIXTURES, temp_dir=tmp_path / "staging")
    )
    uploader_config = CmisConfig(
        base_url=_CMIS_BASE_URL,
        repo_id=_CMIS_REPO_ID,
        username="tester",
        password="secret-not-real",
        timeout_seconds=5.0,
        verify_ssl=False,
        max_bandwidth_mbps=0.0,
        retry_max_attempts=2,
        retry_base_delay_s=0.0,
    )
    uploader = CmisUploader(uploader_config)
    tracking_store = SQLiteTrackingStore(tmp_path / "tracking.db")

    def _build_pipeline(
        triggers_csv: Path,
        *,
        prep_workers: int = 1,
        s4_process_pool: object | None = None,
    ) -> StagedPipeline:
        trigger_src = TabularDataSource(triggers_csv)
        opened.append(trigger_src)
        trigger_strategy = CsvTriggerStrategy(trigger_src, CsvTriggerColumnsConfig())
        return StagedPipeline(
            trigger_strategy=trigger_strategy,
            indexing_service=indexing_service,
            mapping_service=mapping_service,
            metadata_service=metadata_service,
            assembler=assembler,
            uploader=uploader,
            tracking_store=tracking_store,
            prep_workers=prep_workers,
            s4_process_pool=s4_process_pool,  # type: ignore[arg-type]
        )

    def _register_cmis_for_docs(txn_nums: list[str], object_id_prefix: str = "cm-id-") -> None:
        """Pre-`stubea` el `warmup` + creación de carpetas + respuestas de
        `upload` por documento.

        060: migrado de ``responses`` a ``respx`` (httpx-native). Los tests
        tienen que decorarse con ``@respx.mock`` para que las rutas se
        resuelvan contra el `mock router` activo.
        """
        respx.get(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "repositoryId": _CMIS_REPO_ID,
                    "productName": "IBM Content Manager",
                    "productVersion": "8.7",
                    "vendorName": "IBM",
                },
            )
        )
        respx.post(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}/root").mock(
            return_value=httpx.Response(201, json={"ok": True})
        )
        if txn_nums:
            bac_url = f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}/root/$type/BAC_04_01_01_01_01"
            respx.post(bac_url).mock(
                side_effect=[
                    httpx.Response(
                        201,
                        json={"succinctProperties": {"cmis:objectId": f"{object_id_prefix}{txn}"}},
                    )
                    for txn in txn_nums
                ]
            )

    harness = PipelineHarness(
        build_pipeline=_build_pipeline,
        tracking_store=tracking_store,
        register_cmis_for_docs=_register_cmis_for_docs,
        db_path=tmp_path / "tracking.db",
        _opened_sources=opened,
    )
    yield harness
    harness.close()
