"""Factory de adapters: convierte un :class:`PipelineConfig` en un pipeline cableado.

El orchestrator y los adapters NO importan Pydantic. Este módulo se
encarga de la traducción entre el schema (Pydantic) y la config basada
en dataclasses de cada servicio. Se mantiene el Principio I de la
Constitución (separación de capas).
"""

from __future__ import annotations

__all__ = ["build_pipeline"]

import atexit
from concurrent.futures import ProcessPoolExecutor

from cmcourier.adapters.assembly import (
    AssemblerConfig,
    PdfAssembler,
    build_s4_process_pool,
)
from cmcourier.adapters.sources import As400DataSource, TabularDataSource
from cmcourier.adapters.tracking import SqliteDocumentCache, SQLiteTrackingStore
from cmcourier.adapters.tracking.as400_niarvilog import (
    As400NiarvilogStore,
    NiarvilogColumns,
)
from cmcourier.adapters.upload.cmis_uploader import CmisConfig, CmisUploader
from cmcourier.config.loader import Secrets
from cmcourier.config.schema import (
    As400RvabrepSource,
    CsvMetadataSourceConfig,
    CsvRvabrepSource,
    CsvTriggerConfig,
    IndexingColumnsModel,
    IndexingConfig,
    LocalScanTriggerConfig,
    MetadataConfigModel,
    MetadataSourceConfig,
    NiarvilogColumnsModel,
    PipelineConfig,
    RvabrepTriggerConfig,
    SingleDocTriggerConfig,
)
from cmcourier.config.schema import (
    MappingConfig as MappingConfigModel,
)
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.domain.ports import IDataSource, S0Strategy
from cmcourier.observability.metrics import MetricsRecorder
from cmcourier.observability.system_metrics import (
    build_sampler as build_system_metrics_sampler,
)
from cmcourier.orchestrators.staged import StagedPipeline
from cmcourier.services.document_cache import DocumentCacheService
from cmcourier.services.idempotency import IdempotencyCoordinator
from cmcourier.services.indexing import IndexingColumnsConfig, IndexingService
from cmcourier.services.mapping import MappingColumnsConfig, MappingService
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
    ValidationConfig,
)
from cmcourier.services.mock.sizing import parse_size
from cmcourier.services.mock.synthetic_content import (
    SizeBand,
    SizeMix,
    SyntheticPdfProvider,
)
from cmcourier.services.reconciler import (
    As400Reconciler,
    PendingSyncBuffer,
    PeriodicReconciler,
)
from cmcourier.services.recovery import As400Recovery
from cmcourier.services.triggers.csv import (
    CsvTriggerColumnsConfig,
    CsvTriggerStrategy,
)
from cmcourier.services.triggers.direct_rvabrep import (
    DirectRvabrepTriggerStrategy,
    RvabrepColumnsConfig,
    RvabrepFilters,
)
from cmcourier.services.triggers.local_scan import LocalScanTriggerStrategy


