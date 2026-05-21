#!/usr/bin/env bash
#
# build-offline-bundle.sh — versión Linux de build-offline-bundle.ps1.
#
# Arma un bundle de instalación offline autocontenido de CMCourier para un
# servidor Linux air-gapped que ya tiene Python instalado.
#
# Corré ESTO en una máquina Linux CON internet. La salida es un único .zip
# que transferís al servidor air-gapped (SFTP / share). El servidor instala
# offline corriendo el install.sh incluido.
#
# IMPORTANTE: por defecto descarga wheels para la plataforma del HOST de
# build. La máquina de build debe coincidir con el servidor destino en
# arquitectura, familia de glibc y versión de Python. Para un destino
# distinto, ver --python-version (emite un warning) o pre-armá los wheels
# a mano.
#
# Uso:
#   bash installer/build-offline-bundle.sh
#   bash installer/build-offline-bundle.sh --python-version 3.11
#   bash installer/build-offline-bundle.sh --output-dir /releases --skip-build
#
set -euo pipefail

PYTHON_VERSION=""
OUTPUT_DIR="dist-offline"
SKIP_BUILD=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python-version) PYTHON_VERSION="$2"; shift 2 ;;
    --output-dir)     OUTPUT_DIR="$2";     shift 2 ;;
    --skip-build)     SKIP_BUILD=true;     shift ;;
    -h|--help)        grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "ERROR: argumento desconocido: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$1"; }
