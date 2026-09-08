"""Escribir los overrides de [3] al YAML (135, migrado a `YamlDocument` en 137).

Round-trip con ruamel + verificación semántica: lo que NO se toca queda
byte-idéntico, y si el resultado no carga EXACTAMENTE como
``apply_overrides(config, ov)``, el archivo original no se modifica.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cmcourier.cli.console.overrides import SessionOverrides, apply_overrides
from cmcourier.cli.console.persist import PersistError, persist_overrides
from cmcourier.config.loader import load_config
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = pytest.mark.unit


def _yaml_with(yaml_path: Path, extra: str) -> None:
    """Agrega ``extra`` dentro del bloque ``cmis`` del fixture."""
    text = yaml_path.read_text()
    anchor = "  repo_id: repo\n"
    assert anchor in text
    yaml_path.write_text(text.replace(anchor, anchor + extra))


class TestPatch:
    def test_replaces_value_and_keeps_inline_comment(self, tmp_path: Path) -> None:
        """E1: la línea cambia el valor, conserva el comentario, el resto es idéntico."""
        config, yaml_path = _make_config(tmp_path)
        _yaml_with(yaml_path, "  workers: 4  # ojo con prd\n")
        config = load_config(yaml_path)
        before = yaml_path.read_text()

        result = persist_overrides(yaml_path, config, SessionOverrides(workers=8))

        after = yaml_path.read_text()
        assert "  workers: 8  # ojo con prd\n" in after
        restored = after.replace("  workers: 8  # ojo con prd\n", "  workers: 4  # ojo con prd\n")
        assert restored == before
        assert load_config(yaml_path).cmis.workers == 8
        assert result.config.cmis.workers == 8
        assert result.changed == {"cmis.workers": 8}

    def test_appends_missing_key_to_existing_block(self, tmp_path: Path) -> None:
        """El bloque cmis existe pero no tiene workers → se agrega al final del bloque."""
        config, yaml_path = _make_config(tmp_path)
        persist_overrides(yaml_path, config, SessionOverrides(workers=8))
        text = yaml_path.read_text()
        assert "  repo_id: repo\n  workers: 8\n" in text
        assert load_config(yaml_path).cmis.workers == 8

    def test_appends_missing_block_at_end(self, tmp_path: Path) -> None:
        """E2: no hay bloque processing → se agrega entero al final."""
        config, yaml_path = _make_config(tmp_path)
        persist_overrides(yaml_path, config, SessionOverrides(mode="streaming", bucket_size=50))
        text = yaml_path.read_text()
        assert text.endswith("processing:\n  mode: streaming\n  streaming:\n    bucket_size: 50\n")
        loaded = load_config(yaml_path)
        assert loaded.processing.mode == "streaming"
        assert loaded.processing.streaming.bucket_size == 50

    def test_all_seven_scalars_round_trip(self, tmp_path: Path) -> None:
        config, yaml_path = _make_config(tmp_path)
        ov = SessionOverrides(
            mode="streaming",
            prep_workers=3,
            bucket_size=25,
            workers=6,
            auto_tune_enabled=True,
            max_bandwidth_mbps=12.5,
            unmask_pii=True,
        )
        result = persist_overrides(yaml_path, config, ov)
        assert load_config(yaml_path) == apply_overrides(config, ov)
        assert set(result.changed) == {
            "processing.mode",
            "processing.prep_workers",
            "processing.streaming.bucket_size",
            "cmis.workers",
            "cmis.auto_tune.enabled",
            "cmis.max_bandwidth_mbps",
            "observability.unmask_pii",
        }

    def test_trigger_override_is_not_persisted(self, tmp_path: Path) -> None:
        from cmcourier.config.schema import LocalScanTriggerConfig

        config, yaml_path = _make_config(tmp_path)
        trigger = LocalScanTriggerConfig(kind="local_scan", scan_path=tmp_path)
        ov = SessionOverrides(workers=2, trigger=trigger)
        persist_overrides(yaml_path, config, ov)
        assert "local_scan" not in yaml_path.read_text()
        assert load_config(yaml_path).trigger.kind == "csv"

    def test_empty_overrides_is_an_error(self, tmp_path: Path) -> None:
        config, yaml_path = _make_config(tmp_path)
        with pytest.raises(PersistError, match="nada"):
            persist_overrides(yaml_path, config, SessionOverrides())


class TestSafety:
    def test_creates_timestamped_backup(self, tmp_path: Path) -> None:
        config, yaml_path = _make_config(tmp_path)
        before = yaml_path.read_text()
        result = persist_overrides(yaml_path, config, SessionOverrides(workers=8))
        assert result.backup_path.name.startswith("config.yaml.bak-")
        assert result.backup_path.read_text() == before

    def test_flow_style_block_is_written_in_place(self, tmp_path: Path) -> None:
        """137 (REQ-004): `cmis: {…}` ya no se rechaza — ruamel setea dentro del flow mapping."""
        config, yaml_path = _make_config(tmp_path)
        flow = "cmis: {base_url: http://cm.test/cmis, repo_id: repo}\n"
        text = yaml_path.read_text().replace(
            "cmis:\n  base_url: http://cm.test/cmis\n  repo_id: repo\n", flow
        )
        yaml_path.write_text(text)
        config = load_config(yaml_path)

        persist_overrides(yaml_path, config, SessionOverrides(workers=8))

        after = yaml_path.read_text()
        assert after != text
        assert load_config(yaml_path).cmis.workers == 8
        patched = "cmis: {base_url: http://cm.test/cmis, repo_id: repo, workers: 8}\n"
        assert patched in after
        assert after.replace(patched, flow) == text
        assert not list(tmp_path.glob(".config.yaml.*"))

    def test_symlink_writes_through_to_the_real_file(self, tmp_path: Path) -> None:
        """Antagonista B2: Path.replace sobre un symlink lo pisaba y el real quedaba igual."""
        config, real = _make_config(tmp_path)
        link = tmp_path / "link.yaml"
        link.symlink_to(real)

        result = persist_overrides(link, config, SessionOverrides(workers=8))

        assert link.is_symlink()
        assert load_config(real).cmis.workers == 8
        assert result.backup_path.parent == real.parent
        assert result.backup_path.name.startswith("config.yaml.bak-")

    def test_preserves_file_mode(self, tmp_path: Path) -> None:
        """Antagonista B3: el archivo nuevo salía con el umask, no con el modo original."""
        config, yaml_path = _make_config(tmp_path)
        yaml_path.chmod(0o600)
        persist_overrides(yaml_path, config, SessionOverrides(workers=8))
        assert yaml_path.stat().st_mode & 0o777 == 0o600

    def test_preserves_crlf_line_endings(self, tmp_path: Path) -> None:
        """Antagonista I5: read_text/write_text normalizaban y el archivo entero cambiaba."""
        config, yaml_path = _make_config(tmp_path)
        crlf = yaml_path.read_text().replace("\n", "\r\n").encode()
        yaml_path.write_bytes(crlf)
        persist_overrides(yaml_path, config, SessionOverrides(workers=8, mode="streaming"))
        data = yaml_path.read_bytes()
        assert b"\r\n" in data
        assert data.count(b"\n") == data.count(b"\r\n")
        assert b"  workers: 8\r\n" in data
        assert data.endswith(b"processing:\r\n  mode: streaming\r\n")
        assert load_config(yaml_path).cmis.workers == 8

    def test_m2_unwritable_directory_raises_persist_error(self, tmp_path: Path) -> None:
        """M2: el OSError de la escritura atómica salía crudo por la [3] — la
        pane sólo atrapa PersistError, así que el toast era un traceback."""
        if os.geteuid() == 0:
            pytest.skip("como root todo directorio es escribible")
        config, yaml_path = _make_config(tmp_path)
        before = yaml_path.read_text()
        tmp_path.chmod(0o500)
        try:
            with pytest.raises(PersistError):
                persist_overrides(yaml_path, config, SessionOverrides(workers=8))
        finally:
            tmp_path.chmod(0o700)
        assert yaml_path.read_text() == before
        assert not list(tmp_path.glob("config.yaml.bak-*"))

    def test_duplicate_key_refuses_to_write(self, tmp_path: Path) -> None:
        """137: ruamel rechaza la clave duplicada al CARGAR → PersistError que la nombra."""
        config, yaml_path = _make_config(tmp_path)
        _yaml_with(yaml_path, "  workers: 4\n  workers: 5\n")
        config = load_config(yaml_path)
        text = yaml_path.read_text()
        with pytest.raises(PersistError, match="workers"):
            persist_overrides(yaml_path, config, SessionOverrides(workers=8))
        assert yaml_path.read_text() == text
        assert not list(tmp_path.glob("config.yaml.bak-*"))