def build_pipeline(
    config: PipelineConfig,
    secrets: Secrets,
    *,
    trigger_strategy_override: S0Strategy | None = None,
    pipeline_name: str = "csv-trigger",
) -> StagedPipeline:
    """Construye cada adapter / servicio y devuelve el pipeline cableado.

    Pasá ``trigger_strategy_override`` para saltearte el `dispatch` basado
    en schema — lo usa la CLI single-doc para inyectar una `strategy`
    construida a partir de los argumentos de CLI.
    """
    # 048: el `source` RVABREP es pluggable (CSV ↔ AS400). Se construye
    # una sola vez acá y se comparte entre S0 (DirectRvabrepTriggerStrategy
    # / LocalScanTriggerStrategy) Y S1 (IndexingService) — la tabla
    # RVABREP es la misma data independientemente de dónde viva.
    rvabrep_src = _build_rvabrep_source(config.indexing, secrets)
    metadata_sources = _build_metadata_sources(config.metadata.sources, secrets)

    indexing_service = IndexingService(
        rvabrep_src,
        _indexing_columns_from_schema(config.indexing.columns),
    )
    trigger_strategy = trigger_strategy_override or _build_trigger_strategy(
        config, secrets, rvabrep_src, indexing_service
    )
    mapping_service = build_mapping_service(config.mapping)
    metadata_service = MetadataService(
        _metadata_config_from_schema(config.metadata),
        metadata_sources,
    )
    # 102: contenido sintético on-the-fly. Default off → comportamiento
    # intacto; con size_mix vacío se usa la distribución 60/30/10.
    synthetic_cfg = config.assembly.synthetic_content
    synthetic_provider: SyntheticPdfProvider | None = None
    if synthetic_cfg.enabled:
        if synthetic_cfg.size_mix:
            synthetic_provider = SyntheticPdfProvider(
                size_mix=SizeMix(
                    bands=tuple(
                        SizeBand(
                            name=band.name,
                            weight=band.weight,
                            min_bytes=parse_size(band.min),
                            max_bytes=parse_size(band.max),
                        )
                        for band in synthetic_cfg.size_mix
                    )
                ),
                seed=synthetic_cfg.seed,
            )
        else:
            synthetic_provider = SyntheticPdfProvider(seed=synthetic_cfg.seed)
    assembler_config = AssemblerConfig(
        source_root=config.assembly.source_root,
        temp_dir=config.assembly.temp_dir,
        image_type_map=config.assembly.image_type_map,
        synthetic_provider=synthetic_provider,
    )
    assembler = PdfAssembler(assembler_config)
    # 066: `ProcessPoolExecutor` opcional para S4 (`PDF assembly`).
    # Esquiva el GIL para el trabajo CPU-bound de img2pdf/PIL/PyPDF2;
    # `default-on` vía ``processing.s4_use_processes`` porque todo
    # `benchmark` por encima de ~5 docs/s se beneficia. El `process pool`
    # se apaga al salir del proceso vía ``atexit`` — un spec posterior
    # puede moverlo a un ``close()`` del pipeline si hace falta ciclo
    # de vida explícito.
    s4_process_pool: ProcessPoolExecutor | None = None
    if config.processing.s4_use_processes:
        s4_process_pool = build_s4_process_pool(
            assembler_config,
            max_workers=config.processing.s4_max_processes,
        )
        atexit.register(s4_process_pool.shutdown, wait=True)
    # 038: dimensionar el `connection pool` al mayor `worker` count que
    # `AIMD` pueda alcanzar para que el default de urllib3 (10) nunca sea
    # el cuello de botella en el pico de concurrencia.
    cmis_pool_size = (
        max(config.cmis.workers, config.cmis.auto_tune.max_threads)
        if config.cmis.auto_tune.enabled
        else config.cmis.workers
    )
    uploader = CmisUploader(
        CmisConfig(
            base_url=config.cmis.base_url,
            repo_id=config.cmis.repo_id,
            username=secrets.cmis_username,
            password=secrets.cmis_password,
            timeout_seconds=config.cmis.timeout_seconds,
            verify_ssl=config.cmis.verify_ssl,
            max_bandwidth_mbps=config.cmis.max_bandwidth_mbps,
            retry_max_attempts=config.cmis.retry_max_attempts,
            retry_base_delay_s=config.cmis.retry_base_delay_s,
            pool_size=cmis_pool_size,
            unmask_pii=config.observability.unmask_pii,
            http2=config.cmis.http2,
            upload_chunk_bytes=config.cmis.upload_chunk_bytes,
        )
    )
    tracking_store = SQLiteTrackingStore(config.tracking.db_path)
    metrics_recorder = MetricsRecorder(
        log_dir=config.observability.log_dir,
        slow_op_threshold_ms=float(config.observability.slow_op_threshold_ms),
        slow_op_top_n=config.observability.slow_op_top_n,
        enabled=config.observability.enabled,
        pipeline_metrics_enabled=config.observability.pipeline_metrics,
    )
    sampler = build_system_metrics_sampler(
        config.observability, log_dir=config.observability.log_dir
    )
    # 034 fase 3: capa opcional de coordinación AS400 NIARVILOG. Cuando
    # tracking.as400_sync.enabled es false (default), esto es None y el
    # pipeline corre en modo legacy solo-SQLite.
    coordinator, periodic_reconciler = _build_idempotency_coordinator(
        config=config, secrets=secrets, sqlite_store=tracking_store
    )
    document_cache = _build_document_cache_service(config=config)
    return StagedPipeline(
        trigger_strategy=trigger_strategy,
        indexing_service=indexing_service,
        mapping_service=mapping_service,
        metadata_service=metadata_service,
        assembler=assembler,
        metrics_recorder=metrics_recorder,
        pipeline_name=pipeline_name,
        uploader=uploader,
        tracking_store=tracking_store,
        workers=config.cmis.workers,
        prep_workers=config.processing.prep_workers,
        auto_tune=config.cmis.auto_tune,
        sampler=sampler,
        coordinator=coordinator,
        periodic_reconciler=periodic_reconciler,
        heavy_light_lanes=config.processing.heavy_light_lanes,
        document_cache=document_cache,
        s4_process_pool=s4_process_pool,
        keep_staged_files=config.assembly.keep_staged_files,
        s4_smart_routing=config.processing.s4_smart_routing,
    )


