"""`YamlDocument` (137): round-trip con ruamel — lo que NO se toca queda byte-idéntico."""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from cmcourier.config.loader import load_config
from cmcourier.config.schema import MssqlConnectionConfig
from cmcourier.config.yaml_doc import (
    DELETE,
    MISSING,
    Edit,
    YamlDocument,
    YamlDocumentError,
    YamlWriteError,
    apply_edits,
    to_plain,
)
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent.parent.parent
_SAMPLES = [*sorted(_ROOT.glob("sample/*.yaml")), _ROOT / "docs/reference/config-reference.yaml"]


def _doc(tmp_path: Path, text: str, *, name: str = "c.yaml") -> tuple[YamlDocument, Path]:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return YamlDocument.load(path), path


class TestLoad:
    def test_duplicate_key_names_the_key(self, tmp_path: Path) -> None:
        """E6: clave duplicada → YamlDocumentError que menciona `workers`."""
        path = tmp_path / "c.yaml"
        path.write_text("cmis:\n  workers: 4\n  workers: 5\n")
        with pytest.raises(YamlDocumentError, match="workers"):
            YamlDocument.load(path)

    def test_root_must_be_a_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text("- a\n- b\n")
        with pytest.raises(YamlDocumentError, match="mapping"):
            YamlDocument.load(path)

    def test_resolves_symlinks(self, tmp_path: Path) -> None:
        real = tmp_path / "real.yaml"
        real.write_text("a: 1\n")
        link = tmp_path / "link.yaml"
        link.symlink_to(real)
        assert YamlDocument.load(link).path == real


