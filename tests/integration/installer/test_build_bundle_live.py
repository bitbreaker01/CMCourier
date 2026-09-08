"""142 — Instalador offline: build + install end-to-end contra la red real.

Gateado por ``CMCOURIER_INSTALLER_LIVE=1``: necesita internet (para que
``uv export`` + ``pip download`` traigan wheels reales) y, para el caso
Windows, ``pwsh``. No se corre en CI por defecto — es un smoke test manual
antes de confiar en un release del instalador (ver riesgos, spec 142).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("CMCOURIER_INSTALLER_LIVE") != "1",
        reason="set CMCOURIER_INSTALLER_LIVE=1 (needs internet, and pwsh for the Windows case)",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[3]

_ALLOWED_TOP_LEVEL = {
    "wheels",
    "config",
    "reference-data",
    "requirements.txt",
    "requirements-download.txt",
    "install.sh",
    "install.bat",
    "INSTALL.txt",
    "README.md",
}


def _project_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return str(data["project"]["version"])


def _extract_zip(zip_path: Path, dest: Path) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    entries = list(dest.iterdir())
    assert len(entries) == 1, f"Esperaba un único directorio del bundle, encontré: {entries}"
    return entries[0]


def test_linux_bundle_builds_and_installs(tmp_path: Path):
    output_dir = tmp_path / "dist-offline"
    result = subprocess.run(
        ["bash", "installer/build-offline-bundle.sh", "--output-dir", str(output_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    zips = list(output_dir.glob("*.zip"))
    assert len(zips) == 1, f"Esperaba un único .zip, encontré: {zips}"

    extract_dir = tmp_path / "extracted"
    bundle_dir = _extract_zip(zips[0], extract_dir)

    top_level_names = {p.name for p in bundle_dir.iterdir()}
    assert top_level_names <= _ALLOWED_TOP_LEVEL, f"Archivos inesperados: {top_level_names}"

    wheels_dir = bundle_dir / "wheels"
    project_wheels = list(wheels_dir.glob("cmcourier-*.whl"))
    assert len(project_wheels) == 1, f"Esperaba un único wheel, encontré: {project_wheels}"

    install_result = subprocess.run(
        ["bash", "install.sh"],
        cwd=bundle_dir,
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert install_result.returncode == 0, install_result.stdout + install_result.stderr

    version_result = subprocess.run(
        [str(bundle_dir / ".venv" / "bin" / "cmcourier"), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert _project_version() in version_result.stdout


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh no está instalado")
def test_windows_bundle_builds_with_pwsh(tmp_path: Path):
    output_dir = tmp_path / "dist-offline"
    result = subprocess.run(
        [
            "pwsh",
            "-File",
            "installer/build-offline-bundle.ps1",
            "-PythonVersion",
            "3.11",
            "-OutputDir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    version = _project_version()
    expected_zip = output_dir / f"cmcourier-offline-{version}-py3.11-win_amd64.zip"
    assert expected_zip.is_file(), list(output_dir.glob("*.zip"))

    extract_dir = tmp_path / "extracted"
    bundle_dir = _extract_zip(expected_zip, extract_dir)

    wheels_dir = bundle_dir / "wheels"
    project_wheels = list(wheels_dir.glob("cmcourier-*.whl"))
    assert len(project_wheels) == 1, f"Esperaba un único wheel, encontré: {project_wheels}"
    assert list(wheels_dir.glob("colorama-*.whl")), "Falta colorama en wheels/"
    pyodbc_whl = list(wheels_dir.glob("pyodbc-*-cp311-*-win_amd64.whl"))
    assert pyodbc_whl, "Falta pyodbc cp311 win_amd64 en wheels/"

    install_bat_text = (bundle_dir / "install.bat").read_text()
    assert "TARGET_VERSION" in install_bat_text
    assert "3.11" in install_bat_text
