"""139 REQ-001 — walker puro del schema (E1), ``coerce`` (E2) y ``diff_edits`` (E3)."""

from __future__ import annotations

from types import UnionType
from typing import Annotated, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from cmcourier.cli.console.schema_form import (
    DictSection,
    FieldSpec,
    ListSection,
    Node,
    Section,
    build_form,
    coerce,
    diff_edits,
)
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.yaml_doc import DELETE, MISSING, Edit, YamlPath

pytestmark = pytest.mark.unit


def _sample(
    *,
    trigger: str = "csv",
    source: str = "csv",
    meta_kinds: tuple[str, ...] = ("csv", "as400", "mssql"),
) -> dict:
    triggers = {
        "csv": {"kind": "csv", "csv_path": "t.csv"},
        "rvabrep": {"kind": "rvabrep", "filters": {"systems": ["A"]}},
        "local_scan": {"kind": "local_scan", "scan_path": ".", "recursive": True},
        "single_doc": {"kind": "single_doc"},
    }
    sources = {
        "csv": {"kind": "csv", "csv_path": "r.csv"},
        "as400": {"kind": "as400", "connection": "rvi", "query": "SELECT 1"},
    }
    metas = {
        "csv": {"kind": "csv", "alias": "c", "csv_path": "m.csv"},
        "as400": {"kind": "as400", "alias": "a", "as400_connection": {"host": "h"}, "table": "T"},
        "mssql": {"kind": "mssql", "alias": "m", "connection": "sql", "table": "dbo.t"},
    }
    return {
        "connections": {
            "rvi": {"kind": "as400", "host": "h"},
            "sql": {"kind": "mssql", "host": "s", "database": "d"},
        },
        "trigger": triggers[trigger],
        "indexing": {"source": sources[source], "columns": {}, "batch_size": 50},
        "mapping": {"csv_path": "m.csv"},
        "metadata": {
            "field_aliases": {"a": "b"},
            "field_sources": {
                "cif": {
                    "sources": [
                        {
                            "source_type": "trigger",
                            "lookup_value_column": "CIF",
                            "validation": {"allowed_pattern": "x"},
                        }
                    ]
                }
            },
            "sources": [metas[k] for k in meta_kinds],
            "cache": {"enabled": True},
        },
        "assembly": {
            "source_root": ".",
            "temp_dir": "tmp",
            "synthetic_content": {"size_mix": [{"name": "s", "weight": 1, "min": "1kb"}]},
        },
        "cmis": {"base_url": "u", "repo_id": "r", "workers": 4, "auto_tune": {}},
        "tracking": {
            "db_path": "t.db",
            "as400_sync": {"connection": None, "periodic": {"interval_minutes": 3}},
        },
        "observability": {"system_metrics": {}},
        "processing": {"streaming": {}, "heavy_light_lanes": {}},
    }


def _flatten(nodes: list[Node]) -> dict[YamlPath, Node]:
    out: dict[YamlPath, Node] = {}
    for node in nodes:
        out[node.path] = node
        if isinstance(node, Section):
            out.update(_flatten(node.children))
        elif isinstance(node, ListSection | DictSection):
            out.update(_flatten(list(node.items)))
    return out


def _spec(nodes: list[Node], path: YamlPath) -> FieldSpec:
    node = _flatten(nodes)[path]
    assert isinstance(node, FieldSpec), node
    return node


# ------------------------------------------------------------ E1: walker