def _build_document_cache_service(*, config: PipelineConfig) -> DocumentCacheService | None:
    """037: devuelve un servicio sii ``metadata.cache.enabled``, si no None."""
    if not config.metadata.cache.enabled:
        return None
    sqlite_cache = SqliteDocumentCache(config.tracking.db_path)
    return DocumentCacheService(
        cache=sqlite_cache,
        ttl_minutes=config.metadata.cache.ttl_minutes,
    )


def _build_idempotency_coordinator(
    *,
    config: PipelineConfig,
    secrets: Secrets,
    sqlite_store: SQLiteTrackingStore,
) -> tuple[IdempotencyCoordinator | None, PeriodicReconciler | None]:
    """Cablea el coordinador SQLite + (opcional) AS400 NIARVILOG (034).

    Devuelve ``(None, None)`` cuando ``tracking.as400_sync.enabled=false``
    para que el StagedPipeline se quede en modo legacy pre-034.

    096: cuando ``mode == "periodic"`` devuelve también un
    :class:`PeriodicReconciler` — el coordinador escribe solo SQLite +
    encola en un buffer, y el reconciliador propaga a AS400 cada
    ``periodic.interval_minutes``.
    """
    sync_cfg = config.tracking.as400_sync
    if not sync_cfg.enabled:
        return None, None
    if sync_cfg.connection is None:  # pragma: no cover — el schema lo garantiza
        raise ConfigurationError(
            "tracking.as400_sync.enabled=true requires connection settings",
        )
    if not secrets.as400_username or not secrets.as400_password:
        raise ConfigurationError(
            "AS400 credentials missing in environment (set AS400_USERNAME / AS400_PASSWORD)",
        )
    as400_store = As400NiarvilogStore(
        connection=sync_cfg.connection,
        username=secrets.as400_username,
        password=secrets.as400_password,
        library=sync_cfg.library,
        table=sync_cfg.table,
        columns=_niarvilog_columns_from_schema(sync_cfg.columns),
        stale_in_progress_minutes=sync_cfg.stale_in_progress_minutes,
        retry_attempts=sync_cfg.retry_attempts,
        retry_base_delay_s=sync_cfg.retry_base_delay_s,
    )
    if sync_cfg.mode == "periodic":
        assert sync_cfg.periodic is not None  # el schema lo garantiza
        buffer = PendingSyncBuffer()
        coordinator = IdempotencyCoordinator(
            sqlite_store=sqlite_store,
            as400_store=as400_store,
            mode="periodic",
            pending_buffer=buffer,
        )
        reconciler = As400Reconciler(sqlite_store=sqlite_store, as400_store=as400_store)
        periodic = PeriodicReconciler(
            reconciler=reconciler,
            buffer=buffer,
            interval_s=sync_cfg.periodic.interval_minutes * 60.0,
        )
        return coordinator, periodic
    return IdempotencyCoordinator(sqlite_store=sqlite_store, as400_store=as400_store), None


