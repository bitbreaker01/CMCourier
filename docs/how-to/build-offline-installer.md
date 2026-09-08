# How to: Armar el instalador offline para un servidor air-gapped

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

> Cómo empaquetar CMCourier en un bundle de instalación **autocontenido**
> para un servidor sin acceso a internet (air-gapped) — el caso típico de
> un servidor de migración dentro de la red del banco.

---

## Cuándo necesitás esto

El servidor donde corre la migración **no tiene internet**: no puede hacer
`pip install` contra PyPI. Necesitás llevarle CMCourier + todas sus
dependencias ya descargadas, en un único `.zip`.

El flujo es de dos máquinas:

```
  Máquina de build (CON internet)          Servidor air-gapped (SIN internet)
  ───────────────────────────────          ──────────────────────────────────
  build-offline-bundle.{ps1,sh}    ──zip──►  install.{bat,sh}
  descarga wheels + arma el bundle           instala offline en un .venv
```

---

## Antes de empezar: la versión de Python del servidor

**Todo depende de este dato.** Los wheels de dependencias son específicos
del `major.minor` de Python (`cp311`, `cp312`…): un bundle armado para
3.12 NO instala en un 3.11. Averiguá la versión EXACTA del servidor
antes de armar nada:

```
py -0p                 # Windows: lista todos los Python instalados y sus rutas
python --version       # Windows / Linux
python3 --version      # Linux
```

Versiones verificadas con el `uv.lock` actual: **3.11 y 3.12** (todas las
dependencias tienen wheel para `win_amd64` y `manylinux_2_28`). Con 3.13+
hay que volver a verificar que `pip download` traiga todo; 3.10 o menor
no sirve (`requires-python >= 3.11`).

---

## Los dos scripts

Viven en `installer/` (no en `scripts/` — son un **entregable**, no tooling
interno):

| Script | Corré esto en… | Para un servidor destino… |
|---|---|---|
| `installer/build-offline-bundle.ps1` | Windows con internet, **o Linux/macOS con `pwsh`** | Windows Server x86_64 |
| `installer/build-offline-bundle.sh` | Linux con internet | Linux x86_64 |

Los dos hacen lo mismo: leen `uv.lock`, buildean el wheel del proyecto,
descargan todos los wheels de dependencias para la plataforma y versión
destino, copian config + datos de referencia, generan el instalador desde
`installer/templates/install.{bat,sh}.tmpl` y comprimen todo en un `.zip`.

---

## Prerequisitos en la máquina de build

- **Python** instalado. Idealmente la misma versión `major.minor` que el
  servidor destino; si no, pasá `-PythonVersion` / `--python-version` y
  el script cross-descarga (ver abajo).
- **`uv`** — el package manager. Instalar desde
  <https://docs.astral.sh/uv/>.
- **Internet** — para que `pip download` traiga los wheels.
- Linux: **`zip`** (`apt install zip` / `dnf install zip`).
- Para armar el bundle **Windows desde Linux**: `pwsh` (PowerShell 7).

---

## Prerequisitos en el servidor destino

El bundle trae CMCourier y sus dependencias Python. **No trae** Python ni
los drivers ODBC — eso lo tiene que tener el servidor antes de instalar.

### Windows Server

1. **Python `X.Y` x86_64 de python.org** — NO el alias de la Microsoft
   Store. Dejá tildado el launcher `py` (viene por default): `install.bat`
   lo usa para elegir la versión correcta aunque haya varios Python.
2. **Microsoft Visual C++ Redistributable 2015-2022 x64** (casi siempre
   ya está; lo necesitan `pyodbc`, `numpy`, `pillow`, `lxml`).
3. **IBM i Access ODBC driver** — si hay fuente AS400/RVI.
4. **Microsoft ODBC Driver 18 for SQL Server** — si hay fuente MSSQL
   (`connections.<alias>.kind: mssql`).
5. **System DSN de 64-bit** ("ODBC Data Sources (64-bit)") para cada
   fuente que use DSN.

### Linux