class TestBuildForm:
    def test_scalar_with_constraints(self) -> None:
        spec = _spec(build_form(PipelineConfig, _sample()), ("cmis", "workers"))
        assert spec.kind == "int"
        assert spec.constraints == ">= 1"
        assert spec.default == 4
        assert spec.required is False
        assert spec.label == "workers"

    def test_range_constraints(self) -> None:
        nodes = build_form(PipelineConfig, _sample())
        assert _spec(nodes, ("cmis", "upload_chunk_bytes")).constraints == "4096..67108864"
        assert _spec(nodes, ("metadata", "cache", "ttl_minutes")).constraints == "> 0, <= 43200"
        assert _spec(nodes, ("cmis", "timeout_seconds")).kind == "float"

    def test_literal_is_choice(self) -> None:
        spec = _spec(build_form(PipelineConfig, _sample()), ("processing", "mode"))
        assert spec.kind == "choice"
        assert spec.choices == ("batched", "streaming")
        assert spec.default == "batched"

    def test_bool_path_and_required(self) -> None:
        nodes = build_form(PipelineConfig, _sample())
        assert _spec(nodes, ("cmis", "verify_ssl")).kind == "bool"
        csv_path = _spec(nodes, ("trigger", "csv_path"))
        assert csv_path.kind == "path"
        assert csv_path.required is True
        assert csv_path.default is None
        assert _spec(nodes, ("assembly", "temp_dir")).kind == "path"
        assert _spec(nodes, ("assembly", "source_root")).kind == "path"
        mapping_csv = _spec(nodes, ("mapping", "csv_path"))
        assert mapping_csv.kind == "path"
        assert mapping_csv.nullable is True

    def test_trigger_is_a_discriminated_section(self) -> None:
        nodes = build_form(PipelineConfig, _sample(trigger="csv"))
        trigger = _flatten(nodes)[("trigger",)]
        assert isinstance(trigger, Section)
        first = trigger.children[0]
        assert first == FieldSpec(
            ("trigger", "kind"),
            "kind",
            "choice",
            choices=("csv", "rvabrep", "local_scan", "single_doc"),
            required=True,
            discriminator=True,
        )
        names = [c.path[-1] for c in trigger.children]
        assert names == ["kind", "csv_path", "shortname_column", "cif_column", "system_id_column"]

    def test_trigger_variant_follows_the_data(self) -> None:
        nodes = build_form(PipelineConfig, _sample(trigger="local_scan"))
        flat = _flatten(nodes)
        assert ("trigger", "scan_path") in flat
        assert ("trigger", "recursive") in flat
        assert ("trigger", "csv_path") not in flat
        # sin `kind` en los datos → la primera variante (csv), como `_inject_default_kinds`
        data = _sample()
        del data["trigger"]["kind"]
        assert ("trigger", "csv_path") in _flatten(build_form(PipelineConfig, data))

    def test_metadata_sources_is_a_discriminated_list(self) -> None:
        nodes = build_form(PipelineConfig, _sample())
        sources = _flatten(nodes)[("metadata", "sources")]
        assert isinstance(sources, ListSection)
        assert sources.discriminator == "kind"
        assert sources.choices == ("csv", "as400", "mssql")
        assert [item.path for item in sources.items] == [
            ("metadata", "sources", 0),
            ("metadata", "sources", 1),
            ("metadata", "sources", 2),
        ]
        flat = _flatten(nodes)
        assert flat[("metadata", "sources", 0, "csv_path")].kind == "path"
        as400_conn = flat[("metadata", "sources", 1, "as400_connection")]
        assert isinstance(as400_conn, FieldSpec)
        assert as400_conn.kind == "connection"
        assert as400_conn.nullable is False
        mssql_conn = flat[("metadata", "sources", 2, "connection")]
        assert isinstance(mssql_conn, FieldSpec)
        assert mssql_conn.kind == "connection"
        assert mssql_conn.choices == ("mssql",)
        assert as400_conn.choices == ("as400",)

    def test_field_sources_dict_and_aliases_map(self) -> None:
        nodes = build_form(PipelineConfig, _sample())
        flat = _flatten(nodes)
        fs = flat[("metadata", "field_sources")]
        assert isinstance(fs, DictSection)
        assert [item.path for item in fs.items] == [("metadata", "field_sources", "cif")]
        inner = flat[("metadata", "field_sources", "cif", "sources")]
        assert isinstance(inner, ListSection)
        assert inner.discriminator is None
        assert flat[("metadata", "field_sources", "cif", "sources", 0, "source_type")].kind == "str"
        assert _spec(nodes, ("metadata", "field_aliases")).kind == "str_map"
        assert _spec(nodes, ("assembly", "image_type_map")).kind == "str_map"

    def test_str_list(self) -> None:
        nodes = build_form(PipelineConfig, _sample(trigger="rvabrep"))
        assert _spec(nodes, ("trigger", "filters", "systems")).kind == "str_list"

    def test_connections_is_readonly(self) -> None:
        conns = _flatten(build_form(PipelineConfig, _sample()))[("connections",)]
        assert isinstance(conns, Section)
        assert conns.readonly is True
        assert conns.children == []
        assert "[2]" in conns.description

    def test_sync_connection_is_nullable_connection(self) -> None:
        nodes = build_form(PipelineConfig, _sample())
        spec = _spec(nodes, ("tracking", "as400_sync", "connection"))
        assert spec.kind == "connection"
        assert spec.nullable is True
        assert spec.choices == ("as400",)

    def test_indexing_source_as400_connection(self) -> None:
        nodes = build_form(PipelineConfig, _sample(source="as400"))
        spec = _spec(nodes, ("indexing", "source", "connection"))
        assert spec.kind == "connection"
        assert spec.nullable is False
        assert _spec(nodes, ("indexing", "source", "query")).kind == "str"

    def test_tuple_of_models_is_a_list_section(self) -> None:
        flat = _flatten(build_form(PipelineConfig, _sample()))
        mix = flat[("assembly", "synthetic_content", "size_mix")]
        assert isinstance(mix, ListSection)
        assert mix.discriminator is None
        assert flat[("assembly", "synthetic_content", "size_mix", 0, "weight")].kind == "float"

    def test_optional_model_is_a_section(self) -> None:
        flat = _flatten(build_form(PipelineConfig, _sample()))
        periodic = flat[("tracking", "as400_sync", "periodic")]
        assert isinstance(periodic, Section)
        assert ("tracking", "as400_sync", "periodic", "interval_minutes") in flat

    def test_missing_data_still_yields_every_node(self) -> None:
        flat = _flatten(build_form(PipelineConfig, {}))
        assert ("cmis", "workers") in flat
        assert ("trigger", "csv_path") in flat
        assert ("metadata", "field_sources") in flat


