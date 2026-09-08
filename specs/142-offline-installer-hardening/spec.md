# 142 — Instalador offline: que el bundle se arme y que instale en el servidor

## Por qué

El operador va a instalar CMCourier en un **Windows Server sin internet**
con una versión de Python específica. La spec 101 dejó el flujo (armar
el bundle en una máquina con internet → `.zip` → `install.bat`), pero
verificando los scripts contra la realidad (2026-09-08, `uv 0.11.6`,
`uv.lock` de 0.113.0) aparecieron fallas que hoy impiden que el bundle
se arme, o que instale una vez en el servidor:

1. **El build FALLA (exit 2).** `uv export` emite el propio proyecto
   como `-e .` en `requirements.txt`; `pip download --only-binary=:all:
   -r requirements.txt` intenta ARCHIVAR el checkout entero como sdist
   (incluido `sample/`, con sus PDFs) y revienta en un
   `FileNotFoundError` sobre `sample/local-scan-pool/…`. Si no
   reventara sería peor: empaquetaría el working directory completo —
   configs, datos — dentro de `wheels/`. Reproducido con el comando
   exacto del paso 6 de ambos scripts.
2. **Los markers se evalúan contra la máquina de build, no contra el
   destino.** `colorama==0.4.6 ; sys_platform == 'win32'` NO se descarga
   si el bundle Windows se arma desde Linux/macOS, y `click` lo exige en
   Windows → `pip install` falla en el servidor con "No matching
   distribution found for colorama". Reproducido: `pip download
   --platform win_amd64` desde Linux baja 49 wheels y ninguno es
   `colorama`.
3. **`install.bat` / `install.sh` toman `python` / `python3` a ciegas.**
   Un servidor con más de un Python (el del sistema + el que instaló el
   operador) o con el alias de la Microsoft Store en `PATH` crea el
   `.venv` con el intérprete equivocado, y pip contesta un "No matching
   distribution found" que no menciona versiones. El bundle sabe para
   qué `major.minor` fue armado (está en el nombre) y no lo comprueba.
4. **Linux: el cross-download para otra versión de Python está roto.**
   El script pide sólo `manylinux_2_17`/`manylinux2014`; `numpy 2.4.4`
   (pineado) publica únicamente `manylinux_2_28`. Con `_2_28` agregado
   bajan 50/50. Consecuencia no documentada: el servidor Linux necesita
   **glibc ≥ 2.28** (RHEL/Rocky 8+, Ubuntu 20.04+, Debian 10+); RHEL 7
   es imposible con estos pins.
5. **Prerequisitos incompletos.** El wheel de `pyodbc` NO trae
   `libodbc` (verificado abriendo el wheel): en Linux hace falta
   unixODBC del sistema. Ni `INSTALL.txt` ni el how-to mencionan el
   **Microsoft ODBC Driver 18 for SQL Server** para las fuentes MSSQL
   (spec 119+), ni que en Windows el Python tiene que ser el de
   python.org x86_64 (no el de la Store). `zip` no figura como
   prerequisito del host de build Linux.

Lo que SÍ está bien y se conserva: los 49 wheels de dependencias existen
para `win_amd64` y `manylinux_2_28` en cp311 y cp312 (ninguna es
sdist-only; Pillow de 141 incluida), la guía explica el gotcha del
`major.minor`, y el layout del bundle es correcto.

## Qué

### REQ-001 — El `requirements.txt` del bundle no contiene el proyecto ni depende de markers

* Ambos scripts exportan con `uv export --no-emit-project --no-hashes
  --no-dev --format requirements-txt`. El `requirements.txt` que viaja
  en el bundle es ese (con markers, legible para humanos).
* Para el `pip download` se deriva `requirements-download.txt` con los
  markers **eliminados** (todo lo que sigue a ` ;` en cada línea): se
  descargan TODAS las dependencias del lock sin importar la plataforma
  del host. En el servidor, `pip install` evalúa los markers de verdad y
  usa sólo lo que corresponde. Un wheel de más (`colorama` en Linux) es
  inofensivo; uno de menos rompe la instalación.
* El wheel del proyecto sigue viniendo de `uv build --wheel`, único
  origen. Si `wheels/` termina con más de un `cmcourier-*.whl`, el
  script falla.

### REQ-002 — Linux: tags `manylinux_2_28` y requisito de glibc

* En la rama cross-target (`--python-version` ≠ host) el `.sh` pide
  `--platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64
  --platform manylinux2014_x86_64`, y `--abi cp3XY --abi none`
  (wheels puros `py3-none-any` con `--only-binary`).
* `INSTALL.txt` (Linux) y el how-to declaran **glibc ≥ 2.28** y
  **unixODBC** (`libodbc.so.2`) como prerequisitos del servidor.

### REQ-003 — Los instaladores eligen el intérprete correcto y lo comprueban

Los instaladores se generan desde plantillas versionadas en
`installer/templates/install.bat.tmpl` e `installer/templates/install.sh.tmpl`
(placeholders `@@PYTHON_VERSION@@`, `@@PROJECT_VERSION@@`), que los dos
scripts de build sustituyen y copian. Una sola fuente por instalador,
testeable sin armar un bundle.

Resolución del intérprete, en orden:

1. Variable de entorno `PYTHON` (ruta a un ejecutable) si está definida.
2. Windows: `py -@@PYTHON_VERSION@@` (launcher de python.org) si existe;
   Linux: `python@@PYTHON_VERSION@@` si está en `PATH`.
3. `python` (Windows) / `python3` (Linux).

Con el intérprete elegido, ANTES de crear el `.venv`, se comprueba
`major.minor` contra `@@PYTHON_VERSION@@`. Si no coincide, el
instalador sale con código 1 y un mensaje que dice qué encontró, qué
esperaba, y cómo apuntarlo (`set PYTHON=C:\Python311\python.exe` /
`PYTHON=/usr/bin/python3.11 bash install.sh`). Si ya existe un `.venv`
con OTRA versión, sale con código 1 y pide borrarlo (no lo borra solo).

El resto del flujo (upgrade de pip offline, `pip install --no-index
--find-links wheels cmcourier`, `cmcourier --version`) no cambia.

### REQ-004 — Prerequisitos completos en `INSTALL.txt` y en el how-to

Windows Server:

* Python `@@PYTHON_VERSION@@` **x86_64 de python.org** (no la Microsoft
  Store), con el launcher `py` (default del instalador).
* Microsoft Visual C++ Redistributable 2015-2022 x64.
* IBM i Access ODBC driver, si hay fuente AS400.
* **Microsoft ODBC Driver 18 for SQL Server**, si hay fuente MSSQL
  (`connections.<alias>.kind: mssql`).
* System DSN (64-bit) para cada fuente ODBC que use DSN.

Linux:

* Python `@@PYTHON_VERSION@@` x86_64 con `venv`; glibc ≥ 2.28;
  unixODBC; los mismos drivers ODBC según fuente.

Host de build (ambos): `uv`, el mismo `major.minor` que el destino (o
`--python-version`), internet, y en Linux `zip`.

### REQ-005 — `build-offline-bundle.ps1` corre bajo `pwsh` en Linux

Para poder armar y verificar el bundle Windows desde una máquina Linux
(CI, o el dev que no tiene Windows a mano): rutas con `Join-Path` (sin
`\` literales), intérprete de build `python` con fallback a `python3`,
y el `pip download` con `--platform win_amd64` que ya hace. El
`install.bat` resultante es el mismo. No se exige que corra en Windows
PowerShell 5.1 nada que no corriera antes.

### REQ-006 — Tests

* `tests/unit/installer/test_offline_bundle.py` (sin red, rápidos):
  * Las plantillas existen y contienen la resolución en tres pasos, la
    comprobación de `major.minor`, el mensaje con `PYTHON=` y la
    negativa a reusar un `.venv` de otra versión.
  * Ambos scripts de build usan `--no-emit-project` y derivan el
    `requirements-download.txt` sin markers; el `.sh` pide
    `manylinux_2_28`; ambos fallan si hay más de un `cmcourier-*.whl`.
  * `install.sh` renderizado y EJECUTADO: con `PYTHON` apuntando a un
    intérprete falso que reporta otra versión → exit 1 y el mensaje
    esperado; con la versión correcta y un `wheels/` vacío → pasa la
    comprobación y falla recién en el paso de pip (se verifica por el
    texto del paso alcanzado); con un `.venv` de otra versión → exit 1
    pidiendo borrarlo.
  * `install.bat`: sólo texto (no hay `cmd.exe` acá) — se documenta esa
    limitación en el test.
* `tests/integration/installer/test_build_bundle_live.py`, gateado por
  `CMCOURIER_INSTALLER_LIVE=1` (necesita red y `pwsh`):
  * `bash installer/build-offline-bundle.sh --output-dir <tmp>` produce
    el `.zip`; se extrae, se corre `install.sh` en un venv limpio y
    `cmcourier --version` imprime la versión del `pyproject`.
  * `pwsh -File installer/build-offline-bundle.ps1 -PythonVersion 3.11
    -OutputDir <tmp>` produce `cmcourier-offline-<v>-py3.11-win_amd64.zip`
    con exactamente un `cmcourier-*.whl`, `colorama-*.whl`,
    `pyodbc-*-cp311-*-win_amd64.whl`, y un `install.bat` con la
    comprobación de versión.

### REQ-007 — Documentación y release

* `docs/how-to/build-offline-installer.md`: prerequisitos completos por
  plataforma (REQ-004), la variable `PYTHON`, cómo armar el bundle
  Windows desde Linux con `pwsh`, el requisito de glibc, y una sección
  "Verificar el bundle antes de llevarlo" (abrir el zip y contar
  wheels; `pip install --dry-run --no-index --find-links wheels
  cmcourier` en un venv de la versión destino).
* `docs/tutorials/00-getting-started.md` y `validation-checklist.md`
  si mencionan el flujo offline: consistencia con lo anterior.
* CHANGELOG `0.113.1` (Fixed) y bump.

## Criterios de aceptación

1. `bash installer/build-offline-bundle.sh` en esta máquina produce el
   `.zip` sin error, con exactamente un wheel de `cmcourier` y sin
   ningún archivo del checkout en `wheels/`.
2. `pwsh -File installer/build-offline-bundle.ps1 -PythonVersion 3.11`
   en esta máquina (Linux) produce el bundle `win_amd64` con `colorama`
   y los 49 wheels de dependencias para cp311.
3. `install.sh` con `PYTHON` de otra versión sale con 1 y el mensaje
   claro; con la versión correcta instala offline y `cmcourier
   --version` responde.
4. `INSTALL.txt` de ambos bundles lista los prerequisitos de REQ-004.
5. Tests de REQ-006 verdes; ruff/mypy limpios.

## Riesgos / notas

* `install.bat` no se puede ejecutar acá: queda verificado por texto y
  por la simetría con `install.sh` (misma lógica, misma plantilla de
  mensajes). Hay que probarlo UNA vez en un Windows real antes de
  confiar ciegamente — se deja dicho en el how-to.
* Bajar todos los wheels sin markers agrega `colorama` (~20 KB) al
  bundle Linux. Aceptado.
* No se toca `uv.lock`: los pins actuales tienen wheels para cp311 y
  cp312 en ambas plataformas. Si el servidor tuviera Python 3.10 o
  3.13+, hay que volver a verificar (`requires-python >= 3.11`; 3.13
  no fue probado).