1. **Python `X.Y` x86_64** con el módulo `venv` (en Debian/Ubuntu es el
   paquete `python3.X-venv`).
2. **glibc ≥ 2.28** — RHEL/Rocky/Alma 8+, Ubuntu 20.04+, Debian 10+.
   Los wheels pineados (`numpy 2.4.4` entre otros) son `manylinux_2_28`;
   en RHEL 7 / CentOS 7 **no instalan**.
3. **unixODBC** (`libodbc.so.2`) — el wheel de `pyodbc` no lo trae.
4. Los mismos drivers ODBC según fuente (IBM i Access / msodbcsql18) y sus
   DSN en `odbc.ini`.

---

## Armar el bundle

### Destino Windows

```powershell
# En Windows (PowerShell 5.1 o 7):
.\installer\build-offline-bundle.ps1
.\installer\build-offline-bundle.ps1 -PythonVersion 3.11
.\installer\build-offline-bundle.ps1 -PythonVersion 3.12 -OutputDir C:\releases
```

```bash
# En Linux/macOS con pwsh, para un servidor Windows:
pwsh -File installer/build-offline-bundle.ps1 -PythonVersion 3.11 -OutputDir dist-offline
```

El script cross-descarga siempre para `win_amd64` con
`pip download --platform win_amd64 --python-version X.Y --implementation cp
--abi cpXY --only-binary=:all:`, así que el host de build puede ser
cualquiera. Emite un warning si `X.Y` no coincide con el Python del host —
es informativo, no bloquea.

### Destino Linux

```bash
bash installer/build-offline-bundle.sh
bash installer/build-offline-bundle.sh --python-version 3.11
bash installer/build-offline-bundle.sh --output-dir /releases --skip-build
```

Si `--python-version` difiere del host, cross-descarga pidiendo
`manylinux_2_28_x86_64` / `manylinux_2_17_x86_64` / `manylinux2014_x86_64`
(por eso el requisito de glibc de arriba).

| Flag | Default | Qué hace |
|---|---|---|
| `-PythonVersion` / `--python-version` | la del host de build | Versión Python del servidor destino. |
| `-OutputDir` / `--output-dir` | `dist-offline` | Dónde queda el `.zip`. |
| `-SkipBuild` / `--skip-build` | (off) | Reusa el wheel del proyecto si ya existe en `dist/`. |

La salida es:
`dist-offline/cmcourier-offline-<version>-py<X.Y>-<plataforma>.zip`

### Qué hace por dentro (y por qué)

- `uv export --no-emit-project` → `requirements.txt` con los pins del
  lock, **sin** el propio proyecto (el proyecto viaja como wheel de
  `uv build`; con `-e .` `pip download` intentaba empaquetar el checkout
  entero).
- De ahí deriva `requirements-download.txt` con los markers de plataforma
  **eliminados**: `pip download` evalúa los markers contra la máquina de
  BUILD, no contra el destino, y sin esto `colorama` (`sys_platform ==
  'win32'`) no viajaba si el bundle Windows se armaba desde Linux. En el
  servidor, `pip install` evalúa los markers de verdad y usa sólo lo que
  corresponde — un wheel de más es inofensivo, uno de menos rompe la
  instalación.
- Falla si en `wheels/` queda más (o menos) de un `cmcourier-*.whl`.

---

## Qué hay adentro del bundle

```
cmcourier-offline-<version>-py<X.Y>-<plataforma>/
├── install.bat / install.sh      # el instalador offline (desde installer/templates/)
├── INSTALL.txt                   # instrucciones y prerequisitos para el operador
├── requirements.txt              # dependencias pineadas (desde uv.lock, con markers)
├── requirements-download.txt     # lo que se le pidió a pip download (sin markers)
├── wheels/                       # TODOS los .whl — cmcourier + dependencias + pip
├── config/
│   ├── config-prod.yaml.template # config para copiar y editar
│   └── config-reference.yaml     # referencia anotada de TODA la config
├── reference-data/               # datos de referencia
└── README.md
```

---

## Verificar el bundle antes de llevarlo

Cinco minutos acá te ahorran un viaje al servidor.