def build_as400_recovery(
    config: PipelineConfig,
    secrets: Secrets,
    *,
    sqlite_store: SQLiteTrackingStore,
) -> As400Recovery:
    """099: arma el :class:`As400Recovery` para ``cmcourier sync recover``.

    El caller (CLI) ya validó que ``tracking.as400_sync`` está habilitado
    con conexión y credenciales — acá se asume y se construyen las cuatro
    dependencias: store SQLite (provisto), store AS400, servicio de
    indexing (para re-derivar DOCFRM/IMGTIP desde RVABREP) y servicio de
    mapping (para IDNBAC/TIPIDN)."""
    sync_cfg = config.tracking.as400_sync
    assert sync_cfg.connection is not None  # el CLI lo valida antes
    as400_store = As400NiarvilogStore(
        connection=sync_cfg.connection,
        username=secrets.as400_username,
        password=secrets.as400_password,
        library=sync_cfg.library,
        table=sync_cfg.table,
        columns=_niarvilog_columns_from_schema(sync_cfg.columns),
        stale_in_progress_minutes=sync_cfg.stale_in_progress_minutes,
        retry_attempts=sync_cfg.retry_attempts,
        retry_base_delay_s=sync_cfg.retry_base_delay_s,
    )
    rvabrep_src = _build_rvabrep_source(config.indexing, secrets)
    indexing_service = IndexingService(
        rvabrep_src,
        _indexing_columns_from_schema(config.indexing.columns),
    )
    return As400Recovery(
        sqlite_store=sqlite_store,
        as400_store=as400_store,
        indexing_service=indexing_service,
        mapping_service=build_mapping_service(config.mapping),
    )


# ---------------------------------------------------------------------------
# Dispatch del `source` RVABREP (048)
# ---------------------------------------------------------------------------


def _build_rvabrep_source(indexing_cfg: IndexingConfig, secrets: Secrets) -> IDataSource:
    """Construye el ``IDataSource`` RVABREP desde ``indexing.source`` (048).

    ``csv`` → ``TabularDataSource`` sobre el archivo CSV.
    ``as400`` → ``As400DataSource`` en modo `query` — el SELECT del
    operador (JOINs / filtros permitidos) se envuelve como ``(query) AS T``
    para que todo el contrato `IDataSource` funcione transparentemente. El
    único `source` retornado alimenta tanto a S0 (descubrimiento de
    triggers) como a S1 (lookup de docs).
    """
    source = indexing_cfg.source
    if isinstance(source, CsvRvabrepSource):
        return TabularDataSource(source.csv_path)
    if isinstance(source, As400RvabrepSource):
        if not secrets.as400_username or not secrets.as400_password:
            raise ConfigurationError(
                "indexing.source.kind 'as400' requires AS400_USERNAME and AS400_PASSWORD env vars",
                missing_vars=[
                    name
                    for name, value in (
                        ("AS400_USERNAME", secrets.as400_username),
                        ("AS400_PASSWORD", secrets.as400_password),
                    )
                    if not value
                ],
            )
        return As400DataSource(
            host=source.connection.host,
            port=source.connection.port,
            database=source.connection.database,
            driver=source.connection.driver,
            username=secrets.as400_username,
            password=secrets.as400_password,
            query=source.query,
        )
    raise ConfigurationError(
        "unknown indexing.source.kind",
        kind=getattr(source, "kind", "<missing>"),
    )


# ---------------------------------------------------------------------------
# Dispatch de la `strategy` de trigger
# ---------------------------------------------------------------------------


