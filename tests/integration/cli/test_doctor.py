"""Tests de integración para ``cmcourier doctor``."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from click.testing import CliRunner

from cmcourier.cli import doctor as doctor_module
from cmcourier.cli.app import main
from cmcourier.cli.doctor import CheckResult, CheckStatus, run_doctor
from cmcourier.config.loader import Credential, Secrets, load_config

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_TESTS_ROOT = Path(__file__).parent.parent.parent
_PIPELINE_FIXTURES = _TESTS_ROOT / "fixtures" / "pipeline"
_SERVICES_FIXTURES = _TESTS_ROOT / "fixtures" / "services"
_ASSEMBLY_FIXTURES = _TESTS_ROOT / "fixtures" / "assembly"

_CMIS_BASE_URL = "http://cmis.example.test:9080/opencmcmis/browser"
_CMIS_REPO_ID = "$x!testrepo"

# El `fixture` del Modelo Documental tiene estos cm_object_types distintos
# después de descartar el duplicado FF17 (first-wins).
_DISTINCT_TYPES: tuple[str, ...] = (
    "$t!-2_BAC_01_02_04_01_01v-1",
    "$t!-2_BAC_02_01_03_01_01v-1",
    "$t!-2_BAC_03_01_01_01_01v-1",
    "$t!-2_BAC_04_01_01_01_01v-1",
    "$t!-2_BAC_05_01_01_01_01v-1",
    "$t!-2_BAC_06_01_01_01_01v-1",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(
    tmp_path: Path,
    *,
    triggers_csv: Path | None = None,
    mapping_csv: Path | None = None,
    clients_csv: Path | None = None,
) -> Path:
    if triggers_csv is None:
        triggers_csv = tmp_path / "triggers.csv"
        triggers_csv.write_text("ShortName,CIF,SystemID\nTESTCLIENT01,123456,1\n")
    if mapping_csv is None:
        mapping_csv = _SERVICES_FIXTURES / "modelo_documental.csv"
    if clients_csv is None:
        clients_csv = _SERVICES_FIXTURES / "metadata" / "clients.csv"
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        dedent(
            f"""\
            trigger:
              csv_path: {triggers_csv}
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
              csv_path: {mapping_csv}
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
                  csv_path: {clients_csv}
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
    return Secrets({"cmis": Credential("tester", "secret-not-real")})


def _stub_warmup_ok() -> None:
    # 060: respx por default no distingue por querystring, pero el flujo del
    # doctor pega contra la MISMA base URL tanto para `cmisselector=repositoryInfo`
    # como para `cmisselector=typeDefinition&typeId=...`. Usamos el kwarg
    # params= así cada `stub` matchea una querystring específica.
    respx.get(
        f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}",
        params={"cmisselector": "repositoryInfo"},
    ).mock(
        return_value=httpx.Response(200, json={"repositoryId": _CMIS_REPO_ID, "productName": "IBM"})
    )


def _stub_type_definitions_ok(types: tuple[str, ...] = _DISTINCT_TYPES) -> None:
    for type_id in types:
        respx.get(
            f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}",
            params={"cmisselector": "typeDefinition", "typeId": type_id},
        ).mock(return_value=httpx.Response(200, json={"id": type_id}))


# ---------------------------------------------------------------------------
# 038 — helpers del modo `split` usados por los tests de cm-targets
# ---------------------------------------------------------------------------


def _write_rvi_cm_csv(
    path: Path,
    rows: list[tuple[str, str, str, str, str]],
) -> None:
    """rows: ``(IDRVI, IDCM, IDClaseDocumental, CMISType, CMISFolder)``."""
    lines = ["IDSistema,IDRVI,IDCM,IDClaseDocumental,CMISType,CMISFolder"]
    for idrvi, idcm, idclase, cmis_type, cmis_folder in rows:
        lines.append(f",{idrvi},{idcm},{idclase},{cmis_type},{cmis_folder}")
    path.write_text("\n".join(lines) + "\n")


def _write_metadatos_csv(
    path: Path,
    rows: list[tuple[str, str, str, str]],
) -> None:
    """rows: ``(IDCorto, Metadato, Requerido, CMISPropertyId)``."""
    lines = ["IDCorto,Metadato,Requerido,CMISPropertyId"]
    for idcorto, meta, req, cmis_prop in rows:
        lines.append(f"{idcorto},{meta},{req},{cmis_prop}")
    path.write_text("\n".join(lines) + "\n")


