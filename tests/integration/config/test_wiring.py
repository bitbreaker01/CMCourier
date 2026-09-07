"""Integration tests for ``cmcourier.config.wiring.build_pipeline``."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import httpx
import pytest
import respx

from cmcourier.config.loader import Secrets, load_config
from cmcourier.config.wiring import build_pipeline
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.orchestrators.staged import StagedPipeline

pytestmark = pytest.mark.integration

_TESTS_ROOT = Path(__file__).parent.parent.parent
_PIPELINE_FIXTURES = _TESTS_ROOT / "fixtures" / "pipeline"
_SERVICES_FIXTURES = _TESTS_ROOT / "fixtures" / "services"
_ASSEMBLY_FIXTURES = _TESTS_ROOT / "fixtures" / "assembly"

_CMIS_BASE_URL = "http://cmis.example.test:9080/opencmcmis/browser"
_CMIS_REPO_ID = "$x!testrepo"


def _write_yaml(tmp_path: Path, *, triggers_path: Path | None = None) -> Path:
    triggers = triggers_path or (tmp_path / "triggers.csv")
    if not triggers.exists():
        triggers.write_text("ShortName,CIF,SystemID\nTESTCLIENT01,123456,1\n")
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        dedent(
            f"""\
            trigger:
              csv_path: {triggers}
              shortname_column: ShortName
              cif_column: CIF
              system_id_column: SystemID
            indexing:
              source:
                kind: csv
                csv_path: {_PIPELINE_FIXTURES / "rvabrep.csv"}
              columns:
                shortname_column: shortname
                system_id_column: system_id
                delete_code_column: delete_code
                txn_num_column: txn_num
                index2_column: index2
                index3_column: index3
                index4_column: index4
                index5_column: index5
                index6_column: index6
                index7_column: index7
                image_type_column: image_type
                image_path_column: image_path
                file_name_column: file_name
                creation_date_column: creation_date
                last_view_date_column: last_view_date
                total_pages_column: total_pages
            mapping:
              csv_path: {_SERVICES_FIXTURES / "modelo_documental.csv"}
            metadata:
              field_aliases:
                CIF: BAC_CIF
                Nombre_Cliente: BAC_Nombre_Cliente
              field_sources:
                BAC_CIF:
                  sources:
                    - source_type: trigger
                      lookup_value_column: cif
                    - source_type: rvabrep
                      lookup_value_column: index2
                BAC_Nombre_Cliente:
                  sources:
                    - source_type: "csv:clients"
                      lookup_value_column: Nombre_Cliente
                      lookup_key_column: CIF
              sources:
                - alias: clients
                  csv_path: {_SERVICES_FIXTURES / "metadata" / "clients.csv"}
            assembly:
              source_root: {_ASSEMBLY_FIXTURES}
              temp_dir: {tmp_path / "stg"}
            cmis:
              base_url: {_CMIS_BASE_URL}
              repo_id: "{_CMIS_REPO_ID}"
              retry_base_delay_s: 0.0
              retry_max_attempts: 2
            tracking:
              db_path: {tmp_path / "tracking.db"}
            """
        )
    )
    return yaml_path


def _secrets() -> Secrets:
    return Secrets(cmis_username="tester", cmis_password="secret-not-real")


def _register_cmis_for_doc(txn: str) -> None:
    respx.get(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}").mock(
        return_value=httpx.Response(200, json={"repositoryId": _CMIS_REPO_ID, "productName": "IBM"})
    )
    respx.post(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}/root").mock(
        return_value=httpx.Response(201, json={"ok": True})
    )
    respx.post(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}/root/$type/BAC_04_01_01_01_01").mock(
        return_value=httpx.Response(
            201, json={"succinctProperties": {"cmis:objectId": f"cm-{txn}"}}
        )
    )


class TestBuildPipeline:
    @respx.mock
    def test_returns_pipeline_runs_end_to_end(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        config = load_config(yaml_path)
        pipeline = build_pipeline(config, _secrets())
        assert isinstance(pipeline, StagedPipeline)
        _register_cmis_for_doc("TXN_PIPE_001")
        triggers = config.trigger.csv_path
        report = pipeline.run(source_descriptor=str(triggers))
        assert report.s5_done == 1
        assert report.s5_failed == 0

    def test_repeated_calls_produce_independent_pipelines(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        config = load_config(yaml_path)
        p1 = build_pipeline(config, _secrets())
        p2 = build_pipeline(config, _secrets())
        assert p1 is not p2
        assert isinstance(p1, StagedPipeline)
        assert isinstance(p2, StagedPipeline)

    def test_as400_metadata_source_builds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a YAML with an additional as400 metadata source. pyodbc is
        # mocked so the As400DataSource constructor doesn't try to connect
        # (it doesn't — connection is lazy in _connect()).
        yaml_path = _write_yaml(tmp_path)
        text = yaml_path.read_text()
        text = text.replace(
            "  sources:\n    - alias: clients",
            "  sources:\n"
            "    - kind: as400\n"
            "      alias: customers\n"
            "      as400_connection:\n"
            '        host: "10.0.0.1"\n'
            "      table: CUSTOMERS\n"
            "    - alias: clients",
            1,
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        secrets = Secrets(
            cmis_username="tester",
            cmis_password="secret-not-real",
            as400_username="as400tester",
            as400_password="as400secret",
        )
        pipeline = build_pipeline(config, secrets)
        # Inspect MetadataService's registered sources.
        registry = pipeline._metadata_service._sources_registry  # type: ignore[attr-defined]
        assert "customers" in registry
        assert "clients" in registry

    def test_as400_metadata_source_missing_secret_raises(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        text = yaml_path.read_text()
        text = text.replace(
            "  sources:\n    - alias: clients",
            "  sources:\n"
            "    - kind: as400\n"
            "      alias: customers\n"
            "      as400_connection:\n"
            '        host: "10.0.0.1"\n'
            "      table: CUSTOMERS\n"
            "    - alias: clients",
            1,
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        # _secrets() returns AS400 creds empty.
        with pytest.raises(ConfigurationError) as ei:
            build_pipeline(config, _secrets())
        assert ei.value.context["alias"] == "customers"
        assert "AS400_USERNAME" in ei.value.context["missing_vars"]

    def test_as400_metadata_source_with_query_builds(self, tmp_path: Path) -> None:
        # 018: query-mode AS400 metadata source. pyodbc connection is lazy
        # so the constructor doesn't touch the network.
        yaml_path = _write_yaml(tmp_path)
        text = yaml_path.read_text()
        text = text.replace(
            "  sources:\n    - alias: clients",
            "  sources:\n"
            "    - kind: as400\n"
            "      alias: customers\n"
            "      as400_connection:\n"
            '        host: "10.0.0.1"\n'
            "      query: SELECT CIF, NAME FROM CUSTOMERS WHERE ACTIVE = 1\n"
            "    - alias: clients",
            1,
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        secrets = Secrets(
            cmis_username="tester",
            cmis_password="secret-not-real",
            as400_username="as400tester",
            as400_password="as400secret",
        )
        pipeline = build_pipeline(config, secrets)
        registry = pipeline._metadata_service._sources_registry  # type: ignore[attr-defined]
        assert "customers" in registry
        # The adapter's source_expr should wrap the query in a derived-table alias.
        customers_src = registry["customers"]
        assert customers_src._source_expr == (  # type: ignore[attr-defined]
            "(SELECT CIF, NAME FROM CUSTOMERS WHERE ACTIVE = 1) AS T"
        )

    def test_single_doc_without_override_raises(self, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        text = yaml_path.read_text()
        text = text.replace(
            "trigger:\n"
            "  csv_path: " + str(tmp_path / "triggers.csv") + "\n"
            "  shortname_column: ShortName\n"
            "  cif_column: CIF\n"
            "  system_id_column: SystemID",
            "trigger:\n  kind: single_doc",
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        with pytest.raises(ConfigurationError) as ei:
            build_pipeline(config, _secrets())
        assert ei.value.context["kind"] == "single_doc"

    def test_single_doc_with_override_succeeds(self, tmp_path: Path) -> None:
        from cmcourier.services.triggers import SingleDocTriggerStrategy

        yaml_path = _write_yaml(tmp_path)
        text = yaml_path.read_text()
        text = text.replace(
            "trigger:\n"
            "  csv_path: " + str(tmp_path / "triggers.csv") + "\n"
            "  shortname_column: ShortName\n"
            "  cif_column: CIF\n"
            "  system_id_column: SystemID",
            "trigger:\n  kind: single_doc",
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        strategy = SingleDocTriggerStrategy(shortname="TESTCLIENT01", system_id="1", cif="123456")
        pipeline = build_pipeline(config, _secrets(), trigger_strategy_override=strategy)
        assert isinstance(pipeline, StagedPipeline)
        assert pipeline._trigger_strategy is strategy  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# build_mapping_service helper (035): consolidated vs split mode
# ---------------------------------------------------------------------------


class TestBuildMappingService:
    def test_consolidated_mode(self) -> None:
        """Legacy single-CSV mode: uses the consolidated test fixture."""
        from cmcourier.config.schema import MappingConfig
        from cmcourier.config.wiring import build_mapping_service

        cfg = MappingConfig(csv_path=_SERVICES_FIXTURES / "modelo_documental.csv")
        svc = build_mapping_service(cfg)
        assert svc.count() > 0
        # The fixture has FF17 → Autorizacion SMS (verified in
        # tests/unit/services/test_mapping.py).
        m = svc.get_mapping("FF17")
        assert m.id_corto == "PT57"
        assert m.clase_name == "Autorizacion SMS"
        assert m.cmis_type == ""  # consolidated fixture has no CMISType column

    def test_split_mode(self, tmp_path: Path) -> None:
        """Two-CSV mode: MapeoRVI_CM + MetadatosCM joined by IDCM↔IDCorto."""
        from cmcourier.config.schema import MappingConfig
        from cmcourier.config.wiring import build_mapping_service

        rvi_cm = tmp_path / "MapeoRVI_CM.csv"
        rvi_cm.write_text(
            "IDSistema,IDRVI,IDCM,IDClaseDocumental,CMISType\n"
            ",FB01,CN01,01.01.01.01.01,DocCN01\n"
            ",FB23,CN02,01.01.01.01.02,DocCN02\n"
        )
        metadatos = tmp_path / "MetadatosCM.csv"
        metadatos.write_text(
            "IDCorto,Metadato,Requerido\nCN01,CIF,Yes\nCN01,Nombre_Cliente,Yes\nCN02,CIF,Yes\n"
        )
        cfg = MappingConfig(rvi_cm_csv_path=rvi_cm, metadatos_csv_path=metadatos)
        svc = build_mapping_service(cfg)
        assert svc.count() == 2

        m1 = svc.get_mapping("FB01")
        assert m1.clase_id == "01.01.01.01.01"
        assert m1.id_corto == "CN01"
        assert m1.cmis_type == "DocCN01"
        assert m1.clase_name == m1.clase_id  # split mode uses clase_id as name
        assert m1.required_metadata_fields == ("CIF", "Nombre_Cliente")

        m2 = svc.get_mapping("FB23")
        assert m2.cmis_type == "DocCN02"
        assert m2.required_metadata_fields == ("CIF",)


# ---------------------------------------------------------------------------
# 048 — pluggable RVABREP source (_build_rvabrep_source)
# ---------------------------------------------------------------------------


class TestBuildRvabrepSource048:
    """The RVABREP source is built once and feeds both S0 and S1.
    ``csv`` → TabularDataSource; ``as400`` → As400DataSource (query mode)."""

    def test_csv_source_builds_tabular(self, tmp_path: Path) -> None:
        from cmcourier.adapters.sources import TabularDataSource
        from cmcourier.config.wiring import _build_rvabrep_source

        yaml_path = _write_yaml(tmp_path)
        config = load_config(yaml_path)
        src = _build_rvabrep_source(config.indexing, _secrets())
        try:
            assert isinstance(src, TabularDataSource)
        finally:
            src.close()

    def test_as400_source_builds_as400_datasource(self, tmp_path: Path) -> None:
        """AS400 RVABREP source: the operator's query feeds As400DataSource
        in query mode. No live server — As400DataSource construction does
        not connect; the driver-level fake in test_as400.py covers I/O."""
        from cmcourier.adapters.sources import As400DataSource
        from cmcourier.config.wiring import _build_rvabrep_source

        yaml_path = _write_yaml(tmp_path)
        text = yaml_path.read_text()
        text = text.replace(
            "indexing:\n"
            "  source:\n"
            "    kind: csv\n"
            f"    csv_path: {_PIPELINE_FIXTURES / 'rvabrep.csv'}\n",
            "indexing:\n"
            "  source:\n"
            "    kind: as400\n"
            "    connection:\n"
            "      host: as400.bank.test\n"
            '    query: "SELECT * FROM RVILIB.RVABREP"\n',
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        secrets = Secrets(
            cmis_username="t",
            cmis_password="x",
            as400_username="a400",
            as400_password="a400pw",
        )
        src = _build_rvabrep_source(config.indexing, secrets)
        try:
            assert isinstance(src, As400DataSource)
        finally:
            src.close()

    def test_as400_source_missing_secrets_raises(self, tmp_path: Path) -> None:
        from cmcourier.config.wiring import _build_rvabrep_source

        yaml_path = _write_yaml(tmp_path)
        text = yaml_path.read_text()
        text = text.replace(
            "indexing:\n"
            "  source:\n"
            "    kind: csv\n"
            f"    csv_path: {_PIPELINE_FIXTURES / 'rvabrep.csv'}\n",
            "indexing:\n"
            "  source:\n"
            "    kind: as400\n"
            "    connection:\n"
            "      host: as400.bank.test\n"
            '    query: "SELECT * FROM RVILIB.RVABREP"\n',
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        # _secrets() carries no AS400 creds.
        with pytest.raises(ConfigurationError) as ei:
            _build_rvabrep_source(config.indexing, _secrets())
        assert "AS400_USERNAME" in ei.value.context["missing_vars"]


class TestNiarvilogColumnsWiring049:
    """049: tracking.as400_sync.columns reaches the NIARVILOG store."""

    def test_translator_maps_all_fields(self) -> None:
        from cmcourier.config.schema import NiarvilogColumnsModel
        from cmcourier.config.wiring import niarvilog_columns_from_schema

        model = NiarvilogColumnsModel(status_column="ESTADO", txn_num_column="NUMTRX")
        cols = niarvilog_columns_from_schema(model)
        assert cols.status == "ESTADO"
        assert cols.txn_num == "NUMTRX"
        # untouched → canonical defaults
        assert cols.cm_object_id == "OBJIDN"
        assert cols.error_message == "EERRMSG"

    def test_custom_columns_reach_the_store(self, tmp_path: Path) -> None:
        from cmcourier.adapters.tracking import SQLiteTrackingStore
        from cmcourier.config.wiring import _build_idempotency_coordinator

        yaml_path = _write_yaml(tmp_path)
        text = yaml_path.read_text()
        # Append an as400_sync block with custom column names. The written
        # YAML is dedent()'d, so ``tracking:`` sits at column 0.
        text = text.replace(
            f"tracking:\n  db_path: {tmp_path / 'tracking.db'}\n",
            "tracking:\n"
            f"  db_path: {tmp_path / 'tracking.db'}\n"
            "  as400_sync:\n"
            "    enabled: true\n"
            "    library: MIBIB\n"
            "    table: MININARVILOG\n"
            "    connection:\n"
            "      host: as400.bank.test\n"
            "    columns:\n"
            "      status_column: ESTADO\n"
            "      txn_num_column: NUMTRX\n",
        )
        assert "as400_sync:" in text, "replace target did not match"
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        secrets = Secrets(
            cmis_username="t",
            cmis_password="x",
            as400_username="a400",
            as400_password="a400pw",
        )
        sqlite_store = SQLiteTrackingStore(tmp_path / "tracking.db")
        try:
            # 096: _build_idempotency_coordinator devuelve la tupla
            # (coordinator, periodic_reconciler).
            coordinator, _reconciler = _build_idempotency_coordinator(
                config=config, secrets=secrets, sqlite_store=sqlite_store
            )
            assert coordinator is not None
            assert coordinator._as400 is not None
            assert coordinator._as400._cols.status == "ESTADO"
            assert coordinator._as400._cols.txn_num == "NUMTRX"
            assert coordinator._as400._cols.cm_object_id == "OBJIDN"
            assert coordinator._as400._library == "MIBIB"
        finally:
            sqlite_store.close()
