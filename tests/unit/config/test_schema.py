"""Unit tests for ``cmcourier.config.schema``.

Pydantic v2 validation tests: every model is ``frozen=True, extra="forbid"``
so unknown keys raise, mutation raises, and the type system catches
required-field omissions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cmcourier.config.schema import (
    As400ConnectionConfig,
    As400MetadataSourceConfig,
    As400RvabrepSource,
    AssemblyConfig,
    CmisConfigModel,
    CsvMetadataSourceConfig,
    CsvRvabrepSource,
    CsvTriggerConfig,
    FieldConfig,
    FieldSourceItem,
    HeavyLightLanesConfig,
    IndexingConfig,
    MappingConfig,
    MetadataCacheConfig,
    MetadataConfigModel,
    MssqlConnectionConfig,
    MssqlMetadataSourceConfig,
    PipelineConfig,
    ProcessingConfig,
    RvabrepTriggerConfig,
    TrackingConfig,
    TriggerCsvConfig,
    split_lookup_source_type,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_full_data(
    trigger_csv: Path,
    rvabrep_csv: Path,
    modelo_csv: Path,
    clients_csv: Path,
    assembly_root: Path,
    tmp_path: Path,
) -> dict[str, Any]:
    return {
        "trigger": {"kind": "csv", "csv_path": str(trigger_csv)},
        "indexing": {"source": {"kind": "csv", "csv_path": str(rvabrep_csv)}},
        "mapping": {"csv_path": str(modelo_csv)},
        "metadata": {
            "field_aliases": {"CIF": "BAC_CIF"},
            "field_sources": {
                "BAC_CIF": {
                    "sources": [
                        {"source_type": "trigger", "lookup_value_column": "cif"},
                    ],
                },
            },
            "sources": [{"kind": "csv", "alias": "clients", "csv_path": str(clients_csv)}],
        },
        "assembly": {
            "source_root": str(assembly_root),
            "temp_dir": str(tmp_path / "stg"),
        },
        "cmis": {
            "base_url": "http://cmis.test:9080/cmis",
            "repo_id": "$x!test",
        },
        "tracking": {"db_path": str(tmp_path / "tracking.db")},
    }


@pytest.fixture
def fixture_paths(tmp_path: Path) -> dict[str, Path]:
    """Materialize a synthetic file tree the schema can validate against."""
    trigger = tmp_path / "triggers.csv"
    trigger.write_text("ShortName,CIF,SystemID\n")
    rvabrep = tmp_path / "rvabrep.csv"
    rvabrep.write_text("shortname,system_id,txn_num\n")
    modelo = tmp_path / "modelo.csv"
    modelo.write_text("ID RVI,ID CLASE DOCUMENTAL\n")
    clients = tmp_path / "clients.csv"
    clients.write_text("CIF,Nombre_Cliente\n")
    assembly_root = tmp_path / "assembly"
    assembly_root.mkdir()
    return {
        "trigger": trigger,
        "rvabrep": rvabrep,
        "modelo": modelo,
        "clients": clients,
        "assembly_root": assembly_root,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_full_config_validates(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        config = PipelineConfig.model_validate(data)
        assert config.cmis.base_url == "http://cmis.test:9080/cmis"
        assert config.batch_size == 1000
        assert config.tracking.db_path == tmp_path / "tracking.db"

    def test_extra_top_level_key_rejected(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["cosmic_settings"] = {"intent": "blast"}
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)

    def test_extra_nested_key_rejected(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["mapping"]["unknown_field"] = "foo"
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)


class TestFieldSourceItem:
    @pytest.mark.parametrize(
        "source_type",
        ["trigger", "rvabrep", "csv:clients", "as400:default"],
    )
    def test_accepted_source_types(self, source_type: str) -> None:
        item = FieldSourceItem(source_type=source_type, lookup_value_column="x")
        assert item.source_type == source_type

    def test_rejects_unknown_source_type(self) -> None:
        with pytest.raises(ValidationError):
            FieldSourceItem(source_type="http:remote", lookup_value_column="x")


class TestFieldConfig:
    def test_requires_at_least_one_source(self) -> None:
        with pytest.raises(ValidationError):
            FieldConfig(sources=[])

    def test_default_value_optional(self) -> None:
        fc = FieldConfig(
            sources=[FieldSourceItem(source_type="trigger", lookup_value_column="cif")]
        )
        assert fc.default_value is None


class TestNumericConstraints:
    def test_cmis_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            CmisConfigModel(base_url="x", repo_id="y", timeout_seconds=0)

    def test_retry_max_attempts_must_be_ge_one(self) -> None:
        with pytest.raises(ValidationError):
            CmisConfigModel(base_url="x", repo_id="y", retry_max_attempts=0)

    def test_prep_workers_defaults_to_one(self) -> None:
        # 056: default 1 keeps S2/S3/S4 serial — byte-identical to pre-056.
        assert ProcessingConfig().prep_workers == 1

    def test_prep_workers_must_be_ge_one(self) -> None:
        with pytest.raises(ValidationError):
            ProcessingConfig(prep_workers=0)

    def test_auto_tune_min_samples_defaults_to_twenty(self) -> None:
        # 061: AIMD gates on sample_count >= min_samples (default 20) to
        # avoid halving on a single cold-connection outlier.
        from cmcourier.config.schema import AutoTuneConfig

        assert AutoTuneConfig().min_samples == 20

    def test_auto_tune_min_samples_must_be_ge_one(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        with pytest.raises(ValidationError):
            AutoTuneConfig(min_samples=0)

    def test_processing_mode_defaults_to_batched(self) -> None:
        # 063: default keeps every byte of pre-063 behaviour intact.
        assert ProcessingConfig().mode == "batched"

    def test_processing_mode_rejects_unknown_value(self) -> None:
        with pytest.raises(ValidationError):
            ProcessingConfig(mode="continuous")  # type: ignore[arg-type]

    def test_processing_mode_accepts_streaming(self) -> None:
        cfg = ProcessingConfig(mode="streaming")
        assert cfg.mode == "streaming"
        assert cfg.streaming.bucket_size == 100  # default

    def test_streaming_bucket_size_defaults_to_one_hundred(self) -> None:
        from cmcourier.config.schema import StreamingConfig

        assert StreamingConfig().bucket_size == 100

    def test_streaming_bucket_size_must_be_ge_one(self) -> None:
        from cmcourier.config.schema import StreamingConfig

        with pytest.raises(ValidationError):
            StreamingConfig(bucket_size=0)
        with pytest.raises(ValidationError):
            StreamingConfig(bucket_size=-1)

    def test_s4_use_processes_defaults_to_true(self) -> None:
        # 066: default-on to give real CPU-bound parallelism out of the
        # box. Operators can opt out via `s4_use_processes: false`.
        assert ProcessingConfig().s4_use_processes is True

    def test_s4_max_processes_defaults_to_none(self) -> None:
        # 066: None → os.cpu_count() at construction time.
        assert ProcessingConfig().s4_max_processes is None

    def test_s4_max_processes_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            ProcessingConfig(s4_max_processes=0)

    def test_s4_max_processes_accepts_explicit_int(self) -> None:
        cfg = ProcessingConfig(s4_max_processes=4)
        assert cfg.s4_max_processes == 4

    def test_auto_tune_growth_factor_defaults_to_1_25(self) -> None:
        # 068: aggressive growth — current * 1.25 with +1 floor.
        from cmcourier.config.schema import AutoTuneConfig

        assert AutoTuneConfig().growth_factor == 1.25

    def test_auto_tune_growth_factor_range(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        with pytest.raises(ValidationError):
            AutoTuneConfig(growth_factor=0.99)
        with pytest.raises(ValidationError):
            AutoTuneConfig(growth_factor=4.01)

    def test_auto_tune_halve_factor_defaults_to_0_75(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        assert AutoTuneConfig().halve_factor == 0.75

    def test_auto_tune_halve_factor_range(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        with pytest.raises(ValidationError):
            AutoTuneConfig(halve_factor=0.0)
        with pytest.raises(ValidationError):
            AutoTuneConfig(halve_factor=1.01)

    def test_auto_tune_halve_threshold_ratio_defaults_to_1_5(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        assert AutoTuneConfig().halve_threshold_ratio == 1.5

    def test_auto_tune_halve_threshold_ratio_range(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        with pytest.raises(ValidationError):
            AutoTuneConfig(halve_threshold_ratio=1.0)
        with pytest.raises(ValidationError):
            AutoTuneConfig(halve_threshold_ratio=10.01)

    def test_batch_size_must_be_ge_one(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["batch_size"] = 0
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)


class TestFrozenness:
    def test_pipeline_config_is_frozen(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        config = PipelineConfig.model_validate(data)
        with pytest.raises(ValidationError):
            config.batch_size = 5000  # type: ignore[misc]


class TestSubmodelDefaults:
    def test_assembly_image_type_map_default(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        cfg = AssemblyConfig(
            source_root=fixture_paths["assembly_root"],
            temp_dir=tmp_path / "stg",
        )
        assert cfg.image_type_map == {
            "B": "image/tiff",
            "O": "application/pdf",
            "C": "image/jpeg",
        }

    def test_trigger_column_defaults(self, fixture_paths: dict[str, Path]) -> None:
        cfg = TriggerCsvConfig(csv_path=fixture_paths["trigger"])
        assert cfg.shortname_column == "ShortName"
        assert cfg.cif_column == "CIF"
        assert cfg.system_id_column == "SystemID"

    def test_indexing_columns_defaults_match_as400(self, fixture_paths: dict[str, Path]) -> None:
        cfg = IndexingConfig(source=CsvRvabrepSource(csv_path=fixture_paths["rvabrep"]))
        assert cfg.columns.shortname_column == "ABABCD"
        assert cfg.columns.txn_num_column == "ABAANB"
        assert cfg.columns.index7_column == "ABAHCD"

    def test_indexing_source_csv_variant(self, fixture_paths: dict[str, Path]) -> None:
        cfg = IndexingConfig(source=CsvRvabrepSource(csv_path=fixture_paths["rvabrep"]))
        assert isinstance(cfg.source, CsvRvabrepSource)
        assert cfg.source.kind == "csv"

    def test_indexing_source_as400_variant(self) -> None:
        cfg = IndexingConfig(
            source=As400RvabrepSource(
                kind="as400",
                connection=As400ConnectionConfig(host="as400.test"),
                query="SELECT * FROM RVILIB.RVABREP",
            )
        )
        assert isinstance(cfg.source, As400RvabrepSource)
        assert cfg.source.connection.host == "as400.test"
        assert "RVABREP" in cfg.source.query

    def test_mapping_column_defaults(self, fixture_paths: dict[str, Path]) -> None:
        cfg = MappingConfig(csv_path=fixture_paths["modelo"])
        assert cfg.id_rvi_column == "ID RVI"
        assert cfg.clase_id_column == "ID CLASE DOCUMENTAL"

    def test_metadata_empty_field_sources_invalid(self, fixture_paths: dict[str, Path]) -> None:
        # field_sources is required (no default), so this should pass; but
        # with no fields configured, the orchestrator's get_mapping later
        # may fail. The schema doesn't enforce min field count.
        cfg = MetadataConfigModel(field_sources={})
        assert cfg.field_sources == {}

    def test_tracking_db_path_is_required(self) -> None:
        with pytest.raises(ValidationError):
            TrackingConfig()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Discriminated union: trigger.kind
# ---------------------------------------------------------------------------


class TestTriggerDiscriminatedUnion:
    def test_csv_kind_loads(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        config = PipelineConfig.model_validate(data)
        assert isinstance(config.trigger, CsvTriggerConfig)
        assert config.trigger.kind == "csv"

    def test_rvabrep_kind_loads(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["trigger"] = {
            "kind": "rvabrep",
            "filters": {"systems": ["1"], "document_types": ["FF17"]},
        }
        config = PipelineConfig.model_validate(data)
        assert isinstance(config.trigger, RvabrepTriggerConfig)
        assert config.trigger.filters.systems == ["1"]

    def test_local_scan_kind_loads(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        from cmcourier.config.schema import LocalScanTriggerConfig

        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        data["trigger"] = {"kind": "local_scan", "scan_path": str(scan_dir)}
        config = PipelineConfig.model_validate(data)
        assert isinstance(config.trigger, LocalScanTriggerConfig)
        assert config.trigger.scan_path == scan_dir

    def test_local_scan_requires_existing_path(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["trigger"] = {
            "kind": "local_scan",
            "scan_path": str(tmp_path / "does_not_exist"),
        }
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)

    def test_trigger_kind_as400_no_longer_a_trigger_kind(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """048 — ``as400`` was removed from ``TriggerConfigUnion``. It's a
        *source* choice now (``indexing.source.kind: as400``). The
        discriminated union rejects it at validation time; the loader
        adds a friendlier directive error on top (see test_loader.py)."""
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["trigger"] = {"kind": "as400", "query": "SELECT 1"}
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)

    def test_indexing_source_as400_loads_via_full_config(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """The AS400 RVABREP source lives under ``indexing.source`` now —
        same pipeline, different data source."""
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["indexing"] = {
            "source": {
                "kind": "as400",
                "connection": {"host": "10.0.0.1", "database": "RVILIB"},
                "query": "SELECT * FROM RVILIB.RVABREP r WHERE r.ABAACD = '3'",
            }
        }
        config = PipelineConfig.model_validate(data)
        assert isinstance(config.indexing.source, As400RvabrepSource)
        assert config.indexing.source.connection.host == "10.0.0.1"
        assert "RVABREP" in config.indexing.source.query

    def test_unknown_kind_rejected(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["trigger"] = {"kind": "ftp", "csv_path": str(fixture_paths["trigger"])}
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)

    def test_as400_connection_defaults(self) -> None:
        cfg = As400ConnectionConfig(host="10.0.0.1")
        assert cfg.port == 446
        assert cfg.database == "RVILIB"
        assert cfg.driver == "iSeries Access ODBC Driver"
        assert cfg.table is None

    def test_csv_alias_for_backwards_compat(self) -> None:
        # TriggerCsvConfig is the old name — kept as an alias to CsvTriggerConfig.
        assert TriggerCsvConfig is CsvTriggerConfig

    def test_single_doc_kind_loads(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        from cmcourier.config.schema import SingleDocTriggerConfig

        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["trigger"] = {"kind": "single_doc"}
        config = PipelineConfig.model_validate(data)
        assert isinstance(config.trigger, SingleDocTriggerConfig)
        assert config.trigger.kind == "single_doc"

    def test_single_doc_rejects_extra_fields(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["trigger"] = {"kind": "single_doc", "shortname": "X"}
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)


# ---------------------------------------------------------------------------
# Metadata source discriminated union (015)
# ---------------------------------------------------------------------------


class TestMetadataSourceDiscriminatedUnion:
    def test_csv_kind_loads(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        # Already kind=csv by helper. Verify the discriminated union picks it.
        config = PipelineConfig.model_validate(data)
        source = config.metadata.sources[0]
        assert isinstance(source, CsvMetadataSourceConfig)
        assert source.kind == "csv"

    def test_as400_kind_loads(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["metadata"]["sources"] = [
            {
                "kind": "as400",
                "alias": "customers",
                "as400_connection": {"host": "10.0.0.1"},
                "table": "CUSTOMERS",
            }
        ]
        config = PipelineConfig.model_validate(data)
        source = config.metadata.sources[0]
        assert isinstance(source, As400MetadataSourceConfig)
        assert source.alias == "customers"
        assert source.table == "CUSTOMERS"
        assert source.as400_connection.host == "10.0.0.1"

    def test_as400_table_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            As400MetadataSourceConfig(
                kind="as400",
                alias="x",
                as400_connection=As400ConnectionConfig(host="h"),
                table="",
            )

    def test_as400_with_query_loads(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["metadata"]["sources"] = [
            {
                "kind": "as400",
                "alias": "customers",
                "as400_connection": {"host": "10.0.0.1"},
                "query": "SELECT CIF, NAME FROM CUSTOMERS WHERE ACTIVE = 'Y'",
            }
        ]
        config = PipelineConfig.model_validate(data)
        source = config.metadata.sources[0]
        assert isinstance(source, As400MetadataSourceConfig)
        assert source.table is None
        assert source.query == "SELECT CIF, NAME FROM CUSTOMERS WHERE ACTIVE = 'Y'"

    def test_as400_both_table_and_query_rejected(self) -> None:
        with pytest.raises(ValidationError) as ei:
            As400MetadataSourceConfig(
                kind="as400",
                alias="x",
                as400_connection=As400ConnectionConfig(host="h"),
                table="CUSTOMERS",
                query="SELECT * FROM CUSTOMERS",
            )
        assert "exactly one" in str(ei.value)

    def test_as400_neither_table_nor_query_rejected(self) -> None:
        with pytest.raises(ValidationError) as ei:
            As400MetadataSourceConfig(
                kind="as400",
                alias="x",
                as400_connection=As400ConnectionConfig(host="h"),
            )
        assert "exactly one" in str(ei.value)


# ---------------------------------------------------------------------------
# 020 — Observability config
# ---------------------------------------------------------------------------


class TestObservabilityConfig:
    def test_defaults(self) -> None:
        from cmcourier.config.schema import ObservabilityConfig

        cfg = ObservabilityConfig()
        assert cfg.enabled is True
        assert cfg.pipeline_metrics is True
        assert cfg.network_metrics is True
        # 026: system_metrics is now a nested model, default ON.
        assert cfg.system_metrics.enabled is True
        assert cfg.system_metrics.sample_interval_s == 5.0
        assert cfg.log_dir == Path("./logs")
        assert cfg.log_format == "json"
        assert cfg.rotation_mb == 100
        assert cfg.retention_days == 30
        assert cfg.slow_op_threshold_ms == 5000
        assert cfg.slow_op_top_n == 20

    def test_system_metrics_structured_disable(self) -> None:
        from cmcourier.config.schema import ObservabilityConfig

        cfg = ObservabilityConfig(system_metrics={"enabled": False})
        assert cfg.system_metrics.enabled is False

    def test_system_metrics_structured_custom_interval(self) -> None:
        from cmcourier.config.schema import ObservabilityConfig

        cfg = ObservabilityConfig(system_metrics={"enabled": True, "sample_interval_s": 10.0})
        assert cfg.system_metrics.sample_interval_s == 10.0

    def test_system_metrics_legacy_bool_false_coerced(self) -> None:
        """REQ-002: pre-026 YAMLs with `system_metrics: false` still load."""
        from cmcourier.config.schema import ObservabilityConfig

        cfg = ObservabilityConfig(system_metrics=False)
        assert cfg.system_metrics.enabled is False
        assert cfg.system_metrics.sample_interval_s == 5.0

    def test_system_metrics_legacy_bool_true_coerced(self) -> None:
        """REQ-002: bool=True coerces to enabled=True (no rejection now)."""
        from cmcourier.config.schema import ObservabilityConfig

        cfg = ObservabilityConfig(system_metrics=True)
        assert cfg.system_metrics.enabled is True

    def test_system_metrics_interval_out_of_range_rejected(self) -> None:
        from cmcourier.config.schema import ObservabilityConfig

        with pytest.raises(ValidationError):
            ObservabilityConfig(system_metrics={"sample_interval_s": 0.5})
        with pytest.raises(ValidationError):
            ObservabilityConfig(system_metrics={"sample_interval_s": 120.0})

    def test_system_metrics_strict_unknown_field_rejected(self) -> None:
        from cmcourier.config.schema import ObservabilityConfig

        with pytest.raises(ValidationError):
            ObservabilityConfig(system_metrics={"enabled": True, "bogus_field": 42})

    def test_log_format_invalid_rejected(self) -> None:
        from cmcourier.config.schema import ObservabilityConfig

        with pytest.raises(ValidationError):
            ObservabilityConfig(log_format="xml")  # type: ignore[arg-type]

    def test_rotation_mb_must_be_ge_one(self) -> None:
        from cmcourier.config.schema import ObservabilityConfig

        with pytest.raises(ValidationError):
            ObservabilityConfig(rotation_mb=0)

    def test_pipeline_config_observability_defaults_when_absent(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        # Regression: existing YAMLs without observability block still validate.
        from cmcourier.config.schema import ObservabilityConfig

        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        config = PipelineConfig.model_validate(data)
        assert isinstance(config.observability, ObservabilityConfig)
        assert config.observability.enabled is True


# ---------------------------------------------------------------------------
# 025 — AutoTuneConfig + cmis.workers/auto_tune
# ---------------------------------------------------------------------------


class TestAutoTuneConfig:
    def test_defaults(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        cfg = AutoTuneConfig()
        assert cfg.enabled is False
        assert cfg.min_threads == 2
        assert cfg.max_threads == 50
        assert cfg.target_p95_ms == 5000.0
        assert cfg.adjustment_interval_s == 30
        assert cfg.warmup_seconds == 60
        assert cfg.timeout_auto_adjust is True
        assert cfg.min_timeout_s == 30
        assert cfg.max_timeout_s == 600

    def test_min_greater_than_max_threads_rejected(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        with pytest.raises(ValidationError) as ei:
            AutoTuneConfig(min_threads=50, max_threads=10)
        assert "min_threads" in str(ei.value)

    def test_min_greater_than_max_timeout_rejected(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        with pytest.raises(ValidationError) as ei:
            AutoTuneConfig(min_timeout_s=600, max_timeout_s=30)
        assert "min_timeout_s" in str(ei.value)

    def test_target_p95_must_be_positive(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        with pytest.raises(ValidationError):
            AutoTuneConfig(target_p95_ms=0)

    def test_warmup_zero_allowed(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig

        cfg = AutoTuneConfig(warmup_seconds=0)
        assert cfg.warmup_seconds == 0


class TestCmisWorkersAndAutoTune:
    def test_cmis_defaults_include_workers(self) -> None:
        from cmcourier.config.schema import AutoTuneConfig, CmisConfigModel

        cfg = CmisConfigModel(base_url="http://x:9080/cmis", repo_id="$x!t")
        assert cfg.workers == 4
        assert isinstance(cfg.auto_tune, AutoTuneConfig)
        assert cfg.auto_tune.enabled is False

    def test_cmis_workers_must_be_ge_one(self) -> None:
        from cmcourier.config.schema import CmisConfigModel

        with pytest.raises(ValidationError):
            CmisConfigModel(base_url="x", repo_id="y", workers=0)

    def test_pipeline_config_loads_workers_and_auto_tune(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["cmis"]["workers"] = 8
        data["cmis"]["auto_tune"] = {
            "enabled": True,
            "target_p95_ms": 2000.0,
        }
        config = PipelineConfig.model_validate(data)
        assert config.cmis.workers == 8
        assert config.cmis.auto_tune.enabled is True
        assert config.cmis.auto_tune.target_p95_ms == 2000.0

    def test_cmis_block_without_workers_uses_defaults(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        # Regression: existing YAMLs without cmis.workers / auto_tune still validate.
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        config = PipelineConfig.model_validate(data)
        assert config.cmis.workers == 4
        assert config.cmis.auto_tune.enabled is False

    def test_unknown_kind_rejected(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["metadata"]["sources"] = [{"kind": "ldap", "alias": "directory", "url": "ldap://..."}]
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)

    def test_mixed_kinds_load(self, fixture_paths: dict[str, Path], tmp_path: Path) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["metadata"]["sources"] = [
            {"kind": "csv", "alias": "clients", "csv_path": str(fixture_paths["clients"])},
            {
                "kind": "as400",
                "alias": "customers",
                "as400_connection": {"host": "10.0.0.1"},
                "table": "CUSTOMERS",
            },
        ]
        config = PipelineConfig.model_validate(data)
        assert len(config.metadata.sources) == 2
        assert isinstance(config.metadata.sources[0], CsvMetadataSourceConfig)
        assert isinstance(config.metadata.sources[1], As400MetadataSourceConfig)


# ---------------------------------------------------------------------------
# 028 — Processing / multi-batch
# ---------------------------------------------------------------------------


class TestProcessingConfig:
    def test_defaults(self) -> None:
        from cmcourier.config.schema import ProcessingConfig

        cfg = ProcessingConfig()
        assert cfg.batches_in_flight == 2

    def test_n_one_accepted(self) -> None:
        from cmcourier.config.schema import ProcessingConfig

        cfg = ProcessingConfig(batches_in_flight=1)
        assert cfg.batches_in_flight == 1

    def test_n_two_accepted(self) -> None:
        from cmcourier.config.schema import ProcessingConfig

        cfg = ProcessingConfig(batches_in_flight=2)
        assert cfg.batches_in_flight == 2

    def test_n_zero_rejected(self) -> None:
        from cmcourier.config.schema import ProcessingConfig

        with pytest.raises(ValidationError):
            ProcessingConfig(batches_in_flight=0)

    def test_n_three_rejected_pointing_to_followup(self) -> None:
        """N>=3 is documented as a future change (see spec 028)."""
        from cmcourier.config.schema import ProcessingConfig

        with pytest.raises(ValidationError):
            ProcessingConfig(batches_in_flight=3)

    def test_pipeline_config_default_factory(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        config = PipelineConfig.model_validate(data)
        assert config.processing.batches_in_flight == 2


# ---------------------------------------------------------------------------
# 034 — AS400 NIARVILOG sync config
# ---------------------------------------------------------------------------


class TestAs400SyncConfig:
    def test_defaults_disabled(self) -> None:
        from cmcourier.config.schema import As400SyncConfig

        cfg = As400SyncConfig()
        assert cfg.enabled is False
        assert cfg.library == "RVILIB"
        assert cfg.table == "NIARVILOG"
        assert cfg.stale_in_progress_minutes == 30
        assert cfg.retry_attempts == 3
        assert cfg.retry_base_delay_s == 5.0
        # When disabled, connection is allowed to be None.
        assert cfg.connection is None

    def test_enabled_requires_connection(self) -> None:
        from cmcourier.config.schema import As400SyncConfig

        with pytest.raises(ValidationError) as ei:
            As400SyncConfig(enabled=True, connection=None)
        assert "connection" in str(ei.value).lower()

    def test_enabled_with_connection_valid(self) -> None:
        from cmcourier.config.schema import As400ConnectionConfig, As400SyncConfig

        cfg = As400SyncConfig(
            enabled=True,
            connection=As400ConnectionConfig(host="10.0.0.1"),
        )
        assert cfg.enabled is True
        assert cfg.connection is not None
        assert cfg.connection.host == "10.0.0.1"

    def test_stale_minutes_out_of_range_rejected(self) -> None:
        from cmcourier.config.schema import As400SyncConfig

        with pytest.raises(ValidationError):
            As400SyncConfig(stale_in_progress_minutes=0)
        with pytest.raises(ValidationError):
            As400SyncConfig(stale_in_progress_minutes=2000)

    def test_retry_policy_out_of_range_rejected(self) -> None:
        from cmcourier.config.schema import As400SyncConfig

        with pytest.raises(ValidationError):
            As400SyncConfig(retry_attempts=0)
        with pytest.raises(ValidationError):
            As400SyncConfig(retry_base_delay_s=0.0)

    def test_tracking_config_has_as400_sync(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        config = PipelineConfig.model_validate(data)
        assert config.tracking.as400_sync.enabled is False

    def test_invalid_library_identifier_rejected(self) -> None:
        from cmcourier.config.schema import As400SyncConfig

        with pytest.raises(ValidationError):
            As400SyncConfig(library="MI BIB")  # space
        with pytest.raises(ValidationError):
            As400SyncConfig(table="NIARVILOG; DROP TABLE X")  # injection attempt

    def test_valid_custom_library_table(self) -> None:
        from cmcourier.config.schema import As400SyncConfig

        cfg = As400SyncConfig(library="MIBIB", table="MININARVILOG")
        assert cfg.library == "MIBIB"
        assert cfg.table == "MININARVILOG"

    # ----- 096: modo de sincronización (claim vs periodic) -----

    def test_mode_defaults_to_claim(self) -> None:
        # 096: el default preserva el claim atómico por-doc pre-096.
        from cmcourier.config.schema import As400SyncConfig

        cfg = As400SyncConfig()
        assert cfg.mode == "claim"
        assert cfg.periodic is None

    def test_mode_rejects_unknown_value(self) -> None:
        from cmcourier.config.schema import As400SyncConfig

        with pytest.raises(ValidationError):
            As400SyncConfig(mode="continuous")  # type: ignore[arg-type]

    def test_mode_periodic_requires_periodic_block(self) -> None:
        # 096: mode=periodic sin el bloque `periodic` es inválido.
        from cmcourier.config.schema import As400SyncConfig

        with pytest.raises(ValidationError) as ei:
            As400SyncConfig(mode="periodic", periodic=None)
        assert "periodic" in str(ei.value).lower()

    def test_mode_periodic_with_block_valid(self) -> None:
        from cmcourier.config.schema import As400SyncConfig, PeriodicSyncConfig

        cfg = As400SyncConfig(mode="periodic", periodic=PeriodicSyncConfig())
        assert cfg.mode == "periodic"
        assert cfg.periodic is not None
        assert cfg.periodic.interval_minutes == 5  # default

    def test_periodic_interval_minutes_range(self) -> None:
        from cmcourier.config.schema import PeriodicSyncConfig

        with pytest.raises(ValidationError):
            PeriodicSyncConfig(interval_minutes=0)
        with pytest.raises(ValidationError):
            PeriodicSyncConfig(interval_minutes=2000)
        assert PeriodicSyncConfig(interval_minutes=15).interval_minutes == 15


# ---------------------------------------------------------------------------
# 049 — NiarvilogColumnsModel
# ---------------------------------------------------------------------------


class TestNiarvilogColumnsModel:
    def test_defaults_are_canonical_names(self) -> None:
        from cmcourier.config.schema import NiarvilogColumnsModel

        cols = NiarvilogColumnsModel()
        assert cols.status_column == "STSCOD"
        assert cols.txn_num_column == "TRNNUM"
        assert cols.cm_object_id_column == "OBJIDN"
        assert cols.error_message_column == "EERRMSG"

    def test_partial_override_keeps_other_defaults(self) -> None:
        from cmcourier.config.schema import NiarvilogColumnsModel

        cols = NiarvilogColumnsModel(status_column="ESTADO", txn_num_column="NUMTRX")
        assert cols.status_column == "ESTADO"
        assert cols.txn_num_column == "NUMTRX"
        # untouched fields keep canonical defaults
        assert cols.cm_object_id_column == "OBJIDN"

    def test_as400_sync_carries_columns(self) -> None:
        from cmcourier.config.schema import As400SyncConfig, NiarvilogColumnsModel

        cfg = As400SyncConfig(columns=NiarvilogColumnsModel(status_column="ESTADO"))
        assert cfg.columns.status_column == "ESTADO"
        # default when omitted
        assert As400SyncConfig().columns.status_column == "STSCOD"

    @pytest.mark.parametrize(
        "bad",
        [
            "ESTA DO",  # space
            "STSCOD;",  # statement terminator
            "1STSCOD",  # leading digit
            "ST'SCOD",  # quote
            "A" * 129,  # too long
            "",  # empty
        ],
    )
    def test_invalid_identifier_rejected(self, bad: str) -> None:
        from cmcourier.config.schema import NiarvilogColumnsModel

        with pytest.raises(ValidationError):
            NiarvilogColumnsModel(status_column=bad)

    def test_db2_special_letters_accepted(self) -> None:
        from cmcourier.config.schema import NiarvilogColumnsModel

        # @, #, $ are valid DB2 identifier letters.
        cols = NiarvilogColumnsModel(status_column="ST#COD", idcm_column="$IDNBAC")
        assert cols.status_column == "ST#COD"
        assert cols.idcm_column == "$IDNBAC"


# ---------------------------------------------------------------------------
# MappingConfig two-mode (035): consolidated CSV vs split MapeoRVI+MetadatosCM
# ---------------------------------------------------------------------------


class TestMappingConfigModes:
    @pytest.fixture
    def split_paths(self, tmp_path: Path) -> dict[str, Path]:
        rvi_cm = tmp_path / "MapeoRVI_CM.csv"
        rvi_cm.write_text("IDSistema,IDRVI,IDCM,IDClaseDocumental,CMISType\n")
        metadatos = tmp_path / "MetadatosCM.csv"
        metadatos.write_text("IDCorto,Metadato,Requerido\n")
        return {"rvi_cm": rvi_cm, "metadatos": metadatos}

    def test_consolidated_mode_only(self, fixture_paths: dict[str, Path]) -> None:
        cfg = MappingConfig(csv_path=fixture_paths["modelo"])
        assert cfg.csv_path == fixture_paths["modelo"]
        assert cfg.rvi_cm_csv_path is None
        assert cfg.metadatos_csv_path is None
        # New defaults exposed by 035 (consolidated mode keeps "CMISType").
        assert cfg.cmis_type_column == "CMISType"

    def test_split_mode_both_paths(self, split_paths: dict[str, Path]) -> None:
        cfg = MappingConfig(
            rvi_cm_csv_path=split_paths["rvi_cm"],
            metadatos_csv_path=split_paths["metadatos"],
        )
        assert cfg.csv_path is None
        assert cfg.rvi_cm_csv_path == split_paths["rvi_cm"]
        assert cfg.metadatos_csv_path == split_paths["metadatos"]
        # Split-mode column defaults match the real bank headers.
        assert cfg.rvi_cm_id_rvi_column == "IDRVI"
        assert cfg.rvi_cm_id_cm_column == "IDCM"
        assert cfg.rvi_cm_clase_id_column == "IDClaseDocumental"
        assert cfg.rvi_cm_cmis_type_column == "CMISType"
        # 038: new optional columns default to "CMISFolder" / "CMISPropertyId".
        assert cfg.rvi_cm_cmis_folder_column == "CMISFolder"
        assert cfg.metadatos_id_corto_column == "IDCorto"
        assert cfg.metadatos_metadata_column == "Metadato"
        assert cfg.metadatos_required_column == "Requerido"
        assert cfg.metadatos_cmis_property_id_column == "CMISPropertyId"
        assert cfg.required_marker == "Yes"

    def test_rejects_both_modes(
        self, fixture_paths: dict[str, Path], split_paths: dict[str, Path]
    ) -> None:
        with pytest.raises(ValidationError) as ei:
            MappingConfig(
                csv_path=fixture_paths["modelo"],
                rvi_cm_csv_path=split_paths["rvi_cm"],
                metadatos_csv_path=split_paths["metadatos"],
            )
        assert (
            "both consolidated and split" in str(ei.value).lower()
            or "either" in str(ei.value).lower()
        )

    def test_rejects_neither_mode(self) -> None:
        with pytest.raises(ValidationError) as ei:
            MappingConfig()
        msg = str(ei.value).lower()
        assert "csv_path" in msg or "rvi_cm_csv_path" in msg or "mapping" in msg

    def test_rejects_partial_split_only_rvi(self, split_paths: dict[str, Path]) -> None:
        with pytest.raises(ValidationError) as ei:
            MappingConfig(rvi_cm_csv_path=split_paths["rvi_cm"])
        msg = str(ei.value).lower()
        assert "metadatos_csv_path" in msg or "split" in msg

    def test_rejects_partial_split_only_metadatos(self, split_paths: dict[str, Path]) -> None:
        with pytest.raises(ValidationError) as ei:
            MappingConfig(metadatos_csv_path=split_paths["metadatos"])
        msg = str(ei.value).lower()
        assert "rvi_cm_csv_path" in msg or "split" in msg

    def test_pipeline_loads_with_split_mapping(
        self,
        fixture_paths: dict[str, Path],
        split_paths: dict[str, Path],
        tmp_path: Path,
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],  # placeholder, will be overridden
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["mapping"] = {
            "rvi_cm_csv_path": str(split_paths["rvi_cm"]),
            "metadatos_csv_path": str(split_paths["metadatos"]),
        }
        config = PipelineConfig.model_validate(data)
        assert config.mapping.rvi_cm_csv_path == split_paths["rvi_cm"]
        assert config.mapping.metadatos_csv_path == split_paths["metadatos"]
        assert config.mapping.csv_path is None


# ---------------------------------------------------------------------------
# HeavyLightLanesConfig (036 — POST-MVP §1 dual-lane upload)
# ---------------------------------------------------------------------------


class TestHeavyLightLanesConfig:
    def test_defaults_disabled(self) -> None:
        cfg = HeavyLightLanesConfig()
        assert cfg.enabled is False
        assert cfg.heavy_threshold_bytes == 10 * 1024 * 1024
        assert cfg.heavy_lane_min_batch == 50
        assert cfg.heavy_initial_ratio == 0.2
        assert cfg.rebalance_interval_s == 10.0
        assert cfg.idle_threshold_s == 15.0

    def test_enabled_with_overrides(self) -> None:
        cfg = HeavyLightLanesConfig(
            enabled=True,
            heavy_threshold_bytes=5 * 1024 * 1024,
            heavy_lane_min_batch=20,
            heavy_initial_ratio=0.4,
            rebalance_interval_s=5.0,
            idle_threshold_s=8.0,
        )
        assert cfg.enabled is True
        assert cfg.heavy_threshold_bytes == 5 * 1024 * 1024
        assert cfg.heavy_initial_ratio == 0.4

    def test_threshold_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            HeavyLightLanesConfig(heavy_threshold_bytes=0)
        with pytest.raises(ValidationError):
            HeavyLightLanesConfig(heavy_threshold_bytes=-1)

    def test_min_batch_must_be_ge_one(self) -> None:
        with pytest.raises(ValidationError):
            HeavyLightLanesConfig(heavy_lane_min_batch=0)

    @pytest.mark.parametrize("ratio", [-0.1, 1.1, 2.0])
    def test_ratio_must_be_in_unit_interval(self, ratio: float) -> None:
        with pytest.raises(ValidationError):
            HeavyLightLanesConfig(heavy_initial_ratio=ratio)

    @pytest.mark.parametrize("ratio", [0.0, 0.5, 1.0])
    def test_ratio_endpoints_accepted(self, ratio: float) -> None:
        cfg = HeavyLightLanesConfig(heavy_initial_ratio=ratio)
        assert cfg.heavy_initial_ratio == ratio

    def test_intervals_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            HeavyLightLanesConfig(rebalance_interval_s=0.0)
        with pytest.raises(ValidationError):
            HeavyLightLanesConfig(idle_threshold_s=0.0)

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HeavyLightLanesConfig(unknown_knob=42)  # type: ignore[call-arg]

    def test_processing_config_includes_lanes(self) -> None:
        pc = ProcessingConfig()
        assert pc.heavy_light_lanes.enabled is False
        assert isinstance(pc.heavy_light_lanes, HeavyLightLanesConfig)

    def test_pipeline_loads_with_lanes_enabled(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["processing"] = {
            "heavy_light_lanes": {
                "enabled": True,
                "heavy_threshold_bytes": 5_000_000,
                "heavy_lane_min_batch": 30,
                "heavy_initial_ratio": 0.3,
            }
        }
        config = PipelineConfig.model_validate(data)
        assert config.processing.heavy_light_lanes.enabled is True
        assert config.processing.heavy_light_lanes.heavy_threshold_bytes == 5_000_000
        assert config.processing.heavy_light_lanes.heavy_initial_ratio == 0.3


# ---------------------------------------------------------------------------
# MetadataCacheConfig (037 — POST-MVP §9 cross-batch document_cache)
# ---------------------------------------------------------------------------


class TestMetadataCacheConfig:
    def test_defaults_disabled(self) -> None:
        cfg = MetadataCacheConfig()
        assert cfg.enabled is False
        assert cfg.ttl_minutes == 60

    def test_enabled_with_override(self) -> None:
        cfg = MetadataCacheConfig(enabled=True, ttl_minutes=120)
        assert cfg.enabled is True
        assert cfg.ttl_minutes == 120

    @pytest.mark.parametrize("ttl", [0, -1])
    def test_ttl_must_be_positive(self, ttl: int) -> None:
        with pytest.raises(ValidationError):
            MetadataCacheConfig(ttl_minutes=ttl)

    def test_ttl_max_30_days(self) -> None:
        MetadataCacheConfig(ttl_minutes=43200)  # 30 days = OK
        with pytest.raises(ValidationError):
            MetadataCacheConfig(ttl_minutes=43201)

    def test_metadata_config_includes_cache(self) -> None:
        mc = MetadataConfigModel(
            field_sources={
                "BAC_CIF": FieldConfig(
                    sources=[FieldSourceItem(source_type="trigger", lookup_value_column="cif")]
                )
            }
        )
        assert mc.cache.enabled is False
        assert isinstance(mc.cache, MetadataCacheConfig)

    def test_pipeline_loads_with_cache_enabled(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["metadata"]["cache"] = {"enabled": True, "ttl_minutes": 30}
        config = PipelineConfig.model_validate(data)
        assert config.metadata.cache.enabled is True
        assert config.metadata.cache.ttl_minutes == 30


class TestConnectionRegistry:
    """129 — bloque ``connections:`` con alias + referencias por alias."""

    def _data(self, fixture_paths: dict[str, Path], tmp_path: Path) -> dict[str, Any]:
        return _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )

    def test_mssql_connection_defaults(self) -> None:
        cfg = MssqlConnectionConfig(host="sql.test", database="cmcourier")
        assert cfg.kind == "mssql"
        assert cfg.port == 1433
        assert cfg.driver == "ODBC Driver 18 for SQL Server"
        assert cfg.encrypt is True
        assert cfg.trust_server_certificate is False

    def test_as400_connection_carries_kind(self) -> None:
        assert As400ConnectionConfig(host="h").kind == "as400"

    def test_empty_registry_by_default(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        config = PipelineConfig.model_validate(self._data(fixture_paths, tmp_path))
        assert config.connections == {}
        assert config.connection_refs() == ()
        assert config.required_aliases() == ()

    def test_alias_reference_from_indexing_source(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """E1 — `indexing.source.connection: rvi` resuelve contra el registro."""
        data = self._data(fixture_paths, tmp_path)
        data["connections"] = {"rvi": {"kind": "as400", "host": "10.0.0.1"}}
        data["indexing"] = {
            "source": {"kind": "as400", "connection": "rvi", "query": "SELECT 1"},
        }
        config = PipelineConfig.model_validate(data)
        assert config.indexing.source.connection == "rvi"
        (ref,) = config.connection_refs()
        assert ref.alias == "rvi"
        assert ref.kind == "as400"
        assert ref.site == "indexing"
        assert isinstance(ref.spec, As400ConnectionConfig)
        assert ref.spec.host == "10.0.0.1"
        assert config.required_aliases() == ("rvi",)
        assert config.connection_ref("indexing") == ref
        assert config.connection_ref("tracking.as400_sync") is None

    def test_inline_connections_get_implicit_as400_alias(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """E3 — el YAML de siempre: inline en indexing + metadata + sync."""
        data = self._data(fixture_paths, tmp_path)
        data["indexing"] = {
            "source": {"kind": "as400", "connection": {"host": "a"}, "query": "SELECT 1"},
        }
        data["metadata"]["sources"].append(
            {
                "kind": "as400",
                "alias": "clientes",
                "as400_connection": {"host": "b"},
                "table": "CLIENTES",
            }
        )
        data["tracking"]["as400_sync"] = {"enabled": True, "connection": {"host": "c"}}
        config = PipelineConfig.model_validate(data)
        refs = config.connection_refs()
        assert [(r.alias, r.site) for r in refs] == [
            ("as400", "indexing"),
            ("as400", "metadata:clientes"),
            ("as400", "tracking.as400_sync"),
        ]
        assert [r.spec.host for r in refs] == ["a", "b", "c"]
        assert config.required_aliases() == ("as400",)

    def test_metadata_and_sync_sites_accept_aliases(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = self._data(fixture_paths, tmp_path)
        data["connections"] = {
            "rvi": {"kind": "as400", "host": "10.0.0.1"},
            "clientes_sql": {"kind": "mssql", "host": "sql", "database": "cm"},
        }
        data["metadata"]["sources"].append(
            {"kind": "as400", "alias": "clientes", "as400_connection": "rvi", "table": "C"}
        )
        data["tracking"]["as400_sync"] = {"enabled": True, "connection": "rvi"}
        config = PipelineConfig.model_validate(data)
        assert [(r.alias, r.site) for r in config.connection_refs()] == [
            ("rvi", "metadata:clientes"),
            ("rvi", "tracking.as400_sync"),
        ]
        # `clientes_sql` está declarada pero nadie la usa: no es requerida.
        assert config.required_aliases() == ("rvi",)
        assert isinstance(config.connections["clientes_sql"], MssqlConnectionConfig)

    def test_unknown_alias_rejected_naming_site(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """E2 — alias inexistente falla nombrando alias y sitio."""
        data = self._data(fixture_paths, tmp_path)
        data["indexing"] = {
            "source": {"kind": "as400", "connection": "nadie", "query": "SELECT 1"},
        }
        with pytest.raises(ValidationError) as ei:
            PipelineConfig.model_validate(data)
        msg = str(ei.value)
        assert "nadie" in msg
        assert "indexing.source.connection" in msg

    def test_wrong_kind_alias_rejected(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = self._data(fixture_paths, tmp_path)
        data["connections"] = {"sql": {"kind": "mssql", "host": "s", "database": "d"}}
        data["metadata"]["sources"].append(
            {"kind": "as400", "alias": "clientes", "as400_connection": "sql", "table": "C"}
        )
        with pytest.raises(ValidationError) as ei:
            PipelineConfig.model_validate(data)
        msg = str(ei.value)
        assert "metadata.sources[clientes]" in msg
        assert "mssql" in msg and "as400" in msg

    @pytest.mark.parametrize("alias", ["cmis", "Mal-Alias", "1abc", "a" * 33, ""])
    def test_bad_aliases_rejected(
        self, fixture_paths: dict[str, Path], tmp_path: Path, alias: str
    ) -> None:
        data = self._data(fixture_paths, tmp_path)
        data["connections"] = {alias: {"kind": "as400", "host": "h"}}
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(data)

    def test_sync_enabled_with_alias_passes_connection_required_check(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = self._data(fixture_paths, tmp_path)
        data["connections"] = {"rvi": {"kind": "as400", "host": "h"}}
        data["tracking"]["as400_sync"] = {"enabled": True, "connection": "rvi"}
        config = PipelineConfig.model_validate(data)
        ref = config.connection_ref("tracking.as400_sync")
        assert ref is not None and ref.spec.host == "h"


class TestMssqlMetadataSource:
    """130 — fuente de metadata ``kind: mssql`` (siempre por alias del registro)."""

    def _data(self, fixture_paths: dict[str, Path], tmp_path: Path) -> dict[str, Any]:
        data = _build_full_data(
            fixture_paths["trigger"],
            fixture_paths["rvabrep"],
            fixture_paths["modelo"],
            fixture_paths["clients"],
            fixture_paths["assembly_root"],
            tmp_path,
        )
        data["connections"] = {
            "clientes_sql": {"kind": "mssql", "host": "h", "database": "cmcourier"}
        }
        return data

    def test_mssql_source_resolves_to_ref_with_schema_qualified_table(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """E4 — carga y `connection_refs()` trae el sitio `metadata:clientes`."""
        data = self._data(fixture_paths, tmp_path)
        data["metadata"]["sources"].append(
            {
                "kind": "mssql",
                "alias": "clientes",
                "connection": "clientes_sql",
                "table": "dbo.clientes",
            }
        )
        config = PipelineConfig.model_validate(data)
        source = config.metadata.sources[-1]
        assert isinstance(source, MssqlMetadataSourceConfig)
        assert source.table == "dbo.clientes"
        ref = config.connection_ref("metadata:clientes")
        assert ref is not None
        assert (ref.alias, ref.kind) == ("clientes_sql", "mssql")
        assert config.required_aliases() == ("clientes_sql",)

    def test_mssql_source_pointing_to_as400_connection_rejected(
        self, fixture_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        data = self._data(fixture_paths, tmp_path)
        data["connections"]["rvi"] = {"kind": "as400", "host": "h"}
        data["metadata"]["sources"].append(
            {"kind": "mssql", "alias": "clientes", "connection": "rvi", "table": "dbo.c"}
        )
        with pytest.raises(ValidationError) as ei:
            PipelineConfig.model_validate(data)
        msg = str(ei.value)
        assert "metadata.sources[clientes].connection" in msg
        assert "requires kind 'mssql'" in msg

    def test_mssql_source_requires_exactly_one_of_table_or_query(self) -> None:
        with pytest.raises(ValidationError):
            MssqlMetadataSourceConfig(kind="mssql", alias="c", connection="clientes_sql")
        with pytest.raises(ValidationError):
            MssqlMetadataSourceConfig(
                kind="mssql", alias="c", connection="clientes_sql", table="t", query="q"
            )

    def test_mssql_source_has_no_inline_form(self) -> None:
        with pytest.raises(ValidationError):
            MssqlMetadataSourceConfig.model_validate(
                {
                    "kind": "mssql",
                    "alias": "c",
                    "connection": {"kind": "mssql", "host": "h", "database": "d"},
                    "table": "t",
                }
            )

    def test_field_source_accepts_mssql_prefix(self) -> None:
        item = FieldSourceItem(source_type="mssql:clientes", lookup_value_column="Nombre")
        assert item.source_type == "mssql:clientes"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("csv:clients", ("csv", "clients")),
            ("as400:personas", ("as400", "personas")),
            ("mssql:clientes", ("mssql", "clientes")),
            ("trigger", None),
            ("rvabrep", None),
            ("oracle:x", None),
        ],
    )
    def test_split_lookup_source_type(self, value: str, expected: tuple[str, str] | None) -> None:
        assert split_lookup_source_type(value) == expected