def _write_split_yaml(
    tmp_path: Path,
    *,
    rvi_cm_csv: Path,
    metadatos_csv: Path,
) -> Path:
    triggers_csv = tmp_path / "triggers.csv"
    triggers_csv.write_text("ShortName,CIF,SystemID\nTESTCLIENT01,123456,1\n")
    clients_csv = _SERVICES_FIXTURES / "metadata" / "clients.csv"
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        dedent(
            f"""\
            trigger:
              csv_path: {triggers_csv}
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
              rvi_cm_csv_path: {rvi_cm_csv}
              metadatos_csv_path: {metadatos_csv}
            metadata:
              field_aliases:
                CIF: BAC_CIF
                Nombre_Cliente: BAC_Nombre_Cliente
              field_sources:
                BAC_CIF:
                  sources:
                    - source_type: trigger
                      lookup_value_column: cif
                BAC_Nombre_Cliente:
                  sources:
                    - source_type: "csv:clients"
                      lookup_value_column: Nombre_Cliente
                      lookup_key_column: CIF
              sources:
                - alias: clients
                  csv_path: {clients_csv}
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


# ---------------------------------------------------------------------------
# Happy path de run_doctor
# ---------------------------------------------------------------------------


class TestRunDoctorHappyPath:
    @respx.mock
    def test_all_checks_pass(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets())
        assert not report.has_failures, [
            f"{r.name}: {r.status.value} — {r.message}"
            for r in report.results
            if r.status == CheckStatus.FAIL
        ]
        # PASS para cada `check` excepto sample_dry_run que depende de
        # `fixtures` del filesystem (también pasa acá).
        assert report.passed_count >= 5

    @respx.mock
    def test_check_order_is_stable(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets())
        names = [r.name for r in report.results]
        assert names == [
            "log_dir_writable",
            "cmis_connectivity",
            "as400_connectivity",
            "mssql_connectivity",  # 130
            "tracking_openable",
            "as400_sync",
            "mapping_completeness",
            "cm_manifest",  # 145
            "metadata_sources",
            "cm_type_alignment",
            "cmis_folders_exist",
            "cmis_properties_alignment",
            "sample_dry_run",
        ]


# ---------------------------------------------------------------------------
# Fallas de `checks` individuales
# ---------------------------------------------------------------------------


class TestCmisFailures:
    @respx.mock
    def test_cmis_unreachable(self, tmp_path: Path) -> None:
        respx.get(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}").mock(
            return_value=httpx.Response(503, json={"error": "boom"})
        )
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets())
        conn_check = next(r for r in report.results if r.name == "cmis_connectivity")
        assert conn_check.status == CheckStatus.FAIL
        # cm_type_alignment tiene que quedar en SKIP, NO crashear.
        align_check = next(r for r in report.results if r.name == "cm_type_alignment")
        assert align_check.status == CheckStatus.SKIP


class TestTrackingFailures:
    @respx.mock
    def test_tracking_db_in_non_creatable_path(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        # Inyecta un db_path no creable (bajo /dev/null que no acepta subdirs).
        broken = config.model_copy(
            update={
                "tracking": config.tracking.model_copy(
                    update={"db_path": Path("/dev/null/cmcourier/tracking.db")}
                )
            }
        )
        report = run_doctor(broken, _secrets())
        track_check = next(r for r in report.results if r.name == "tracking_openable")
        assert track_check.status == CheckStatus.FAIL


class TestMappingWarn:
    @respx.mock
    def test_empty_mapping_warns(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        # CSV de mapping vacío (solo header).
        empty_mapping = tmp_path / "empty_mapping.csv"
        empty_mapping.write_text("ID CLASE DOCUMENTAL,ID RVI,ID Corto,CLASE DOCUMENTAL,METADATOS\n")
        # No hacen falta `stubs` de type-definition porque alignment ve cero tipos.
        config = load_config(_write_yaml(tmp_path, mapping_csv=empty_mapping))
        report = run_doctor(config, _secrets())
        m_check = next(r for r in report.results if r.name == "mapping_completeness")
        assert m_check.status == CheckStatus.WARN
        assert m_check.details["mapping_count"] == "0"


class TestMetadataWarn:
    @respx.mock
    def test_empty_metadata_source_warns(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        empty_clients = tmp_path / "empty_clients.csv"
        empty_clients.write_text("CIF,Nombre_Cliente,Tipo_Cliente\n")
        config = load_config(_write_yaml(tmp_path, clients_csv=empty_clients))
        report = run_doctor(config, _secrets())
        md_check = next(r for r in report.results if r.name == "metadata_sources")
        assert md_check.status == CheckStatus.WARN
        assert "clients" in md_check.details["empty_aliases"]


class TestCmTypeMissing:
    @respx.mock
    def test_missing_type_fails(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        # Registra solo el PRIMER tipo; los otros van a 404.
        # Registramos solo el PRIMER tipo como 200; el resto en 404,
        # distinguidos por el query param typeId así la ruta del warmup
        # queda limpia.
        respx.get(
            f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}",
            params={"cmisselector": "typeDefinition", "typeId": _DISTINCT_TYPES[0]},
        ).mock(return_value=httpx.Response(200, json={"id": _DISTINCT_TYPES[0]}))
        for type_id in _DISTINCT_TYPES[1:]:
            respx.get(
                f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}",
                params={"cmisselector": "typeDefinition", "typeId": type_id},
            ).mock(return_value=httpx.Response(404, json={"error": "not found"}))
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets())
        align = next(r for r in report.results if r.name == "cm_type_alignment")
        assert align.status == CheckStatus.FAIL
        # Todos menos el primer tipo están ausentes.
        for type_id in _DISTINCT_TYPES[1:]:
            assert type_id in align.details["missing_types"]


class TestLogDirWritable:
    @respx.mock
    def test_writable_dir_passes(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets())
        check = next(r for r in report.results if r.name == "log_dir_writable")
        assert check.status == CheckStatus.PASS

    def test_unwritable_dir_fails(self, tmp_path: Path) -> None:
        # Apunta ``log_dir`` a un archivo regular existente. ``Path.mkdir(
        # parents=True, exist_ok=True)`` levanta ``FileExistsError``
        # (subclase de ``OSError``) cuando el último componente no es un
        # directorio, tanto en POSIX como en Windows — no hace falta un
        # path no-escribible específico de la plataforma estilo
        # ``/proc/1/...``.
        blocker = tmp_path / "log_dir_is_a_file"
        blocker.write_text("not a directory")
        yaml_path = _write_yaml(tmp_path)
        text = yaml_path.read_text()
        text += f"\nobservability:\n  log_dir: {blocker}\n"
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        report = run_doctor(config, _secrets())
        check = next(r for r in report.results if r.name == "log_dir_writable")
        assert check.status == CheckStatus.FAIL


class TestAs400Connectivity:
    @respx.mock
    def test_skips_when_source_is_csv(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets())
        check = next(r for r in report.results if r.name == "as400_connectivity")
        assert check.status == CheckStatus.SKIP
        # 129: el check mira el registro completo, no sólo indexing.source.
        assert check.details["reason"] == "no as400 connections in config"


class TestAs400Sync:
    """034: `check` del doctor para el sync de AS400 NIARVILOG."""

    @respx.mock
    def test_skips_when_disabled(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        # La config por default tiene as400_sync.enabled=false.
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets())
        check = next(r for r in report.results if r.name == "as400_sync")
        assert check.status == CheckStatus.SKIP
        assert "disabled" in check.details.get("reason", "")


class TestSampleDryRun:
    @respx.mock
    def test_dry_run_pass(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets())
        dry = next(r for r in report.results if r.name == "sample_dry_run")
        assert dry.status == CheckStatus.PASS
        assert dry.details["stages"] == "S1,S2,S3,S4"

    @respx.mock
    def test_dry_run_skip_on_empty_triggers(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        empty_triggers = tmp_path / "empty_triggers.csv"
        empty_triggers.write_text("ShortName,CIF,SystemID\n")
        config = load_config(_write_yaml(tmp_path, triggers_csv=empty_triggers))
        report = run_doctor(config, _secrets())
        dry = next(r for r in report.results if r.name == "sample_dry_run")
        assert dry.status == CheckStatus.SKIP
        assert dry.details["reason"] == "no_triggers"

    @respx.mock
    def test_dry_run_skip_on_single_doc_kind(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        yaml_path = _write_yaml(tmp_path)
        # Reemplaza el bloque de trigger con kind=single_doc.
        text = yaml_path.read_text()
        text = text.replace(
            f"trigger:\n  csv_path: {tmp_path / 'triggers.csv'}\n",
            "trigger:\n  kind: single_doc\n",
            1,
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        report = run_doctor(config, _secrets())
        dry = next(r for r in report.results if r.name == "sample_dry_run")
        assert dry.status == CheckStatus.SKIP
        assert dry.details["reason"] == "trigger_kind_single_doc_requires_cli_args"


# ---------------------------------------------------------------------------
# Integración CLI
# ---------------------------------------------------------------------------


class TestCli:
    @respx.mock
    def test_doctor_exit_0_happy_path(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "secret-not-real")
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        yaml_path = _write_yaml(tmp_path)
        result = cli_runner.invoke(main, ["doctor", "--config", str(yaml_path)])
        assert result.exit_code == 0, result.stderr
        assert "[PASS]" in result.stdout
        assert "0 failed" in result.stdout

    def test_registry_alias_credentials_reach_the_check(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """130 (bug de 129): el CLI debe cargar los secrets CON la config, si
        no los alias del registro nunca reciben sus env vars."""
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "secret-not-real")
        monkeypatch.setenv("CLIENTES_SQL_USERNAME", "sa")
        monkeypatch.setenv("CLIENTES_SQL_PASSWORD", "not-real")
        probed: list[str] = []
        fake = MagicMock()
        fake.query = MagicMock(side_effect=lambda sql, params: probed.append(sql))
        monkeypatch.setattr(doctor_module, "MssqlDataSource", lambda **kw: fake)
        yaml_path = _write_yaml(tmp_path)
        original = yaml_path.read_text()
        assert original.count("\n  sources:\n") == 1
        text = original.replace(
            "\n  sources:\n",
            "\n  sources:\n"
            "    - kind: mssql\n"
            "      alias: clientes\n"
            "      connection: clientes_sql\n"
            "      table: dbo.clientes\n",
            1,
        )
        yaml_path.write_text(
            "connections:\n  clientes_sql:\n    kind: mssql\n    host: sql.test\n"
            "    database: cmcourier\n" + text
        )
        result = cli_runner.invoke(
            main, ["doctor", "--config", str(yaml_path), "--check", "mssql_connectivity"]
        )
        assert result.exit_code == 0, result.stdout
        assert "credentials missing" not in result.stdout
        # 143: la prueba se deriva del sitio (metadata:clientes → su tabla).
        assert probed == ["SELECT TOP 1 1 FROM dbo.clientes"], result.stdout

    def test_doctor_missing_config_exit_2(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "secret-not-real")
        result = cli_runner.invoke(main, ["doctor", "--config", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 2

    @respx.mock
    def test_doctor_exit_1_on_failure(
        self,
        cli_runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "secret-not-real")
        respx.get(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}").mock(
            return_value=httpx.Response(503, json={"error": "boom"})
        )
        yaml_path = _write_yaml(tmp_path)
        result = cli_runner.invoke(main, ["doctor", "--config", str(yaml_path)])
        assert result.exit_code == 1
        assert "[FAIL]" in result.stdout


# ---------------------------------------------------------------------------
# doctor --check (022)
# ---------------------------------------------------------------------------


class TestDoctorCheckFilter:
    @respx.mock
    def test_connections_runs_only_connection_checks(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets(), selected="connections")
        names = sorted(r.name for r in report.results)
        assert names == sorted(
            [
                "log_dir_writable",
                "cmis_connectivity",
                "as400_connectivity",
                "mssql_connectivity",  # 130
                "tracking_openable",
                "as400_sync",  # 126: pasa al grupo connections (SKIP si sync off)
            ]
        )

    def test_single_check_by_name(self, tmp_path: Path) -> None:
        """126 E1: `selected` acepta el nombre exacto de un check."""
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets(), selected="log_dir_writable")
        assert [r.name for r in report.results] == ["log_dir_writable"]

    def test_check_names_and_group_of(self) -> None:
        from cmcourier.cli.doctor import CHECK_NAMES, group_of

        assert "log_dir_writable" in CHECK_NAMES
        assert "as400_sync" in CHECK_NAMES
        assert len(CHECK_NAMES) == len(set(CHECK_NAMES))
        assert group_of("cmis_folders_exist") == "cm-targets"
        assert group_of("log_dir_writable") == "connections"

    def test_cli_accepts_check_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """126 REQ-002: --check acepta nombres de check, no solo grupos."""
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "secret-not-real")
        result = CliRunner().invoke(
            main, ["doctor", "--config", str(_write_yaml(tmp_path)), "--check", "log_dir_writable"]
        )
        assert result.exit_code == 0, result.output
        assert "log_dir_writable" in result.output
        assert "cmis_connectivity" not in result.output

    @respx.mock
    def test_mapping_runs_the_mapping_group(self, tmp_path: Path) -> None:
        """145: `cm_manifest` se sumo al grupo (offline, SKIP fuera de modo manifest)."""
        _stub_warmup_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets(), selected="mapping")
        names = [r.name for r in report.results]
        assert names == ["mapping_completeness", "cm_manifest"]

    @respx.mock
    def test_metadata_runs_metadata_sources_and_dry_run(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets(), selected="metadata")
        names = sorted(r.name for r in report.results)
        assert names == sorted(["metadata_sources", "sample_dry_run"])

    @respx.mock
    def test_cm_types_runs_only_cm_type_alignment(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets(), selected="cm-types")
        names = [r.name for r in report.results]
        assert names == ["cm_type_alignment"]

    @respx.mock
    def test_all_runs_every_check(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets(), selected="all")
        names = sorted(r.name for r in report.results)
        assert names == sorted(
            [
                "log_dir_writable",
                "cmis_connectivity",
                "as400_connectivity",
                "mssql_connectivity",  # 130
                "tracking_openable",
                "as400_sync",
                "mapping_completeness",
                "cm_manifest",  # 145
                "metadata_sources",
                "cm_type_alignment",
                "cmis_folders_exist",
                "cmis_properties_alignment",
                "sample_dry_run",
            ]
        )

    def test_cli_help_shows_check_flag(self, cli_runner: CliRunner) -> None:
        result = cli_runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "--check" in result.stdout
        assert "connections" in result.stdout

    def test_cli_check_unknown_value_rejected(self, cli_runner: CliRunner, tmp_path: Path) -> None:
        yaml_path = _write_yaml(tmp_path)
        result = cli_runner.invoke(main, ["doctor", "--config", str(yaml_path), "--check", "bogus"])
        assert result.exit_code == 2  # validación de Click


# ---------------------------------------------------------------------------
# 038 — warning de startup por unmask_pii
# ---------------------------------------------------------------------------


class TestUnmaskPiiWarning:
    @respx.mock
    def test_warning_emitted_when_unmask_pii_true(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        yaml_path = _write_yaml(tmp_path)
        # Agrega el flag de unmask al final del YAML.
        yaml_path.write_text(
            yaml_path.read_text()
            + "observability:\n  log_dir: "
            + str(tmp_path / "logs")
            + "\n"
            + "  unmask_pii: true\n"
        )
        config = load_config(yaml_path)
        report = run_doctor(config, _secrets())
        warning = next(
            (r for r in report.results if r.name == "unmask_pii_active"),
            None,
        )
        assert warning is not None
        assert warning.status == CheckStatus.WARN

    @respx.mock
    def test_no_warning_when_default(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        _stub_type_definitions_ok()
        config = load_config(_write_yaml(tmp_path))
        report = run_doctor(config, _secrets())
        names = [r.name for r in report.results]
        assert "unmask_pii_active" not in names


# ---------------------------------------------------------------------------
# 038 — cmis_folders_exist (grupo cm-targets)
# ---------------------------------------------------------------------------


class TestCmisFoldersExist:
    @respx.mock
    def test_skip_when_no_cmis_folder_populated(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        rvi_cm = tmp_path / "rvi_cm.csv"
        metadatos = tmp_path / "metadatos.csv"
        # Sin valores de CMISFolder cargados (todos vacíos).
        _write_rvi_cm_csv(
            rvi_cm,
            [
                ("FB01", "CN01", "01.01.01.01.01", "", ""),
            ],
        )
        _write_metadatos_csv(metadatos, [("CN01", "CIF", "Yes", "")])
        _stub_type_definitions_ok(("$t!-2_BAC_01_01_01_01_01v-1",))
        config = load_config(
            _write_split_yaml(tmp_path, rvi_cm_csv=rvi_cm, metadatos_csv=metadatos)
        )
        report = run_doctor(config, _secrets(), selected="cm-targets")
        folders = next(r for r in report.results if r.name == "cmis_folders_exist")
        assert folders.status == CheckStatus.SKIP
        assert "no CMISFolder" in folders.message.lower() or "nothing to verify" in folders.message

    @respx.mock
    def test_pass_when_all_folders_exist(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        rvi_cm = tmp_path / "rvi_cm.csv"
        metadatos = tmp_path / "metadatos.csv"
        _write_rvi_cm_csv(
            rvi_cm,
            [
                ("FB01", "CN01", "01.01.01.01.01", "", "/cmcourier-staging/CN01"),
            ],
        )
        _write_metadatos_csv(metadatos, [("CN01", "CIF", "Yes", "")])
        # Stub: verify_folder_exists para /cmcourier-staging/CN01 devuelve true.
        respx.get(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}/root/cmcourier-staging/CN01").mock(
            return_value=httpx.Response(
                200, json={"properties": {"cmis:baseTypeId": {"value": "cmis:folder"}}}
            )
        )
        _stub_type_definitions_ok(("$t!-2_BAC_01_01_01_01_01v-1",))
        config = load_config(
            _write_split_yaml(tmp_path, rvi_cm_csv=rvi_cm, metadatos_csv=metadatos)
        )
        report = run_doctor(config, _secrets(), selected="cm-targets")
        folders = next(r for r in report.results if r.name == "cmis_folders_exist")
        assert folders.status == CheckStatus.PASS, f"{folders.message} / {folders.details}"

    @respx.mock
    def test_fail_when_folder_missing(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        rvi_cm = tmp_path / "rvi_cm.csv"
        metadatos = tmp_path / "metadatos.csv"
        _write_rvi_cm_csv(
            rvi_cm,
            [
                ("FB01", "CN01", "01.01.01.01.01", "", "/cmcourier-staging/MISSING"),
            ],
        )
        _write_metadatos_csv(metadatos, [("CN01", "CIF", "Yes", "")])
        respx.get(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}/root/cmcourier-staging/MISSING").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        _stub_type_definitions_ok(("$t!-2_BAC_01_01_01_01_01v-1",))
        config = load_config(
            _write_split_yaml(tmp_path, rvi_cm_csv=rvi_cm, metadatos_csv=metadatos)
        )
        report = run_doctor(config, _secrets(), selected="cm-targets")
        folders = next(r for r in report.results if r.name == "cmis_folders_exist")
        assert folders.status == CheckStatus.FAIL
        assert "/cmcourier-staging/MISSING" in folders.details["missing_folders"]


# ---------------------------------------------------------------------------
# 038 — cmis_properties_alignment (grupo cm-targets)
# ---------------------------------------------------------------------------


class TestCmisPropertiesAlignment:
    @respx.mock
    def test_skip_when_no_property_catalog(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        rvi_cm = tmp_path / "rvi_cm.csv"
        metadatos = tmp_path / "metadatos.csv"
        _write_rvi_cm_csv(rvi_cm, [("FB01", "CN01", "01.01.01.01.01", "", "")])
        _write_metadatos_csv(metadatos, [("CN01", "CIF", "Yes", "")])
        _stub_type_definitions_ok(("$t!-2_BAC_01_01_01_01_01v-1",))
        config = load_config(
            _write_split_yaml(tmp_path, rvi_cm_csv=rvi_cm, metadatos_csv=metadatos)
        )
        report = run_doctor(config, _secrets(), selected="cm-targets")
        props = next(r for r in report.results if r.name == "cmis_properties_alignment")
        assert props.status == CheckStatus.SKIP

    @respx.mock
    def test_pass_when_all_align(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        rvi_cm = tmp_path / "rvi_cm.csv"
        metadatos = tmp_path / "metadatos.csv"
        # cmis_type=D:cmcourier:bacDoc pisa al tipo derivado.
        _write_rvi_cm_csv(
            rvi_cm,
            [("FB01", "CN01", "01.01.01.01.01", "D:cmcourier:bacDoc", "")],
        )
        _write_metadatos_csv(
            metadatos,
            [
                ("CN01", "CIF", "Yes", "cmcourier:BAC_CIF"),
                ("CN01", "Nombre_Cliente", "Yes", "cmcourier:Nombre_Cliente"),
            ],
        )
        # La respuesta de typeDefinition declara ambas propiedades.
        respx.get(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "D:cmcourier:bacDoc",
                    "propertyDefinitions": {
                        "cmcourier:BAC_CIF": {"id": "cmcourier:BAC_CIF"},
                        "cmcourier:Nombre_Cliente": {"id": "cmcourier:Nombre_Cliente"},
                    },
                },
            )
        )
        config = load_config(
            _write_split_yaml(tmp_path, rvi_cm_csv=rvi_cm, metadatos_csv=metadatos)
        )
        report = run_doctor(config, _secrets(), selected="cm-targets")
        props = next(r for r in report.results if r.name == "cmis_properties_alignment")
        assert props.status == CheckStatus.PASS, f"{props.message} / {props.details}"

    @respx.mock
    def test_fail_when_property_missing(self, tmp_path: Path) -> None:
        _stub_warmup_ok()
        rvi_cm = tmp_path / "rvi_cm.csv"
        metadatos = tmp_path / "metadatos.csv"
        _write_rvi_cm_csv(
            rvi_cm,
            [("FB01", "CN01", "01.01.01.01.01", "D:cmcourier:bacDoc", "")],
        )
        _write_metadatos_csv(
            metadatos,
            [
                ("CN01", "CIF", "Yes", "cmcourier:BAC_CIF"),
                ("CN01", "Bogus_Field", "Yes", "cmcourier:DoesNotExist"),
            ],
        )
        # typeDefinition declara solo una de las dos propiedades catalogadas.
        respx.get(f"{_CMIS_BASE_URL}/{_CMIS_REPO_ID}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "D:cmcourier:bacDoc",
                    "propertyDefinitions": {
                        "cmcourier:BAC_CIF": {"id": "cmcourier:BAC_CIF"},
                    },
                },
            )
        )
        config = load_config(
            _write_split_yaml(tmp_path, rvi_cm_csv=rvi_cm, metadatos_csv=metadatos)
        )
        report = run_doctor(config, _secrets(), selected="cm-targets")
        props = next(r for r in report.results if r.name == "cmis_properties_alignment")
        assert props.status == CheckStatus.FAIL
        assert "cmcourier:DoesNotExist" in props.details["missing"]


# ---------------------------------------------------------------------------
# 145 REQ-005 — cm_manifest (grupo mapping, OFFLINE)
# ---------------------------------------------------------------------------


def _write_manifest_json(
    path: Path,
    *,
    decisions: dict[str, str] | None = None,
    reviewed: bool = True,
) -> None:
    """Manifest minimo con UN tipo (CN01) y una propiedad escribible."""
    prop = {
        "id": "cmcourier:BAC_CIF",
        "display_name": "CIF",
        "property_type": "string",
        "cardinality": "single",
        "updatability": "readwrite",
        "required": True,
        "max_length": None,
        "default_value": None,
        "inherited": False,
        "choices": [],
    }
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "service_url": _CMIS_BASE_URL,
                "repository_id": _CMIS_REPO_ID,
                "discovered_at": "2026-01-01T00:00:00Z",
                "types": {
                    "CN01": {
                        "type_id": "$t!-2_BAC_CN01v-1",
                        "local_name": "BAC_CN01",
                        "display_name": "CN01 - Clase",
                        "folder": "/$type/BAC_CN01",
                        "folder_source": "derivada",
                        "folder_ok": True,
                        "properties": [prop],
                        "decisions": decisions or {"cmcourier:BAC_CIF": "usar"},
                        "reviewed": reviewed,
                        "changes": [],
                        "missing_on_server": False,
                    }
                },
                "without_code": [],
            },
            indent=2,
        )
        + "\n"
    )


def _write_rvi_cm_3col(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """145 REQ-001: `MapeoRVI_CM.csv` reducido a `IDSistema,IDRVI,IDCM`."""
    lines = ["IDSistema,IDRVI,IDCM"]
    lines.extend(f"{sistema},{id_rvi},{id_cm}" for sistema, id_rvi, id_cm in rows)
    path.write_text("\n".join(lines) + "\n")


def _write_manifest_yaml(tmp_path: Path, *, rvi_cm_csv: Path, manifest_json: Path) -> Path:
    """El YAML del modo manifest: MapeoRVI_CM de 3 columnas + JSON de tipos."""
    yaml_path = _write_split_yaml(tmp_path, rvi_cm_csv=rvi_cm_csv, metadatos_csv=rvi_cm_csv)
    yaml_path.write_text(
        yaml_path.read_text().replace(
            f"metadatos_csv_path: {rvi_cm_csv}",
            f"type_manifest_path: {manifest_json}",
        )
    )
    return yaml_path


class TestCmManifestCheck:
    def _run(self, tmp_path: Path, yaml_path: Path) -> CheckResult:
        report = run_doctor(load_config(yaml_path), _secrets(), selected="cm_manifest")
        return next(r for r in report.results if r.name == "cm_manifest")

    def test_skip_outside_manifest_mode(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _write_yaml(tmp_path))
        assert result.status == CheckStatus.SKIP

    def test_pass_when_aligned(self, tmp_path: Path) -> None:
        """El YAML de `_write_split_yaml` declara BAC_CIF; el manifest lo usa."""
        rvi_cm = tmp_path / "MapeoRVI_CM.csv"
        manifest = tmp_path / "cm-types.json"
        _write_rvi_cm_3col(rvi_cm, [("", "FB01", "CN01")])
        _write_manifest_json(manifest)
        result = self._run(
            tmp_path, _write_manifest_yaml(tmp_path, rvi_cm_csv=rvi_cm, manifest_json=manifest)
        )
        assert result.status == CheckStatus.PASS, f"{result.message} / {result.details}"
        assert result.details["types_count"] == "1"

    def test_fail_when_an_id_cm_is_not_in_the_manifest(self, tmp_path: Path) -> None:
        rvi_cm = tmp_path / "MapeoRVI_CM.csv"
        manifest = tmp_path / "cm-types.json"
        _write_rvi_cm_3col(rvi_cm, [("", "FB01", "CN01"), ("", "FB23", "ZZ99")])
        _write_manifest_json(manifest)
        result = self._run(
            tmp_path, _write_manifest_yaml(tmp_path, rvi_cm_csv=rvi_cm, manifest_json=manifest)
        )
        assert result.status == CheckStatus.FAIL
        assert "ZZ99" in result.message

    def test_fail_when_a_used_property_has_no_field_source(self, tmp_path: Path) -> None:
        rvi_cm = tmp_path / "MapeoRVI_CM.csv"
        manifest = tmp_path / "cm-types.json"
        _write_rvi_cm_3col(rvi_cm, [("", "FB01", "CN01")])
        _write_manifest_json(manifest)
        # Renombra la propiedad a una que el YAML NO declara.
        manifest.write_text(manifest.read_text().replace("BAC_CIF", "BAC_Sin_Fuente"))
        result = self._run(
            tmp_path, _write_manifest_yaml(tmp_path, rvi_cm_csv=rvi_cm, manifest_json=manifest)
        )
        assert result.status == CheckStatus.FAIL
        assert "BAC_Sin_Fuente" in result.message

    def test_pass_when_the_property_resolves_through_a_field_alias(self, tmp_path: Path) -> None:
        """El YAML de `_write_split_yaml` declara `field_aliases: {CIF: BAC_CIF}`.

        La propiedad del manifest pasa a llamarse `CIF`: el runtime la
        resuelve por el alias, asi que el doctor no puede fallar.
        """
        rvi_cm = tmp_path / "MapeoRVI_CM.csv"
        manifest = tmp_path / "cm-types.json"
        _write_rvi_cm_3col(rvi_cm, [("", "FB01", "CN01")])
        _write_manifest_json(manifest)
        manifest.write_text(manifest.read_text().replace("cmcourier:BAC_CIF", "cmcourier:CIF"))
        result = self._run(
            tmp_path, _write_manifest_yaml(tmp_path, rvi_cm_csv=rvi_cm, manifest_json=manifest)
        )
        assert result.status == CheckStatus.PASS, f"{result.message} / {result.details}"

    def test_warnings_do_not_fail_the_check(self, tmp_path: Path) -> None:
        rvi_cm = tmp_path / "MapeoRVI_CM.csv"
        manifest = tmp_path / "cm-types.json"
        _write_rvi_cm_3col(rvi_cm, [("", "FB01", "CN01")])
        _write_manifest_json(manifest, reviewed=False)
        result = self._run(
            tmp_path, _write_manifest_yaml(tmp_path, rvi_cm_csv=rvi_cm, manifest_json=manifest)
        )
        assert result.status == CheckStatus.PASS
        assert result.details["warning_count"] == "1"