class TestGetHasSet:
    def test_get_and_has(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "a:\n  b: 1\n  l:\n    - x\n")
        assert doc.get(("a", "b")) == 1
        assert doc.get(("a", "l", 0)) == "x"
        assert doc.get(("a", "zz")) is MISSING
        assert doc.get(("a", "l", 5)) is MISSING
        assert doc.get(("a", "b", "deeper")) is MISSING
        assert doc.has(("a", "l"))
        assert not doc.has(("nope",))

    def test_e1_crlf_and_inline_comment_survive(self, tmp_path: Path) -> None:
        """E1: cambia SÓLO el `8`; el EOL CRLF se conserva en TODAS las líneas."""
        doc, _ = _doc(tmp_path, "# top\r\ncmis:\r\n  workers: 4  # ojo\r\n")
        doc.set(("cmis", "workers"), 8)
        assert doc.text() == "# top\r\ncmis:\r\n  workers: 8  # ojo\r\n"

    def test_e2_new_top_level_key_after_blank_line_keeps_list_indent(self, tmp_path: Path) -> None:
        src = "metadata:\n  sources:\n    - name: a\n      kind: csv\n"
        doc, _ = _doc(tmp_path, src)
        doc.set(("x", "y"), 1)
        assert doc.text() == src + "\nx:\n  y: 1\n"
        # Un segundo dump NO duplica la línea en blanco.
        assert doc.text() == src + "\nx:\n  y: 1\n"

    def test_new_top_level_key_after_trailing_blank_adds_only_one(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "a: 1\n\n")
        doc.set(("b",), 2)
        assert doc.text() == "a: 1\n\nb: 2\n"

    def test_list_with_zero_offset_is_preserved(self, tmp_path: Path) -> None:
        src = "items:\n- a\n- b\nx: 1\n"
        doc, _ = _doc(tmp_path, src)
        doc.set(("x",), 2)
        assert doc.text() == "items:\n- a\n- b\nx: 2\n"

    def test_e3_nested_dict_becomes_block_and_delete_removes_it(self, tmp_path: Path) -> None:
        config, yaml_path = _make_config(tmp_path)
        before = yaml_path.read_text()
        doc = YamlDocument.load(yaml_path)
        doc.set(
            ("connections", "clientes_sql"),
            {"kind": "mssql", "host": "h", "port": 1433, "database": "m", "encrypt": True},
        )
        yaml_path.write_text(doc.text())
        assert yaml_path.read_text().endswith(
            "\nconnections:\n  clientes_sql:\n    kind: mssql\n    host: h\n"
            "    port: 1433\n    database: m\n    encrypt: true\n"
        )
        conn = load_config(yaml_path).connections["clientes_sql"]
        assert isinstance(conn, MssqlConnectionConfig)
        assert conn.host == "h" and conn.encrypt is True

        doc.delete(("connections", "clientes_sql"))
        assert doc.text() == before + "\nconnections: {}\n"

    def test_set_none_writes_null(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "a: 1\n")
        doc.set(("a",), None)
        assert doc.text() == "a: null\n"

    def test_set_index_out_of_range_is_an_error(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "l:\n  - a\n")
        with pytest.raises(YamlDocumentError):
            doc.set(("l", 3), "b")

    def test_set_through_a_scalar_is_an_error(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "a: 1\n")
        with pytest.raises(YamlDocumentError):
            doc.set(("a", "b"), 1)

    def test_delete_missing_key_is_a_noop_and_bad_index_is_an_error(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "a: 1\nl:\n  - x\n")
        doc.delete(("nope", "deeper"))
        assert doc.text() == "a: 1\nl:\n  - x\n"
        with pytest.raises(YamlDocumentError):
            doc.delete(("l", 4))

    def test_e4_set_list_element_and_append(self, tmp_path: Path) -> None:
        src = (
            "metadata:\n  sources:\n    - kind: csv\n      alias: a\n"
            "    - kind: csv\n      alias: b\n"
        )
        doc, _ = _doc(tmp_path, src)
        doc.set(("metadata", "sources", 0, "as400_connection"), "rvi")
        assert doc.text() == (
            "metadata:\n  sources:\n    - kind: csv\n      alias: a\n      as400_connection: rvi\n"
            "    - kind: csv\n      alias: b\n"
        )
        doc.append(("metadata", "sources"), {"kind": "csv", "alias": "c"})
        tail = "    - kind: csv\n      alias: b\n    - kind: csv\n      alias: c\n"
        assert doc.text().endswith(tail)

    def test_set_at_len_appends(self, tmp_path: Path) -> None:
        """139: ``diff_edits`` emite ``Edit((…, len), item)`` para un item nuevo."""
        doc, _ = _doc(tmp_path, "l:\n  - a\n")
        doc.set(("l", 1), "b")
        assert doc.text() == "l:\n  - a\n  - b\n"
        with pytest.raises(YamlDocumentError):
            doc.set(("l", 5), "c")

    def test_append_creates_the_sequence(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "a: 1\n")
        doc.append(("a2", "items"), 5)
        doc.append(("a2", "items"), 6)
        assert doc.text() == "a: 1\n\na2:\n  items:\n    - 5\n    - 6\n"

    def test_append_to_a_mapping_is_an_error(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "a:\n  b: 1\n")
        with pytest.raises(YamlDocumentError):
            doc.append(("a",), 1)


