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

_COMMON_TOP_LEVEL = {
    "wheels",
    "config",
    "reference-data",
    "requirements.txt",
    "requirements-download.txt",
    "INSTALL.txt",
    "README.md",
}
_LINUX_TOP_LEVEL = _COMMON_TOP_LEVEL | {"install.sh"}
_WINDOWS_TOP_LEVEL = _COMMON_TOP_LEVEL | {"install.bat"}

# uv.lock hoy resuelve ~55 wheels (deps + pip/setuptools/wheel + cmcourier).
# Un bundle con muchos menos es un pip download que se cortó a mitad.
_MIN_WHEEL_COUNT = 49


def _project_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return str(data["project"]["version"])


def _extract_zip(zip_path: Path, dest: Path) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    entries = list(dest.iterdir())
    assert len(entries) == 1, f"Esperaba un único directorio del bundle, encontré: {entries}"
    return entries[0]


def _assert_wheelhouse(wheels_dir: Path, *, py_tag: str) -> None:
    """Un wheelhouse sano: un solo cmcourier, bastantes wheels, ninguno de OTRA cpXY."""
    wheels = sorted(p.name for p in wheels_dir.glob("*.whl"))
    assert len(wheels) >= _MIN_WHEEL_COUNT, f"Sólo {len(wheels)} wheels: {wheels}"
    assert not list(wheels_dir.glob("*.tar.gz")), "Hay sdists en wheels/ (no instalan offline)"
    project_wheels = [w for w in wheels if w.startswith("cmcourier-")]
    assert len(project_wheels) == 1, f"Esperaba un único wheel, encontré: {project_wheels}"
    foreign = [
        w
        for w in wheels
        if "-cp3" in w and f"-{py_tag}-" not in w and "-abi3-" not in w and "-none-" not in w
    ]
    assert not foreign, f"Wheels de otra versión de Python (esperaba {py_tag}): {foreign}"


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
    assert top_level_names == _LINUX_TOP_LEVEL, f"Contenido inesperado: {top_level_names}"

    wheels_dir = bundle_dir / "wheels"
    host_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    _assert_wheelhouse(wheels_dir, py_tag=host_tag)
    assert list(wheels_dir.glob(f"pyodbc-*-{host_tag}-*-manylinux*x86_64.whl")), (
        f"Falta pyodbc {host_tag} manylinux en wheels/"
    )

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

    top_level_names = {p.name for p in bundle_dir.iterdir()}
    assert top_level_names == _WINDOWS_TOP_LEVEL, f"Contenido inesperado: {top_level_names}"

    wheels_dir = bundle_dir / "wheels"
    _assert_wheelhouse(wheels_dir, py_tag="cp311")
    assert list(wheels_dir.glob("colorama-*.whl")), "Falta colorama en wheels/"
    pyodbc_whl = list(wheels_dir.glob("pyodbc-*-cp311-*-win_amd64.whl"))
    assert pyodbc_whl, "Falta pyodbc cp311 win_amd64 en wheels/"
    assert not list(wheels_dir.glob("*linux*.whl")), "Wheels de Linux en un bundle Windows"

    install_bat_bytes = (bundle_dir / "install.bat").read_bytes()
    assert b"\r\n" in install_bat_bytes, "install.bat sin CRLF (cmd.exe se marea con LF)"
    assert b"\n" not in install_bat_bytes.replace(b"\r\n", b""), "install.bat con LF sueltos"
    install_bat_text = install_bat_bytes.decode("ascii")
    assert 'set "TARGET_VERSION=3.11"' in install_bat_text
    assert "py -3.11 -c" in install_bat_text
    assert "@@" not in install_bat_text, "quedaron placeholders sin renderizar"
    assert "struct.calcsize('P')*8" in install_bat_text

    install_txt = (bundle_dir / "INSTALL.txt").read_bytes()
    assert b"\n" not in install_txt.replace(b"\r\n", b""), "INSTALL.txt con finales mezclados"
    assert b"set PYTHON=C:\\Python311\\python.exe" in install_txt