def _build_trigger_strategy(
    config: PipelineConfig,
    secrets: Secrets,
    rvabrep_src: IDataSource,
    indexing_service: IndexingService,
) -> S0Strategy:
    trigger_cfg = config.trigger
    if isinstance(trigger_cfg, CsvTriggerConfig):
        trigger_src = TabularDataSource(trigger_cfg.csv_path)
        return CsvTriggerStrategy(
            trigger_src,
            CsvTriggerColumnsConfig(
                col_shortname=trigger_cfg.shortname_column,
                col_cif=trigger_cfg.cif_column,
                col_system_id=trigger_cfg.system_id_column,
            ),
        )
    if isinstance(trigger_cfg, RvabrepTriggerConfig):
        return DirectRvabrepTriggerStrategy(
            rvabrep_src,
            filters=RvabrepFilters(
                systems=tuple(trigger_cfg.filters.systems),
                document_types=tuple(trigger_cfg.filters.document_types),
            ),
            columns=RvabrepColumnsConfig(
                col_shortname=config.indexing.columns.shortname_column,
                col_cif=config.indexing.columns.index2_column,
                col_system_id=config.indexing.columns.system_id_column,
                col_id_rvi=config.indexing.columns.index7_column,
            ),
        )
    if isinstance(trigger_cfg, LocalScanTriggerConfig):
        return LocalScanTriggerStrategy(
            scan_path=trigger_cfg.scan_path,
            rvabrep_source=rvabrep_src,
            columns=RvabrepColumnsConfig(
                col_shortname=config.indexing.columns.shortname_column,
                col_cif=config.indexing.columns.index2_column,
                col_system_id=config.indexing.columns.system_id_column,
                col_id_rvi=config.indexing.columns.index7_column,
                file_name_column=config.indexing.columns.file_name_column,
            ),
            recursive=trigger_cfg.recursive,
        )
    if isinstance(trigger_cfg, SingleDocTriggerConfig):
        raise ConfigurationError(
            "single_doc trigger requires CLI-provided shortname/system_id; "
            "use `cmcourier single-doc run` with --shortname/--system/--cif "
            "and trigger_strategy_override",
            kind="single_doc",
        )
    # 048: ``trigger.kind: as400`` fue removido — "AS400" es una elección
    # de `source` (``indexing.source.kind: as400``), no un `kind` de
    # trigger. El loader lo rechaza con un error directivo antes de que
    # lleguemos acá.
    raise ConfigurationError(
        "unknown trigger.kind",
        kind=getattr(trigger_cfg, "kind", "<unknown>"),
    )


# ---------------------------------------------------------------------------
# Dispatch de `sources` de metadata (015)
# ---------------------------------------------------------------------------


def _build_metadata_sources(
    sources: list[MetadataSourceConfig],
    secrets: Secrets,
) -> dict[str, IDataSource]:
    """Abre cada `source` de metadata y devuelve el registro alias→adapter."""
    registry: dict[str, IDataSource] = {}
    for src_cfg in sources:
        if isinstance(src_cfg, CsvMetadataSourceConfig):
            registry[src_cfg.alias] = TabularDataSource(src_cfg.csv_path)
            continue
        # as400 — credenciales requeridas.
        if not secrets.as400_username or not secrets.as400_password:
            missing = [
                name
                for name, value in (
                    ("AS400_USERNAME", secrets.as400_username),
                    ("AS400_PASSWORD", secrets.as400_password),
                )
                if not value
            ]
            raise ConfigurationError(
                "AS400 credentials required for as400 metadata source",
                alias=src_cfg.alias,
                missing_vars=missing,
            )
        registry[src_cfg.alias] = As400DataSource(
            host=src_cfg.as400_connection.host,
            port=src_cfg.as400_connection.port,
            database=src_cfg.as400_connection.database,
            driver=src_cfg.as400_connection.driver,
            username=secrets.as400_username,
            password=secrets.as400_password,
            table=src_cfg.table or "",
            query=src_cfg.query,
        )
    return registry


# ---------------------------------------------------------------------------
# Conversores schema → config de servicio
# ---------------------------------------------------------------------------


def _indexing_columns_from_schema(model: IndexingColumnsModel) -> IndexingColumnsConfig:
    return IndexingColumnsConfig(
        shortname_column=model.shortname_column,
        system_id_column=model.system_id_column,
        delete_code_column=model.delete_code_column,
        txn_num_column=model.txn_num_column,
        index2_column=model.index2_column,
        index3_column=model.index3_column,
        index4_column=model.index4_column,
        index5_column=model.index5_column,
        index6_column=model.index6_column,
        index7_column=model.index7_column,
        image_type_column=model.image_type_column,
        image_path_column=model.image_path_column,
        file_name_column=model.file_name_column,
        creation_date_column=model.creation_date_column,
        last_view_date_column=model.last_view_date_column,
        total_pages_column=model.total_pages_column,
    )