# ------------------------------------------------------------ cobertura total


def _strip(annotation: object) -> tuple[object, list[object]]:
    """Annotated → (tipo, metadata); ``X | None`` → X."""
    meta: list[object] = []
    while get_origin(annotation) is Annotated:
        annotation, *extra = get_args(annotation)
        meta.extend(extra)
    if get_origin(annotation) in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            sub, sub_meta = _strip(args[0])
            return sub, meta + sub_meta
        return Union[tuple(args)], meta  # noqa: UP007 — dinámico
    return annotation, meta


def _is_model(tp: object) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _variant(tp: object, data: object) -> type[BaseModel] | None:
    """El modelo que corresponde a ``data`` para un tipo (modelo, unión discriminada o no)."""
    tp, _ = _strip(tp)
    if _is_model(tp):
        return tp  # type: ignore[return-value]
    if get_origin(tp) is Union:
        models = [a for a in get_args(tp) if _is_model(a)]
        kind = data.get("kind") if isinstance(data, dict) else None
        for model in models:
            kind_field = model.model_fields.get("kind")
            if kind_field is not None and kind in get_args(kind_field.annotation):
                return model
        return models[0] if models else None
    return None


def _check_model(model: type[BaseModel], data: dict, path: YamlPath, flat: dict) -> int:
    seen = 0
    for name, info in model.model_fields.items():
        field_path = (*path, name)
        assert field_path in flat, f"sin nodo para {field_path}"
        seen += 1
        node = flat[field_path]
        value = data.get(name)
        tp, _ = _strip(info.annotation)
        origin = get_origin(tp)
        if isinstance(node, Section) and not node.readonly:
            sub = _variant(tp, value)
            assert sub is not None, field_path
            seen += _check_model(sub, value if isinstance(value, dict) else {}, field_path, flat)
        elif isinstance(node, ListSection):
            assert origin in (list, tuple)
            for i, item in enumerate(value or []):
                sub = _variant(get_args(tp)[0], item)
                assert sub is not None
                seen += _check_model(sub, item, (*field_path, i), flat)
        elif isinstance(node, DictSection):
            assert origin is dict
            for key, item in (value or {}).items():
                sub = _variant(get_args(tp)[1], item)
                assert sub is not None
                seen += _check_model(sub, item, (*field_path, key), flat)
    return seen


class TestCoverage:
    @pytest.mark.parametrize(
        ("trigger", "source"),
        [("csv", "csv"), ("rvabrep", "as400"), ("local_scan", "csv"), ("single_doc", "as400")],
    )
    def test_every_field_of_every_reachable_model_has_a_node(
        self, trigger: str, source: str
    ) -> None:
        data = _sample(trigger=trigger, source=source)
        flat = _flatten(build_form(PipelineConfig, data))
        seen = _check_model(PipelineConfig, data, (), flat)
        assert seen > 100  # ~120 campos en el schema — un walker roto se nota

    def test_no_unknown_kinds(self) -> None:
        typed = {"int", "float", "bool", "choice", "path", "str_list", "str_map", "connection"}
        flat = _flatten(build_form(PipelineConfig, _sample(trigger="rvabrep", source="as400")))
        kinds = {node.kind for node in flat.values() if isinstance(node, FieldSpec)}
        assert kinds <= typed | {"str"}
        assert typed <= kinds


# ------------------------------------------------------------ E2: coerce


def _field(kind: str, *, nullable: bool = False) -> FieldSpec:
    return FieldSpec(("x",), "x", kind, nullable=nullable)  # type: ignore[arg-type]


