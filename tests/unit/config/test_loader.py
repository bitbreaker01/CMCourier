"""Tests unitarios para ``cmcourier.config.loader``."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cmcourier.config.loader import Credential, Secrets, load_config, load_secrets
from cmcourier.config.schema import PipelineConfig
from cmcourier.domain.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_valid_yaml(tmp_path: Path) -> Path:
    trigger = tmp_path / "triggers.csv"
    trigger.write_text("ShortName,CIF,SystemID\n")
    rvabrep = tmp_path / "rvabrep.csv"
    rvabrep.write_text("shortname,system_id,txn_num\n")
    modelo = tmp_path / "modelo.csv"
    modelo.write_text("ID RVI,ID CLASE DOCUMENTAL\n")
    clients = tmp_path / "clients.csv"
    clients.write_text("CIF,Nombre_Cliente\n")
    assembly = tmp_path / "assembly"
    assembly.mkdir()
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        dedent(
            f"""\
            trigger:
              csv_path: {trigger}
            indexing:
              source:
                kind: csv
                csv_path: {rvabrep}
            mapping:
              csv_path: {modelo}
            metadata:
              field_aliases:
                CIF: BAC_CIF
              field_sources:
                BAC_CIF:
                  sources:
                    - source_type: trigger
                      lookup_value_column: cif
              sources:
                - alias: clients
                  csv_path: {clients}
            assembly:
              source_root: {assembly}
              temp_dir: {tmp_path / "stg"}
            cmis:
              base_url: http://cmis.test:9080/cmis
              repo_id: $x!test
            tracking:
              db_path: {tmp_path / "tr.db"}
            """
        )
    )
    return yaml_path


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_valid_yaml_loads(self, tmp_path: Path) -> None:
        yaml_path = _write_valid_yaml(tmp_path)
        config = load_config(yaml_path)
        assert isinstance(config, PipelineConfig)
        assert config.cmis.base_url == "http://cmis.test:9080/cmis"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError) as ei:
            load_config(tmp_path / "nope.yaml")
        assert ei.value.context["config_path"].endswith("nope.yaml")

    def test_invalid_yaml_syntax_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("trigger: {not closed")
        with pytest.raises(ConfigurationError) as ei:
            load_config(path)
        assert "reason" in ei.value.context

    def test_yaml_root_must_be_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- 1\n- 2\n- 3\n")
        with pytest.raises(ConfigurationError) as ei:
            load_config(path)
        assert ei.value.context["actual_type"] == "list"

    def test_validation_failure_surfaces_errors(self, tmp_path: Path) -> None:
        yaml_path = _write_valid_yaml(tmp_path)
        # Agrega un campo top-level desconocido editando el archivo.
        with yaml_path.open("a") as fh:
            fh.write("\ncosmic_settings:\n  intent: blast\n")
        with pytest.raises(ConfigurationError) as ei:
            load_config(yaml_path)
        assert "errors" in ei.value.context

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        yaml_path = _write_valid_yaml(tmp_path)
        text = yaml_path.read_text()
        # Quita la línea `cmis.base_url`.
        text = text.replace("  base_url: http://cmis.test:9080/cmis\n", "")
        yaml_path.write_text(text)
        with pytest.raises(ConfigurationError):
            load_config(yaml_path)


class TestIndexingSource048:
    """048 — la `source` de RVABREP es una unión discriminada (`csv` ↔ `as400`)."""

    def test_csv_source_variant_loads(self, tmp_path: Path) -> None:
        yaml_path = _write_valid_yaml(tmp_path)
        config = load_config(yaml_path)
        from cmcourier.config.schema import CsvRvabrepSource

        assert isinstance(config.indexing.source, CsvRvabrepSource)
        assert config.indexing.source.kind == "csv"

    def test_as400_source_variant_loads(self, tmp_path: Path) -> None:
        yaml_path = _write_valid_yaml(tmp_path)
        text = yaml_path.read_text()
        # Intercambia el bloque de `source` `csv` por uno `as400`.
        text = text.replace(
            f"indexing:\n  source:\n    kind: csv\n    csv_path: {tmp_path / 'rvabrep.csv'}\n",
            "indexing:\n"
            "  source:\n"
            "    kind: as400\n"
            "    connection:\n"
            "      host: as400.bank.test\n"
            '    query: "SELECT * FROM RVILIB.RVABREP"\n',
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        from cmcourier.config.schema import As400RvabrepSource

        assert isinstance(config.indexing.source, As400RvabrepSource)
        assert config.indexing.source.kind == "as400"
        assert config.indexing.source.connection.host == "as400.bank.test"
        assert "RVABREP" in config.indexing.source.query

    def test_trigger_kind_as400_rejected_with_directive_error(self, tmp_path: Path) -> None:
        """048 quitó ``trigger.kind: as400``. El loader debe rechazarlo
        con un mensaje que apunte a la nueva forma ``indexing.source``,
        no un error críptico de unión discriminada."""
        yaml_path = _write_valid_yaml(tmp_path)
        text = yaml_path.read_text()
        text = text.replace(
            f"trigger:\n  csv_path: {tmp_path / 'triggers.csv'}\n",
            'trigger:\n  kind: as400\n  query: "SELECT 1"\n',
        )
        yaml_path.write_text(text)
        with pytest.raises(ConfigurationError) as ei:
            load_config(yaml_path)
        assert ei.value.context.get("removed_kind") == "as400"
        assert "indexing.source" in str(ei.value)


# ---------------------------------------------------------------------------
# load_secrets
# ---------------------------------------------------------------------------


class TestLoadSecrets:
    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "topsecret")
        monkeypatch.delenv("AS400_USERNAME", raising=False)
        monkeypatch.delenv("AS400_PASSWORD", raising=False)
        secrets = load_secrets()
        assert secrets.cmis == Credential(username="tester", password="topsecret")
        assert secrets.cmis_username == "tester"
        assert secrets.cmis_password == "topsecret"
        assert secrets.get("as400") is None
        assert secrets.get("cmis") == secrets.cmis

    def test_missing_cmis_username_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CMIS_USERNAME", raising=False)
        monkeypatch.setenv("CMIS_PASSWORD", "x")
        with pytest.raises(ConfigurationError) as ei:
            load_secrets()
        assert "CMIS_USERNAME" in ei.value.context["missing_vars"]

    def test_empty_cmis_password_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "   ")
        with pytest.raises(ConfigurationError) as ei:
            load_secrets()
        assert "CMIS_PASSWORD" in ei.value.context["missing_vars"]

    def test_as400_optional_without_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Compat: sin config se lee el alias implícito `as400` (inspect)."""
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "x")
        monkeypatch.setenv("AS400_USERNAME", "a400user")
        monkeypatch.setenv("AS400_PASSWORD", "a400pass")
        secrets = load_secrets()
        assert secrets.require("as400") == Credential("a400user", "a400pass")

    def test_reads_every_alias_the_config_needs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """E4 — `<ALIAS>_USERNAME/<ALIAS>_PASSWORD` por cada alias referenciado."""
        yaml_path = _write_valid_yaml(tmp_path)
        text = yaml_path.read_text()
        text = text.replace(
            f"indexing:\n  source:\n    kind: csv\n    csv_path: {tmp_path / 'rvabrep.csv'}\n",
            "connections:\n"
            "  rvi:\n"
            "    kind: as400\n"
            "    host: as400.bank.test\n"
            "  clientes_sql:\n"
            "    kind: mssql\n"
            "    host: sql.bank.test\n"
            "    database: cmcourier\n"
            "indexing:\n"
            "  source:\n"
            "    kind: as400\n"
            "    connection: rvi\n"
            '    query: "SELECT * FROM RVILIB.RVABREP"\n',
        )
        text = text.replace(
            "    - alias: clients\n",
            "    - kind: as400\n"
            "      alias: personas\n"
            "      as400_connection: rvi\n"
            "      table: PERSONAS\n"
            "    - alias: clients\n",
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)
        assert config.required_aliases() == ("rvi",)
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "x")
        monkeypatch.setenv("RVI_USERNAME", "rviuser")
        monkeypatch.setenv("RVI_PASSWORD", "rvipass")
        monkeypatch.delenv("AS400_USERNAME", raising=False)
        monkeypatch.delenv("AS400_PASSWORD", raising=False)
        secrets = load_secrets(config)
        assert secrets.get("rvi") == Credential("rviuser", "rvipass")
        # `as400` no es requerido por esta config: no se lee.
        assert secrets.get("as400") is None
        assert secrets.get("clientes_sql") is None

    def test_get_is_none_when_any_half_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CMIS_USERNAME", "tester")
        monkeypatch.setenv("CMIS_PASSWORD", "x")
        monkeypatch.setenv("AS400_USERNAME", "a400user")
        monkeypatch.setenv("AS400_PASSWORD", "   ")
        secrets = load_secrets()
        assert secrets.get("as400") is None
        with pytest.raises(ConfigurationError) as ei:
            secrets.require("as400")
        assert ei.value.context["missing_vars"] == ["AS400_USERNAME", "AS400_PASSWORD"]

    def test_require_names_alias_env_vars(self) -> None:
        secrets = Secrets({"cmis": Credential("u", "p")})
        with pytest.raises(ConfigurationError) as ei:
            secrets.require("clientes_sql")
        assert ei.value.context["missing_vars"] == [
            "CLIENTES_SQL_USERNAME",
            "CLIENTES_SQL_PASSWORD",
        ]
        assert "clientes_sql" in str(ei.value)

    def test_secrets_frozen(self) -> None:
        import dataclasses

        secrets = Secrets({"cmis": Credential("u", "p")})
        with pytest.raises(dataclasses.FrozenInstanceError):
            secrets.credentials = {}  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            secrets.cmis.username = "other"  # type: ignore[misc]

    def test_secrets_requires_cmis(self) -> None:
        with pytest.raises(ValueError, match="cmis"):
            Secrets({})