def _niarvilog_columns_from_schema(model: NiarvilogColumnsModel) -> NiarvilogColumns:
    return NiarvilogColumns(
        system_id=model.system_id_column,
        txn_num=model.txn_num_column,
        doc_format=model.doc_format_column,
        image_archive=model.image_archive_column,
        image_type=model.image_type_column,
        client_cif=model.client_cif_column,
        client_num=model.client_num_column,
        status=model.status_column,
        idcm=model.idcm_column,
        cm_type=model.cm_type_column,
        cm_object_id=model.cm_object_id_column,
        retry_count=model.retry_count_column,
        started_at=model.started_at_column,
        finished_at=model.finished_at_column,
        error_message=model.error_message_column,
    )


def _mapping_columns_from_schema(model: MappingConfigModel) -> MappingColumnsConfig:
    return MappingColumnsConfig(
        col_clase_id=model.clase_id_column,
        col_id_rvi=model.id_rvi_column,
        col_id_corto=model.id_corto_column,
        col_clase_name=model.clase_name_column,
        col_metadata_list=model.metadata_list_column,
        col_cmis_type=model.cmis_type_column,
        col_rvi_cm_id_rvi=model.rvi_cm_id_rvi_column,
        col_rvi_cm_id_cm=model.rvi_cm_id_cm_column,
        col_rvi_cm_clase_id=model.rvi_cm_clase_id_column,
        col_rvi_cm_cmis_type=model.rvi_cm_cmis_type_column,
        col_rvi_cm_cmis_folder=model.rvi_cm_cmis_folder_column,
        col_metadatos_id_corto=model.metadatos_id_corto_column,
        col_metadatos_metadata=model.metadatos_metadata_column,
        col_metadatos_required=model.metadatos_required_column,
        col_metadatos_cmis_property_id=model.metadatos_cmis_property_id_column,
        required_marker=model.required_marker,
    )


def build_mapping_service(model: MappingConfigModel) -> MappingService:
    """Construye un :class:`MappingService` totalmente cargado desde un ``MappingConfig``.

    Elige modo consolidado o `split` según qué paths estén seteados
    (035). Abre el/los ``TabularDataSource`` subyacente(s), carga el
    cache, y luego cierra el/los `source` — ``MappingService`` lee
    todo en construcción.
    """
    columns = _mapping_columns_from_schema(model)
    if model.csv_path is not None:
        source = TabularDataSource(model.csv_path)
        try:
            return MappingService(source, columns)
        finally:
            source.close()
    assert model.rvi_cm_csv_path is not None  # noqa: S101 - el validador lo garantiza
    assert model.metadatos_csv_path is not None  # noqa: S101 - el validador lo garantiza
    rvi_src = TabularDataSource(model.rvi_cm_csv_path)
    metadatos_src = TabularDataSource(model.metadatos_csv_path)
    try:
        return MappingService(rvi_src, columns, metadata_source=metadatos_src)
    finally:
        rvi_src.close()
        metadatos_src.close()


def _metadata_config_from_schema(model: MetadataConfigModel) -> MetadataConfig:
    field_sources: dict[str, FieldSourceConfig] = {}
    for canonical, fc in model.field_sources.items():
        field_sources[canonical] = FieldSourceConfig(
            sources=tuple(
                SourceConfig(
                    source_type=src.source_type,
                    lookup_value_column=src.lookup_value_column,
                    lookup_key_column=src.lookup_key_column,
                    validation=(
                        ValidationConfig(allowed_pattern=src.validation.allowed_pattern)
                        if src.validation is not None
                        else None
                    ),
                    lookup_value_source=src.lookup_value_source,
                )
                for src in fc.sources
            ),
            default_value=fc.default_value,
        )
    return MetadataConfig(
        field_aliases=dict(model.field_aliases),
        field_sources=field_sources,
        prefetch_enabled=model.prefetch_enabled,
    )