1. Abrí el `.zip` y contá: tiene que haber **un solo** `cmcourier-*.whl`,
   un `colorama-*.whl`, y los wheels compilados (`pyodbc`, `numpy`,
   `pillow`, `lxml`, `pydantic_core`…) con el tag de la versión y
   plataforma destino — `cp311-cp311-win_amd64` para Windows 3.11, por
   ejemplo. Un wheel con `cp312` en un bundle `py3.11` es un bundle roto.
2. Si tenés a mano un Python de la versión destino (aunque sea en otra
   plataforma), un dry-run offline contra el `wheels/` extraído te dice si
   pip resuelve TODO sin red:

   ```bash
   python3.11 -m venv /tmp/chk && /tmp/chk/bin/pip install --dry-run \
       --no-index --find-links wheels cmcourier
   ```

   Para un bundle Windows el dry-run desde Linux va a rechazar los wheels
   `win_amd64` (es esperado); lo que sí podés verificar desde acá es el
   punto 1.
3. El test de integración hace todo esto (arma los dos bundles, instala el
   de Linux en un venv limpio y revisa el contenido del de Windows):

   ```bash
   CMCOURIER_INSTALLER_LIVE=1 uv run pytest tests/integration/installer -q
   ```

---

## Instalar en el servidor air-gapped

1. Transferí el `.zip` al servidor (SFTP, share, USB — lo que tengas).
2. Extraelo.
3. Corré el instalador desde la carpeta extraída:
   - Windows: `install.bat` (desde CMD o PowerShell)
   - Linux: `bash install.sh`
4. El instalador **elige y comprueba el intérprete** antes de tocar nada:
   1. la variable de entorno `PYTHON`, si está definida;
   2. `py -X.Y` (Windows) / `pythonX.Y` (Linux);
   3. `python` / `python3` a secas.

   Si el elegido no es Python `X.Y` (la versión del bundle), sale con
   código 1 y te dice qué encontró, qué esperaba y cómo apuntarlo:

   ```bat
   set PYTHON=C:\Python311\python.exe
   install.bat
   ```

   ```bash
   PYTHON=/usr/bin/python3.11 bash install.sh
   ```

   Si ya existe un `.venv` de OTRA versión, también sale con 1 y te pide
   borrarlo (`rmdir /s /q .venv` / `rm -rf .venv`) — no lo borra solo.
5. Después crea el `.venv`, actualiza pip desde `wheels/` e instala
   `cmcourier` **offline** (`pip install --no-index --find-links wheels`).
6. Verifica solo: corre `cmcourier --version` al final.

Después: copiá `config/config-prod.yaml.template` a `config-prod.yaml`,
editá las rutas y credenciales (la referencia completa está en
`config/config-reference.yaml`), y ya podés correr el pipeline. El
`INSTALL.txt` del bundle tiene el detalle.

> **Nota honesta sobre `install.bat`**: el `install.sh` se ejecuta en los
> tests (intérpretes falsos + instalación real en venv limpio). El
> `install.bat` sólo se puede verificar por texto en el runner Linux —
> misma lógica y mismos mensajes que el `.sh`, pero probalo UNA vez en un
> Windows real antes de confiar ciegamente.

---

## Actualizar una instalación existente

Re-corré `build-offline-bundle` con la versión nueva, llevá el `.zip` nuevo
al servidor, extraelo y corré el instalador de nuevo. El `.venv` existente
se reusa si es de la misma versión de Python; pip solo actualiza los
paquetes que cambiaron desde la nueva carpeta `wheels/`.

---

## Cross-references

* Spec: `specs/142-offline-installer-hardening/spec.md` (por qué cada
  decisión) y `specs/101-offline-installer/spec.md` (el flujo original).
* Referencia de configuración: [`reference/config-reference.yaml`](../reference/config-reference.yaml).
* Driver ODBC AS400: ver [`how-to/as400-sync.md`](as400-sync.md).
* Scripts: `installer/build-offline-bundle.ps1` · `installer/build-offline-bundle.sh` · `installer/templates/`.