warn() { printf '    \033[33m%s\033[0m\n' "$1"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. Ubicar el root del repo y verificar que es un checkout de CMCourier
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
[[ -f pyproject.toml ]] || fail "pyproject.toml no encontrado. Corré desde un checkout de CMCourier."

# ---------------------------------------------------------------------------
# 1. Prerequisitos
# ---------------------------------------------------------------------------
step "Chequeando prerequisitos"
command -v python3 >/dev/null || fail "python3 no está en PATH."
command -v uv >/dev/null || fail "uv no está en PATH. Instalá desde https://docs.astral.sh/uv/"

DETECTED_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[[ -n "$PYTHON_VERSION" ]] || PYTHON_VERSION="$DETECTED_VERSION"
ok "Python (host de build): $DETECTED_VERSION"
ok "Python (destino):       $PYTHON_VERSION"
if [[ "$PYTHON_VERSION" != "$DETECTED_VERSION" ]]; then
  warn "El Python destino ($PYTHON_VERSION) difiere del host ($DETECTED_VERSION)."
  warn "El cross-download en Linux es frágil — algunos paquetes pueden no tener"
  warn "wheels manylinux para el ABI pedido. Idealmente buildeá en un host gemelo."
fi
ok "uv: $(uv --version)"

# ---------------------------------------------------------------------------
# 2. Leer la versión del proyecto desde pyproject.toml
# ---------------------------------------------------------------------------
step "Leyendo la versión del proyecto"
PROJECT_VERSION="$(grep -E '^version[[:space:]]*=' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
[[ -n "$PROJECT_VERSION" ]] || fail "No se pudo parsear la versión de pyproject.toml"
ok "cmcourier version: $PROJECT_VERSION"

# ---------------------------------------------------------------------------
# 3. Preparar el layout del bundle
# ---------------------------------------------------------------------------
BUNDLE_NAME="cmcourier-offline-${PROJECT_VERSION}-py${PYTHON_VERSION}-linux_x86_64"
BUNDLE_DIR="${OUTPUT_DIR}/${BUNDLE_NAME}"
WHEELS_DIR="${BUNDLE_DIR}/wheels"
CONFIG_DIR="${BUNDLE_DIR}/config"

step "Preparando el directorio del bundle: ${BUNDLE_DIR}"
rm -rf "$BUNDLE_DIR"
mkdir -p "$WHEELS_DIR" "$CONFIG_DIR"

# ---------------------------------------------------------------------------
# 4. Exportar los requirements pineados desde uv.lock
# ---------------------------------------------------------------------------
step "Exportando requirements desde uv.lock"
REQ_FILE="${BUNDLE_DIR}/requirements.txt"
uv export --no-hashes --no-dev --format requirements-txt -o "$REQ_FILE" \
  || fail "uv export falló."
ok "Escrito ${REQ_FILE}"

# ---------------------------------------------------------------------------
# 5. Buildear el wheel del proyecto (salvo --skip-build)
# ---------------------------------------------------------------------------
PROJECT_WHEEL="dist/cmcourier-${PROJECT_VERSION}-py3-none-any.whl"
if [[ "$SKIP_BUILD" == "true" && -f "$PROJECT_WHEEL" ]]; then
  step "Salteando el build del wheel (uso el existente ${PROJECT_WHEEL})"
else
  step "Buildeando el wheel del proyecto"
  rm -rf dist
  uv build --wheel || fail "uv build falló."
fi
[[ -f "$PROJECT_WHEEL" ]] || fail "Wheel esperado no encontrado: ${PROJECT_WHEEL}"
cp "$PROJECT_WHEEL" "$WHEELS_DIR/"
ok "Bundle: ${PROJECT_WHEEL}"

# ---------------------------------------------------------------------------
# 6. Descargar los wheels de dependencias (plataforma del host de build)
# ---------------------------------------------------------------------------
step "Descargando wheels de dependencias (Linux x86_64 / py${PYTHON_VERSION})"
PLATFORM_ARGS=()
if [[ "$PYTHON_VERSION" != "$DETECTED_VERSION" ]]; then
  # Cross-target: pip exige --platform/--abi explícitos + only-binary.
  abi="cp${PYTHON_VERSION//./}"
  PLATFORM_ARGS=(
    --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64
    --python-version "$PYTHON_VERSION" --implementation cp --abi "$abi"
    --only-binary=:all:
  )
fi
python3 -m pip download --dest "$WHEELS_DIR" "${PLATFORM_ARGS[@]}" -r "$REQ_FILE" \
  || fail $'pip download falló. Causas comunes:\n  - Una dependencia solo trae sdist (sin wheel).\n  - Host de build no coincide con el destino (arch / glibc / Python).\n  - Problema de red / proxy en esta máquina.'

# Stagear pip/setuptools/wheel para que el instalador offline pueda actualizar pip.
python3 -m pip download --dest "$WHEELS_DIR" "${PLATFORM_ARGS[@]}" \
  pip setuptools wheel >/dev/null 2>&1 || warn "No se pudieron stagear pip/setuptools/wheel."

WHEEL_COUNT="$(find "$WHEELS_DIR" -name '*.whl' | wc -l | tr -d ' ')"
ok "Wheels stageados: ${WHEEL_COUNT}"

# ---------------------------------------------------------------------------
# 7. Copiar config y datos de referencia
# ---------------------------------------------------------------------------
step "Empaquetando config y datos de referencia"
for f in sample/config-staging.yaml sample/clients.csv \
         sample/MapeoRVI_CM.csv sample/MetadatosCM.csv; do
  if [[ -f "$f" ]]; then
    cp "$f" "$CONFIG_DIR/"
    ok "Bundle: ${f}"
  else
    warn "Salteado (no existe): ${f}"
  fi
done

# Siempre incluir el config de referencia anotado — así el operador tiene la
# superficie configurable completa aunque sample/ no exista (es gitignored).
if [[ -f docs/reference/config-reference.yaml ]]; then
  cp docs/reference/config-reference.yaml "$CONFIG_DIR/"
  ok "Bundle: docs/reference/config-reference.yaml"
fi

# Renombrar el config principal a un nombre production-friendly.
if [[ -f "${CONFIG_DIR}/config-staging.yaml" ]]; then
  mv "${CONFIG_DIR}/config-staging.yaml" "${CONFIG_DIR}/config-prod.yaml.template"
fi

if [[ -d reference-data ]]; then
  cp -r reference-data "${BUNDLE_DIR}/reference-data"
  ok "Bundle: reference-data/"
fi
[[ -f README.md ]] && cp README.md "$BUNDLE_DIR/"

# ---------------------------------------------------------------------------
# 8. Generar install.sh
# ---------------------------------------------------------------------------
step "Generando install.sh"
cat > "${BUNDLE_DIR}/install.sh" <<'INSTALLER'
#!/usr/bin/env bash
#
# CMCourier offline installer — generado por build-offline-bundle.sh
#
set -euo pipefail
cd "$(dirname "$0")"

echo "=== CMCourier offline installer ==="
echo "Install dir: $(pwd)"
echo

command -v python3 >/dev/null || { echo "ERROR: python3 no está en PATH."; exit 1; }

echo "[1/4] Creando virtualenv (.venv)..."
if [[ -d .venv ]]; then
  echo "      .venv ya existe, reuso."
else
  python3 -m venv .venv || { echo "ERROR: no se pudo crear .venv."; exit 1; }
fi

echo "[2/4] Actualizando pip desde el wheelhouse local..."
.venv/bin/python -m pip install --no-index --find-links wheels --upgrade \
  pip setuptools wheel || { echo "ERROR: upgrade de pip falló."; exit 1; }

echo "[3/4] Instalando cmcourier y dependencias (offline)..."
.venv/bin/python -m pip install --no-index --find-links wheels cmcourier \
  || { echo "ERROR: instalación de cmcourier falló."; exit 1; }

echo "[4/4] Verificando..."
.venv/bin/cmcourier --version || { echo "ERROR: cmcourier no corre."; exit 1; }

echo
echo "=== Instalado correctamente ==="
echo
echo "Próximos pasos:"
echo "  1. Instalá el driver ODBC de IBM i Access si no está."
echo "  2. Configurá un DSN para la fuente AS400 / RVI."
echo "  3. Editá config/config-prod.yaml (copialo desde el .template)."
echo "  4. Corré:  .venv/bin/cmcourier --config config/config-prod.yaml [comando]"
INSTALLER
chmod +x "${BUNDLE_DIR}/install.sh"

# ---------------------------------------------------------------------------
# 9. Generar INSTALL.txt con instrucciones para el operador
# ---------------------------------------------------------------------------
step "Generando INSTALL.txt"
cat > "${BUNDLE_DIR}/INSTALL.txt" <<TXT
CMCourier ${PROJECT_VERSION} — Instalación Offline (Linux)
==========================================================

Destino: servidor Linux x86_64 con Python ${PYTHON_VERSION} ya instalado.

Prerequisitos en el servidor
----------------------------
1. Python ${PYTHON_VERSION} x86_64 en PATH.
2. El módulo venv de Python (en Debian/Ubuntu: paquete python3-venv).
3. Driver ODBC de IBM i Access (o equivalente para la fuente RVI).
4. Un DSN configurado (unixODBC: /etc/odbc.ini).

Instalación
-----------
1. Copiá este bundle al servidor (ya hecho si estás leyendo esto ahí).
2. Abrí una terminal en este directorio.
3. Corré:
       bash install.sh
   El instalador crea un .venv local e instala cmcourier offline.

Configuración
-------------
1. Copiá config/config-prod.yaml.template a config/config-prod.yaml.
2. Editá connection strings, paths y credenciales. La referencia completa
   de opciones está en config/config-reference.yaml.
3. Ajustá clients.csv / MapeoRVI_CM.csv / MetadatosCM.csv si hace falta.

Ejecución
---------
   .venv/bin/cmcourier --config config/config-prod.yaml --help

Actualización
-------------
Re-corré install.sh con un bundle nuevo. El .venv existente se reusa; pip
actualiza los paquetes cambiados desde la nueva carpeta wheels/.
TXT

# ---------------------------------------------------------------------------
# 10. Archivar el bundle como .zip
# ---------------------------------------------------------------------------
step "Comprimiendo el bundle"
ZIP_PATH="${OUTPUT_DIR}/${BUNDLE_NAME}.zip"
rm -f "$ZIP_PATH"
( cd "$OUTPUT_DIR" && zip -qr "${BUNDLE_NAME}.zip" "$BUNDLE_NAME" )

ZIP_SIZE="$(du -h "$ZIP_PATH" | cut -f1)"

echo
echo "============================================================"
echo " Bundle listo"
echo "============================================================"
echo " Path:    ${ZIP_PATH}"
echo " Tamaño:  ${ZIP_SIZE}"
echo " Wheels:  ${WHEEL_COUNT}"
echo " Destino: Linux x86_64, Python ${PYTHON_VERSION}"
echo
echo " Transferí este .zip al servidor air-gapped, extraelo,"
echo " y corré:  bash install.sh"
echo "============================================================"
