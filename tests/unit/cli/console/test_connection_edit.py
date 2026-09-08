"""138 REQ-001 — módulo puro del editor de conexiones: validación del
borrador, mapping al YAML, sitios que aceptan un alias y los planes de
edición (`plan_write` / `plan_delete`)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cmcourier.cli.console.connection_edit import (
    KIND_FIELDS,
    ConnectionDraft,
    DeletePlan,
    Site,
    connection_sites,
    draft_to_yaml,
    field_default,
    inline_connection,
    plan_delete,
    plan_write,
    validate_draft,
    write_error_text,
)
from cmcourier.config.loader import load_config
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.yaml_doc import DELETE, Edit, YamlDocument, YamlWriteError, apply_edits
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = pytest.mark.unit

_AS400_SOURCE = (
    "indexing:\n"
    "  source:\n"
    "    kind: as400\n"
    "    connection:\n"
    "      host: as400.test\n"
    "      port: 8471\n"
    "    query: SELECT * FROM RVABREP\n"
)


def _config(
    tmp_path: Path,
    *,
    connections: str = "",
    inline_indexing: bool = False,
    metadata_sources: str = "",
    sync: str = "",
) -> tuple[PipelineConfig, Path]:
    """Config CSV base (+ registro / indexing as400 inline / fuentes / sync)."""
    _, path = _make_config(tmp_path)
    text = path.read_text()
    if connections:
        text = f"connections:\n{connections}" + text
    if inline_indexing:
        text, n = re.subn(
            r"indexing:\n  source:\n    kind: csv\n    csv_path: .*\n", _AS400_SOURCE, text
        )
        assert n == 1
    if metadata_sources:
        text = text.replace(
            "metadata:\n  field_aliases: {}\n  field_sources: {}\n",
            "metadata:\n  field_aliases: {}\n  field_sources: {}\n  sources:\n" + metadata_sources,
        )
    if sync:
        text = text.replace("tracking:\n", "tracking:\n  as400_sync:\n" + sync)
    path.write_text(text)
    return load_config(path), path


_RVI = "  rvi: {kind: as400, host: as400.test, port: 446, database: RVILIB}\n"
_SQL = "  clientes_sql: {kind: mssql, host: sql.test, database: cmcourier}\n"
_META_AS400 = (
    "    - kind: as400\n      alias: cli\n      as400_connection: rvi\n      table: CLIENTES\n"
)
_META_MSSQL = (
    "    - kind: mssql\n      alias: clientes\n      connection: clientes_sql\n"
    "      table: dbo.clientes\n"
)
_SYNC_RVI = "    enabled: true\n    connection: rvi\n"


def _draft(alias: str = "nueva", kind: str = "as400", **fields: str) -> ConnectionDraft:
    return ConnectionDraft(alias=alias, kind=kind, fields=fields)  # type: ignore[arg-type]


class TestValidateDraft:
    def test_valid_minimal_as400(self) -> None:
        assert validate_draft(_draft(host="h"), existing={"rvi"}, editing=None) == {}

    def test_alias_rules(self) -> None:
        """E2: mayúsculas, reservados (`cmis` / `as400`), duplicados y vacío."""
        err = validate_draft(_draft("Clientes", host="h"), existing=set(), editing=None)
        assert "minúsculas" in err["alias"]
        err = validate_draft(_draft("cmis", host="h"), existing=set(), editing=None)
        assert "reservado" in err["alias"]
        err = validate_draft(_draft("as400", host="h"), existing=set(), editing=None)
        assert "reservado" in err["alias"]
        err = validate_draft(_draft("rvi", host="h"), existing={"rvi"}, editing=None)
        assert "ya existe" in err["alias"]
        err = validate_draft(_draft("", host="h"), existing=set(), editing=None)
        assert "alias" in err
        err = validate_draft(_draft("a" * 33, host="h"), existing=set(), editing=None)
        assert "alias" in err

    def test_editing_alias_is_not_a_duplicate(self) -> None:
        assert validate_draft(_draft("rvi", host="h"), existing={"rvi"}, editing="rvi") == {}

    def test_host_required_and_port_range(self) -> None:
        err = validate_draft(_draft(host="  ", port="70000"), existing=set(), editing=None)
        assert "requerido" in err["host"]
        assert "1..65535" in err["port"]
        err = validate_draft(_draft(host="h", port="abc"), existing=set(), editing=None)
        assert "1..65535" in err["port"]
        err = validate_draft(_draft(host="h", port="0"), existing=set(), editing=None)
        assert "port" in err
        assert validate_draft(_draft(host="h", port=" 446 "), existing=set(), editing=None) == {}

    def test_m1_unicode_digits_are_rejected_not_crashed(self) -> None:
        """M1: ``"²".isdigit()`` es True pero ``int("²")`` revienta."""
        for raw in ("²", "٤٤٦", "1²"):
            err = validate_draft(_draft(host="h", port=raw), existing=set(), editing=None)
            assert err["port"] == "port: 1..65535", raw

    def test_mssql_database_required_and_booleans(self) -> None:
        err = validate_draft(
            _draft(kind="mssql", host="h", encrypt="quizás"), existing=set(), editing=None
        )
        assert "requerido" in err["database"]
        assert "true/false" in err["encrypt"]
        for value in ("true", "False", "SÍ", "no", "1", "0", "si"):
            ok = validate_draft(
                _draft(kind="mssql", host="h", database="d", trust_server_certificate=value),
                existing=set(),
                editing=None,
            )
            assert ok == {}, value

    def test_empty_optionals_are_fine(self) -> None:
        draft = _draft(kind="mssql", host="h", database="d", port="", driver="", encrypt="")
        assert validate_draft(draft, existing=set(), editing=None) == {}


class TestDraftToYaml:
    def test_types_and_omits_empty(self) -> None:
        """E1: `port` / `driver` / `encrypt` vacíos no se escriben."""
        draft = _draft(
            "clientes_sql", "mssql", host="sql01", database="clientes", port="", driver=""
        )
        assert draft_to_yaml(draft) == {"kind": "mssql", "host": "sql01", "database": "clientes"}

    def test_port_int_and_bools(self) -> None:
        draft = _draft(
            "x",
            "mssql",
            host="h",
            database="d",
            port=" 1444 ",
            encrypt="no",
            trust_server_certificate="Sí",
        )
        assert draft_to_yaml(draft) == {
            "kind": "mssql",
            "host": "h",
            "port": 1444,
            "database": "d",
            "encrypt": False,
            "trust_server_certificate": True,
        }

    def test_kind_fields_and_placeholders_come_from_the_model(self) -> None:
        assert KIND_FIELDS["as400"] == ("host", "port", "database", "driver", "table")
        assert KIND_FIELDS["mssql"] == (
            "host",
            "port",
            "database",
            "driver",
            "encrypt",
            "trust_server_certificate",
        )
        assert field_default("as400", "port") == "446"
        assert field_default("mssql", "port") == "1433"
        assert field_default("mssql", "encrypt") == "true"
        assert field_default("as400", "host") == ""
        assert field_default("as400", "table") == ""


class TestConnectionSites:
    def test_csv_only_config_has_no_sites(self, tmp_path: Path) -> None:
        config, _ = _config(tmp_path)
        assert connection_sites(config) == []

    def test_all_four_site_kinds(self, tmp_path: Path) -> None:
        config, _ = _config(
            tmp_path,
            connections=_RVI + _SQL,
            inline_indexing=True,
            metadata_sources=_META_AS400 + _META_MSSQL,
            sync=_SYNC_RVI,
        )
        sites = connection_sites(config)
        assert [(s.path, s.label, s.kind, s.current) for s in sites] == [
            (("indexing", "source", "connection"), "indexing.source", "as400", None),
            (
                ("metadata", "sources", 0, "as400_connection"),
                "metadata.sources[0] cli",
                "as400",
                "rvi",
            ),
            (
                ("metadata", "sources", 1, "connection"),
                "metadata.sources[1] clientes",
                "mssql",
                "clientes_sql",
            ),
            (("tracking", "as400_sync", "connection"), "tracking.as400_sync", "as400", "rvi"),
        ]

    def test_i2_disabled_sync_that_names_an_alias_is_still_a_site(self, tmp_path: Path) -> None:
        """I2: ``schema.connection_refs(include_disabled=True)`` lo valida igual —
        si no es un sitio, ``plan_delete`` borraría el alias y la config no carga."""
        config, _ = _config(
            tmp_path, connections=_RVI, sync="    enabled: false\n    connection: rvi\n"
        )
        assert [s.path for s in connection_sites(config)] == [
            ("tracking", "as400_sync", "connection")
        ]
        assert not plan_delete("rvi", config).ok

    def test_disabled_sync_without_alias_is_not_a_site(self, tmp_path: Path) -> None:
        config, _ = _config(tmp_path, connections=_RVI, sync="    enabled: false\n")
        assert connection_sites(config) == []


class TestPlanWrite:
    def test_writes_connection_and_marked_sites(self, tmp_path: Path) -> None:
        config, _ = _config(tmp_path, connections=_RVI, inline_indexing=True, sync=_SYNC_RVI)
        draft = _draft("rvi2", "as400", host="as400b", port="")
        edits = plan_write(draft, config, use_at={("indexing", "source", "connection")})
        assert edits == [
            Edit(("connections", "rvi2"), {"kind": "as400", "host": "as400b"}),
            Edit(("indexing", "source", "connection"), "rvi2"),
        ]

    def test_editing_emits_one_edit_per_field(self, tmp_path: Path) -> None:
        """E3: editar toca campo por campo (vacío → DELETE) y NO reemplaza el
        mapping — así un `rvi: {…}` en flow style sigue siendo una línea."""
        config, path = _config(tmp_path, connections=_RVI)
        draft = _draft("rvi", "as400", host="as400b", port="446", database="RVILIB", driver="")
        edits = plan_write(draft, config, use_at=set(), editing="rvi")
        assert edits == [
            Edit(("connections", "rvi", "host"), "as400b"),
            Edit(("connections", "rvi", "port"), 446),
            Edit(("connections", "rvi", "database"), "RVILIB"),
            Edit(("connections", "rvi", "driver"), DELETE),
            Edit(("connections", "rvi", "table"), DELETE),
        ]
        doc = YamlDocument.load(path)
        apply_edits(doc, edits)
        assert "  rvi: {kind: as400, host: as400b, port: 446, database: RVILIB}\n" in doc.text()

    def test_site_of_other_kind_raises(self, tmp_path: Path) -> None:
        """E8: un sitio as400 con un draft mssql no llega al disco."""
        config, _ = _config(tmp_path, inline_indexing=True)
        draft = _draft("sql", "mssql", host="h", database="d")
        with pytest.raises(ValueError, match="indexing.source"):
            plan_write(draft, config, use_at={("indexing", "source", "connection")})

    def test_unknown_site_raises(self, tmp_path: Path) -> None:
        config, _ = _config(tmp_path)
        with pytest.raises(ValueError):
            plan_write(_draft(host="h"), config, use_at={("nada", "aca")})

    def test_edits_apply_to_a_document(self, tmp_path: Path) -> None:
        """E4 a nivel puro: el inline de indexing se reemplaza por el alias."""
        config, path = _config(tmp_path, connections=_RVI, inline_indexing=True)
        draft = _draft("rvi2", "as400", host="as400b")
        doc = YamlDocument.load(path)
        apply_edits(doc, plan_write(draft, config, use_at={("indexing", "source", "connection")}))
        text = doc.text()
        assert "    connection: rvi2\n" in text
        assert "      host: as400.test\n" not in text
        assert "  rvi2:\n    kind: as400\n    host: as400b\n" in text


class TestPlanDelete:
    def test_blocked_when_referenced(self, tmp_path: Path) -> None:
        config, _ = _config(tmp_path, connections=_RVI, metadata_sources=_META_AS400)
        plan = plan_delete("rvi", config)
        assert isinstance(plan, DeletePlan)
        assert plan.edits == []
        assert [s.label for s in plan.blocked_by] == ["metadata.sources[0] cli"]
        assert not plan.ok

    def test_delete_edit_when_unused(self, tmp_path: Path) -> None:
        config, _ = _config(tmp_path, connections=_RVI)
        plan = plan_delete("rvi", config)
        assert plan.ok
        assert plan.edits == [Edit(("connections", "rvi"), DELETE)]
        assert plan.blocked_by == []


class TestInlineConnection:
    """M3: ``plan_move_inline`` era código muerto — mover abre el modal y usa
    ``plan_write``; de la parte pura sólo sobrevive el lector del inline (E6)."""

    def test_reads_the_inline_model_of_a_site(self, tmp_path: Path) -> None:
        config, _ = _config(tmp_path, inline_indexing=True)
        site = connection_sites(config)[0]
        model = inline_connection(site, config)
        assert (model.host, model.port, model.database) == ("as400.test", 8471, "RVILIB")

    def test_site_with_alias_is_not_inline(self, tmp_path: Path) -> None:
        config, _ = _config(tmp_path, connections=_RVI, metadata_sources=_META_AS400)
        site = connection_sites(config)[0]
        assert site.current == "rvi"
        with pytest.raises(ValueError, match="inline"):
            inline_connection(site, config)


class TestWriteErrorText:
    def test_verify_failure_shows_pydantic_messages_not_the_input_dump(
        self, tmp_path: Path
    ) -> None:
        """E8: el toast dice QUÉ rechazó pydantic, no vuelca la config entera."""
        config, path = _config(tmp_path, connections=_RVI, metadata_sources=_META_AS400)
        doc = YamlDocument.load(path)
        apply_edits(doc, [Edit(("metadata", "sources", 0, "as400_connection"), "nope")])
        with pytest.raises(YamlWriteError) as info:
            doc.write(verify=load_config)
        text = write_error_text(info.value)
        assert "unknown connection alias 'nope'" in text
        assert "Value error" not in text
        assert "'input':" not in text and len(text) < 200

    def test_plain_errors_pass_through(self) -> None:
        assert write_error_text(OSError("disco lleno")) == "disco lleno"


class TestSite:
    def test_site_is_frozen(self) -> None:
        site = Site(path=("a",), label="a", kind="as400", current=None)
        with pytest.raises(AttributeError):
            site.label = "b"  # type: ignore[misc]
