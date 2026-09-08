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


def _fake_interpreter(path: Path, version: str, bits: int = 64) -> None:
    """Un intérprete falso que ignora sus argumentos y siempre imprime `version bits`
    (lo mismo que imprime el probe real: ``3.11 64``)."""
    path.write_text(f"#!/bin/sh\necho '{version} {bits}'\n")
    path.chmod(0o755)


def _run_install_sh(tmp_path: Path, python_version: str, env: dict[str, str]) -> tuple[int, str]:
    rendered = _render(SH_TMPL, python_version=python_version)
    install_sh = tmp_path / "install.sh"
    install_sh.write_text(rendered)
    install_sh.chmod(0o755)
    (tmp_path / "wheels").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", "install.sh"],
        cwd=tmp_path,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode, result.stdout + result.stderr


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


_BAT_PROBE = (
    "import sys, struct; print('.'.join(map(str, sys.version_info[:2])), struct.calcsize('P')*8)"
)


def test_install_bat_tmpl_probe_is_a_plain_command_line_not_a_for_f_command():
    """Antagonista 142 (I3/M7): el probe corre como línea de comando normal
    redirigida a un archivo temporal — sin `for /f` sobre un comando (ahí
    `cmd /c` re-parsea comillas y el `2>nul` se pierde). Se fija la línea
    ENTERA porque no hay cmd.exe para ejecutarla."""
    text = BAT_TMPL.read_text()
    probe_line = f'"%~1" %~2 -c "{_BAT_PROBE}" > "%TEMP%\\cmcourier-pyver.txt" 2>nul'
    assert probe_line in text
    assert 'for /f "usebackq tokens=1,2" %%a in ("%TEMP%\\cmcourier-pyver.txt")' in text
    assert "usebackq delims=" not in text
    assert 'call :probe "!PY!" "!PYARGS!"' in text
    assert 'call :probe ".venv\\Scripts\\python.exe" ""' in text


def test_install_bat_tmpl_checks_64_bit_and_venv_integrity():
    """I1: Python 3.11 de 32 bits pasa el check de major.minor y muere en pip.
    M6: un .venv sin Scripts\\python.exe se "reusa" y muere con mensaje engañoso."""
    text = BAT_TMPL.read_text()
    assert 'if not "!FOUND_BITS!"=="64"' in text
    assert "64-bit" in text
    assert "if exist .venv (" in text
    assert "if not exist .venv\\Scripts\\python.exe (" in text


def test_install_bat_tmpl_aborts_when_cd_fails_and_pauses_on_double_click():
    """I2: `cd /d` a una ruta UNC falla y el .venv se crearía en C:\\Windows.
    M13: con doble clic la ventana se cierra sin que se lea el error."""
    text = BAT_TMPL.read_text()
    assert 'cd /d "%INSTALL_DIR%" || goto :fail' in text
    assert "exit /b 1" not in text.split(":fail")[0]
    assert "if defined PAUSE_ON_EXIT pause" in text
    assert "%cmdcmdline%" in text


def test_install_bat_tmpl_strips_quotes_and_resolves_relative_python_before_cd():
    text = BAT_TMPL.read_text()
    assert 'set "PY=!PYTHON:"=!"' in text
    assert 'set "PY=%%~fp"' in text
    assert text.index('set "PY=%%~fp"') < text.index('cd /d "%INSTALL_DIR%"')


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


def test_sh_strips_indented_comment_lines_from_requirements_download():
    """M5: uv export emite `    # via pydantic`; sin trim quedaban 69 líneas de ruido."""
    text = SH_SCRIPT.read_text()
    assert 'line="${line#"${line%%[![:space:]]*}"}"' in text


def test_sh_always_downloads_binary_only_and_rejects_sdists():
    """M8: sin --only-binary en la rama no-cross pip puede bajar un sdist que el
    servidor air-gapped no puede compilar."""
    text = SH_SCRIPT.read_text()
    assert text.count("--only-binary=:all:") >= 2
    assert "*.tar.gz" in text


@pytest.mark.parametrize("script", [SH_SCRIPT, PS1_SCRIPT])
def test_build_scripts_fail_when_pip_wheel_staging_fails(script: Path):
    """M10: si el staging de pip falla, el bundle sale sin pip y nadie se entera."""
    text = script.read_text()
    marker = "pip setuptools wheel"
    tail = text[text.index(marker) :]
    assert ("Fail" in tail[:600]) or ("fail " in tail[:600])


def test_ps1_join_paths_handles_single_segment():
    text = PS1_SCRIPT.read_text()
    assert "if ($Segments.Count -lt 2) { return $Segments[0] }" in text


def test_ps1_install_txt_interpolates_python_dir_example():
    """M13: decía `C:\\PythonXXX\\python.exe` con XXX literal."""
    text = PS1_SCRIPT.read_text()
    assert "PythonXXX" not in text
    assert 'set PYTHON=C:\\Python$($PythonVersion.Replace(".", ""))\\python.exe' in text


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


def test_install_sh_32_bit_interpreter_exits_1(tmp_path: Path):
    fake = tmp_path / "fake-python-32"
    _fake_interpreter(fake, "3.11", bits=32)
    rc, output = _run_install_sh(tmp_path, "3.11", {"PYTHON": str(fake)})
    assert rc == 1
    assert "32 bits" in output
    assert "64 bits" in output


def test_install_sh_accepts_quoted_and_relative_python(tmp_path: Path):
    """M13: `PYTHON="./tools/python"` (con comillas, relativa al cwd del operador)
    tiene que resolverse ANTES del cd al directorio del bundle."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    _fake_interpreter(tools / "python", "3.9")
    rendered = _render(SH_TMPL, python_version="3.11")
    (bundle / "install.sh").write_text(rendered)
    (bundle / "wheels").mkdir()
    result = subprocess.run(
        ["bash", "bundle/install.sh"],
        cwd=tmp_path,
        env={**os.environ, "PYTHON": '"./tools/python"'},
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    # Llegó a ejecutar el intérprete (no "no se pudo ejecutar"): la versión falsa se leyó.
    assert "es Python 3.9" in output


def test_install_sh_broken_venv_asks_to_delete_it(tmp_path: Path):
    """M6: .venv sin bin/python → pedir borrarlo, no "no se pudo comprobar"."""
    fake = tmp_path / "fake-python"
    _fake_interpreter(fake, "3.11")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    rc, output = _run_install_sh(tmp_path, "3.11", {"PYTHON": str(fake)})
    assert rc == 1
    assert "rm -rf .venv" in output


def test_install_sh_reports_when_venv_creation_leaves_no_python(tmp_path: Path):
    """M13: `python3.11 -m venv` sin python3-venv puede dejar un .venv a medias."""
    fake = tmp_path / "fake-python"
    # Devuelve 0 en `-m venv` pero no crea nada: el .venv queda sin bin/python.
    fake.write_text("#!/bin/sh\ncase \"$1\" in -c) echo '3.11 64';; -m) mkdir -p .venv;; esac\n")
    fake.chmod(0o755)
    rc, output = _run_install_sh(tmp_path, "3.11", {"PYTHON": str(fake)})
    assert rc == 1
    assert ".venv/bin/python" in output
    assert "python3-venv" in output


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