class TestCoerce:
    def test_int_and_float(self) -> None:
        assert coerce(_field("int"), "8") == 8
        assert coerce(_field("int"), "abc") == "abc"
        assert coerce(_field("int"), "") is MISSING
        assert coerce(_field("float"), "2.5") == 2.5
        assert coerce(_field("float"), "x") == "x"

    def test_empty_nullable_is_none(self) -> None:
        assert coerce(_field("str", nullable=True), "") is None
        assert coerce(_field("str"), "") is MISSING
        assert coerce(_field("int", nullable=True), "  ") is None

    def test_bool_words(self) -> None:
        assert coerce(_field("bool"), "Sí") is True
        assert coerce(_field("bool"), "no") is False
        assert coerce(_field("bool"), "true") is True
        assert coerce(_field("bool"), "0") is False
        assert coerce(_field("bool"), "maybe") == "maybe"

    def test_str_list_and_map(self) -> None:
        assert coerce(_field("str_list"), "a, b,,c") == ["a", "b", "c"]
        assert coerce(_field("str_map"), "k: v\nk2: v2") == {"k": "v", "k2": "v2"}
        assert coerce(_field("str_map"), "k: a: b\n\n") == {"k": "a: b"}

    def test_passthrough_kinds(self) -> None:
        assert coerce(_field("choice"), "batched") == "batched"
        assert coerce(_field("connection"), "rvi") == "rvi"
        assert coerce(_field("path"), " ./x ") == "./x"
        assert coerce(_field("str"), "hola") == "hola"


# ------------------------------------------------------------ E3: diff


def _base() -> dict:
    return {
        "cmis": {"workers": 4, "base_url": "u"},
        "metadata": {
            "sources": [
                {"kind": "csv", "alias": "a", "csv_path": "a.csv"},
                {"kind": "csv", "alias": "b", "csv_path": "b.csv"},
            ],
        },
        "trigger": {"filters": {"systems": ["A", "B"]}},
    }


class TestDiffEdits:
    def test_no_changes(self) -> None:
        assert diff_edits(_base(), _base()) == []

    def test_scalar_change_and_delete_and_add(self) -> None:
        working = _base()
        working["cmis"]["workers"] = 8
        assert diff_edits(_base(), working) == [Edit(("cmis", "workers"), 8)]
        working = _base()
        del working["cmis"]["workers"]
        assert diff_edits(_base(), working) == [Edit(("cmis", "workers"), DELETE)]
        working = _base()
        working["cmis"]["http2"] = False
        assert diff_edits(_base(), working) == [Edit(("cmis", "http2"), False)]

    def test_new_nested_block(self) -> None:
        working = _base()
        working["processing"] = {"mode": "streaming"}
        assert diff_edits(_base(), working) == [Edit(("processing",), {"mode": "streaming"})]

    def test_bool_vs_int_are_different(self) -> None:
        original = {"a": {"x": 1}}
        assert diff_edits(original, {"a": {"x": True}}) == [Edit(("a", "x"), True)]

    def test_append_third_source(self) -> None:
        working = _base()
        new = {"kind": "mssql", "alias": "c", "connection": "sql"}
        working["metadata"]["sources"].append(new)
        assert diff_edits(_base(), working) == [Edit(("metadata", "sources", 2), new)]

    def test_remove_last_source(self) -> None:
        working = _base()
        del working["metadata"]["sources"][1]
        assert diff_edits(_base(), working) == [Edit(("metadata", "sources", 1), DELETE)]

    def test_remove_first_source_replaces_index_0_and_deletes_1(self) -> None:
        """Aceptado (E3): el diff es por índice — el item 0 se reemplaza entero
        (pierde sus comentarios) y el índice 1 se borra."""
        working = _base()
        del working["metadata"]["sources"][0]
        assert diff_edits(_base(), working) == [
            Edit(("metadata", "sources", 0), {"kind": "csv", "alias": "b", "csv_path": "b.csv"}),
            Edit(("metadata", "sources", 1), DELETE),
        ]

    def test_remove_two_deletes_from_highest_index(self) -> None:
        original = {"l": [{"a": 1}, {"a": 2}, {"a": 3}]}
        assert diff_edits(original, {"l": []}) == [
            Edit(("l", 2), DELETE),
            Edit(("l", 1), DELETE),
            Edit(("l", 0), DELETE),
        ]

    def test_edit_inside_item_replaces_the_item(self) -> None:
        working = _base()
        working["metadata"]["sources"][1]["csv_path"] = "c.csv"
        assert diff_edits(_base(), working) == [
            Edit(("metadata", "sources", 1), {"kind": "csv", "alias": "b", "csv_path": "c.csv"})
        ]

    def test_scalar_list_is_one_edit(self) -> None:
        working = _base()
        working["trigger"]["filters"]["systems"] = ["A"]
        assert diff_edits(_base(), working) == [Edit(("trigger", "filters", "systems"), ["A"])]

    def test_type_change_dict_to_scalar(self) -> None:
        original = {"t": {"as400_sync": {"connection": {"host": "h"}}}}
        working = {"t": {"as400_sync": {"connection": "rvi"}}}
        assert diff_edits(original, working) == [Edit(("t", "as400_sync", "connection"), "rvi")]
        assert diff_edits(working, original) == [
            Edit(("t", "as400_sync", "connection"), {"host": "h"})
        ]