class TestWrite:
    def test_e5_verify_failure_leaves_everything_intact(self, tmp_path: Path) -> None:
        src = "a: 1\n"
        doc, path = _doc(tmp_path, src)
        doc.set(("a",), 2)

        def boom(_: Path) -> None:
            raise ValueError("x")

        with pytest.raises(YamlWriteError, match="x"):
            doc.write(verify=boom)
        assert path.read_text() == src
        assert not list(tmp_path.glob("*.bak-*"))
        assert not list(tmp_path.glob(".c.yaml.*"))

    def test_e5_success_returns_value_backup_and_keeps_mode(self, tmp_path: Path) -> None:
        doc, path = _doc(tmp_path, "a: 1\n")
        path.chmod(0o600)
        doc.set(("a",), 2)
        seen: list[Path] = []

        def verify(tmp: Path) -> str:
            seen.append(tmp)
            return tmp.read_text()

        result = doc.write(verify=verify)
        assert result.value == "a: 2\n"
        assert result.path == path
        assert seen[0].parent == tmp_path and seen[0].name.startswith(".c.yaml.")
        assert path.read_text() == "a: 2\n"
        assert path.stat().st_mode & 0o777 == 0o600
        assert result.backup_path.name.startswith("c.yaml.bak-")
        assert result.backup_path.read_text() == "a: 1\n"
        assert not list(tmp_path.glob(".c.yaml.*"))

    def test_e5_symlink_stays_a_symlink_and_real_file_changes(self, tmp_path: Path) -> None:
        real = tmp_path / "real.yaml"
        real.write_text("a: 1\n")
        link = tmp_path / "link.yaml"
        link.symlink_to(real)
        doc = YamlDocument.load(link)
        doc.set(("a",), 2)
        result = doc.write(verify=lambda p: None)
        assert link.is_symlink()
        assert real.read_text() == "a: 2\n"
        assert result.backup_path.parent == tmp_path
        assert result.backup_path.name.startswith("real.yaml.bak-")

    def test_second_write_in_the_same_second_gets_a_distinct_backup(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "a: 1\n")
        doc.set(("a",), 2)
        first = doc.write(verify=lambda p: None)
        doc.set(("a",), 3)
        second = doc.write(verify=lambda p: None)
        assert first.backup_path != second.backup_path
        assert first.backup_path.read_text() == "a: 1\n"
        assert second.backup_path.read_text() == "a: 2\n"
        assert len(list(tmp_path.glob("c.yaml.bak-*"))) == 2


class TestHelpers:
    def test_apply_edits_sets_and_deletes(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, "a: 1\nb: 2\n")
        apply_edits(doc, [Edit(("a",), 9), Edit(("b",), DELETE), Edit(("c", "d"), True)])
        assert doc.text() == "a: 9\n\nc:\n  d: true\n"

    def test_edit_is_frozen(self) -> None:
        edit = Edit(("a",), 1)
        with pytest.raises(AttributeError):
            edit.value = 2  # type: ignore[misc]

    def test_to_plain_converts_containers_recursively(self, tmp_path: Path) -> None:
        doc, _ = _doc(tmp_path, 'a:\n  b: [1, "two"]\n  c:\n    d: null\n')
        node = doc.get(("a",))
        assert isinstance(node, CommentedMap)
        assert isinstance(node["b"], CommentedSeq)
        plain = to_plain(node)
        assert plain == {"b": [1, "two"], "c": {"d": None}}
        assert type(plain) is dict
        assert type(plain["b"]) is list  # type: ignore[index]
        assert type(plain["c"]) is dict  # type: ignore[index]
        assert to_plain(7) == 7


def _first_scalar(node: object, prefix: tuple[str | int, ...] = ()) -> tuple[str | int, ...]:
    if isinstance(node, CommentedMap):
        for key, value in node.items():
            found = _first_scalar(value, (*prefix, key))
            if found:
                return found
        return ()
    if isinstance(node, CommentedSeq):
        for i, value in enumerate(node):
            found = _first_scalar(value, (*prefix, i))
            if found:
                return found
        return ()
    return prefix


@pytest.mark.parametrize("sample", _SAMPLES, ids=lambda p: p.name)
def test_e7_samples_survive_a_one_line_edit(sample: Path) -> None:
    """E7: load + set de un escalar existente + text() → diff de UNA línea."""
    original = sample.read_bytes().decode("utf-8")
    doc = YamlDocument.load(sample)
    path = _first_scalar(doc.root)
    assert path, sample
    before = doc.get(path)
    replacement: object = "cambiado" if isinstance(before, str) else 777
    doc.set(path, replacement)
    after = doc.text()
    pairs = zip(original.splitlines(), after.splitlines(), strict=True)
    changed = [(a, b) for a, b in pairs if a != b]
    assert len(changed) == 1, changed
    assert str(replacement) in changed[0][1]
    assert doc.get(path) == replacement
