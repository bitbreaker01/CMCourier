"""142 — Instalador offline: plantillas versionadas + hardening de los scripts.

Sin red, rápidos. Los tests que EJECUTAN ``install.sh`` renderizado corren
bash real (disponible en el runner) contra intérpretes falsos, así no
dependen de tener las versiones exactas de Python instaladas.

``install.bat`` sólo se verifica por TEXTO: no hay ``cmd.exe`` en este
runner Linux, así que su lógica queda validada por simetría con
``install.sh`` (misma resolución de intérprete, mismos mensajes) — ver
REQ-006 / riesgos en la spec 142.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = REPO_ROOT / "installer" / "templates"
SH_TMPL = TEMPLATES_DIR / "install.sh.tmpl"
BAT_TMPL = TEMPLATES_DIR / "install.bat.tmpl"
SH_SCRIPT = REPO_ROOT / "installer" / "build-offline-bundle.sh"
PS1_SCRIPT = REPO_ROOT / "installer" / "build-offline-bundle.ps1"


def _render(tmpl_path: Path, python_version: str, project_version: str = "0.113.0") -> str:
    content = tmpl_path.read_text()
    content = content.replace("@@PYTHON_VERSION@@", python_version)
    content = content.replace("@@PROJECT_VERSION@@", project_version)
    return content.replace("@@PYTHON_VERSION_NODOT@@", python_version.replace(".", ""))


def _fake_interpreter(path: Path, version: str) -> None:
    """Un intérprete falso que ignora sus argumentos y siempre imprime `version`."""
    path.write_text(f"#!/bin/sh\necho '{version}'\n")
    path.chmod(0o755)


# ---------------------------------------------------------------------------
# Las plantillas existen y tienen la resolución de intérprete en tres pasos
# ---------------------------------------------------------------------------


def test_templates_exist():
    assert SH_TMPL.is_file()
    assert BAT_TMPL.is_file()


def test_install_sh_tmpl_resolution_order():
    text = SH_TMPL.read_text()
    assert '-n "${PYTHON:-}"' in text
    assert "python${TARGET_VERSION}" in text
    assert "python3" in text


def test_install_sh_tmpl_version_check_and_venv_refusal():
    text = SH_TMPL.read_text()
    assert "TARGET_VERSION" in text
    assert "@@PYTHON_VERSION@@" in text
    assert "PYTHON=/usr/bin/python@@PYTHON_VERSION@@ bash install.sh" in text
    assert "Borralo" in text
    assert "rm -rf .venv" in text


def test_install_bat_tmpl_resolution_order():
    text = BAT_TMPL.read_text()
    assert "if defined PYTHON" in text
    assert "py -@@PYTHON_VERSION@@" in text
    assert "where python" in text


def test_install_bat_tmpl_version_check_and_venv_refusal():
    text = BAT_TMPL.read_text()
    assert "TARGET_VERSION" in text
    assert "set PYTHON=C:\\Python@@PYTHON_VERSION_NODOT@@\\python.exe" in text
    assert "rmdir /s /q .venv" in text


def test_install_bat_tmpl_is_ascii_with_lf_source():
    """Se escribe con `-Encoding ASCII`: cualquier byte no-ASCII saldría como `?`."""
    raw = BAT_TMPL.read_bytes()
    assert b"\r" not in raw
    raw.decode("ascii")


def test_install_bat_tmpl_quotes_interpreter_path_and_avoids_percent_formats():
    """`PYTHON=C:\\Program Files\\...\\python.exe` tiene espacios: el ejecutable va
    entre comillas y los argumentos del launcher (`-3.11`) separados. Y el probe
    de versión no usa `%` (que cmd re-expande) ni comillas simples anidadas
    dentro de `for /f '...'` — va con `usebackq`."""
    text = BAT_TMPL.read_text()
    assert '"!PY!" !PYARGS!' in text
    assert 'set "PYARGS=-@@PYTHON_VERSION@@"' in text
    assert "usebackq" in text
    assert "%%d" not in text
    assert "'.'.join(map(str, sys.version_info[:2]))" in text


def test_ps1_renders_install_bat_with_crlf():
    """cmd.exe parsea mal `goto`/bloques con LF solo; la plantilla vive en git con LF."""
    text = PS1_SCRIPT.read_text()
    assert '-replace "`r?`n", "`r`n"' in text


# ---------------------------------------------------------------------------
# Los scripts de build: REQ-001 / REQ-002
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [SH_SCRIPT, PS1_SCRIPT])
def test_build_scripts_use_no_emit_project(script: Path):
    assert "--no-emit-project" in script.read_text()


@pytest.mark.parametrize("script", [SH_SCRIPT, PS1_SCRIPT])
def test_build_scripts_derive_requirements_download(script: Path):
    assert "requirements-download.txt" in script.read_text()


def test_sh_requests_manylinux_2_28_on_cross_target():
    assert "manylinux_2_28_x86_64" in SH_SCRIPT.read_text()


@pytest.mark.parametrize("script", [SH_SCRIPT, PS1_SCRIPT])
def test_build_scripts_fail_on_more_than_one_project_wheel(script: Path):
    text = script.read_text()
    assert "cmcourier-*.whl" in text
    # La comprobación de conteo debe aparecer además de la referencia al wheel
    # original (build/copy) — dos o más ocurrencias del patrón.
    assert text.count("cmcourier-*.whl") >= 2


def test_sh_references_install_sh_template():
    assert "install.sh.tmpl" in SH_SCRIPT.read_text()


def test_ps1_references_install_bat_template():
    assert "install.bat.tmpl" in PS1_SCRIPT.read_text()


# ---------------------------------------------------------------------------
# INSTALL.txt: REQ-004
# ---------------------------------------------------------------------------


def test_sh_install_txt_has_linux_prerequisites():
    text = SH_SCRIPT.read_text()
    assert "glibc" in text
    assert "unixODBC" in text
    assert "ODBC Driver 18 for SQL Server" in text


def test_ps1_install_txt_has_windows_prerequisites():
    text = PS1_SCRIPT.read_text()
    assert "python.org" in text
    assert "Redistributable" in text
    assert "ODBC Driver 18 for SQL Server" in text


# ---------------------------------------------------------------------------
# install.sh renderizado y EJECUTADO — REQ-003 / REQ-006
# ---------------------------------------------------------------------------


def test_install_sh_wrong_python_version_exits_1(tmp_path: Path):
    rendered = _render(SH_TMPL, python_version="3.11")
    install_sh = tmp_path / "install.sh"
    install_sh.write_text(rendered)
    install_sh.chmod(0o755)
    (tmp_path / "wheels").mkdir()

    fake_python = tmp_path / "fake-python-3.9"
    _fake_interpreter(fake_python, "3.9")

    result = subprocess.run(
        ["bash", "install.sh"],
        cwd=tmp_path,
        env={**os.environ, "PYTHON": str(fake_python)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "Python 3.9" in output
    assert "esperaba Python 3.11" in output
    assert "PYTHON=/usr/bin/python3.11 bash install.sh" in output


def test_install_sh_correct_version_empty_wheels_reaches_pip_step(tmp_path: Path):
    running_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    rendered = _render(SH_TMPL, python_version=running_version)
    install_sh = tmp_path / "install.sh"
    install_sh.write_text(rendered)
    install_sh.chmod(0o755)
    (tmp_path / "wheels").mkdir()

    result = subprocess.run(
        ["bash", "install.sh"],
        cwd=tmp_path,
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = result.stdout + result.stderr
    # Pasa la comprobación de versión y llega al paso de pip install, que
    # falla porque wheels/ está vacío (no hay cmcourier para instalar).
    assert "Instalando cmcourier y dependencias" in output
    assert result.returncode == 1


def test_install_sh_venv_with_other_version_asks_to_delete_it(tmp_path: Path):
    rendered = _render(SH_TMPL, python_version="3.11")
    install_sh = tmp_path / "install.sh"
    install_sh.write_text(rendered)
    install_sh.chmod(0o755)
    (tmp_path / "wheels").mkdir()

    fake_python = tmp_path / "fake-python-3.11"
    _fake_interpreter(fake_python, "3.11")

    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _fake_interpreter(venv_bin / "python", "3.10")

    result = subprocess.run(
        ["bash", "install.sh"],
        cwd=tmp_path,
        env={**os.environ, "PYTHON": str(fake_python)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert ".venv existente es Python 3.10" in output
    assert "Borralo" in output
    assert "rm -rf .venv" in output
