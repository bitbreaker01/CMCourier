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
command -v zip >/dev/null || fail "zip no está en PATH. Instalalo (apt/yum install zip)."

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
uv export --no-emit-project --no-hashes --no-dev --format requirements-txt -o "$REQ_FILE" \
  || fail "uv export falló."
ok "Escrito ${REQ_FILE}"

# requirements-download.txt: mismas líneas, sin los markers (todo lo que
# sigue a " ;"). pip download los ignora y baja TODAS las dependencias del
# lock sin importar la plataforma del host de build — los markers los
# evalúa pip install de verdad, en el servidor destino.
REQ_DOWNLOAD_FILE="${BUNDLE_DIR}/requirements-download.txt"
: > "$REQ_DOWNLOAD_FILE"
while IFS= read -r line; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  echo "${line%% ;*}" >> "$REQ_DOWNLOAD_FILE"
done < "$REQ_FILE"
ok "Escrito ${REQ_DOWNLOAD_FILE}"

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
  # Cross-target: pip exige --platform/--abi explícitos + only-binary. Se
  # piden los tres tags manylinux (numpy 2.4.4 sólo publica _2_28) y
  # --abi none además de cp3XY para los wheels puros py3-none-any.
  abi="cp${PYTHON_VERSION//./}"
  PLATFORM_ARGS=(
    --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64
    --python-version "$PYTHON_VERSION" --implementation cp --abi "$abi" --abi none
    --only-binary=:all:
  )
fi
python3 -m pip download --dest "$WHEELS_DIR" "${PLATFORM_ARGS[@]}" -r "$REQ_DOWNLOAD_FILE" \
  || fail $'pip download falló. Causas comunes:\n  - Una dependencia solo trae sdist (sin wheel).\n  - Host de build no coincide con el destino (arch / glibc / Python).\n  - Problema de red / proxy en esta máquina.'

# Stagear pip/setuptools/wheel para que el instalador offline pueda actualizar pip.
python3 -m pip download --dest "$WHEELS_DIR" "${PLATFORM_ARGS[@]}" \
  pip setuptools wheel >/dev/null 2>&1 || warn "No se pudieron stagear pip/setuptools/wheel."

WHEEL_COUNT="$(find "$WHEELS_DIR" -name '*.whl' | wc -l | tr -d ' ')"
ok "Wheels stageados: ${WHEEL_COUNT}"

CMCOURIER_WHEEL_COUNT="$(find "$WHEELS_DIR" -maxdepth 1 -name 'cmcourier-*.whl' | wc -l | tr -d ' ')"
[[ "$CMCOURIER_WHEEL_COUNT" -eq 1 ]] \
  || fail "Se esperaba exactamente un cmcourier-*.whl en ${WHEELS_DIR}, se encontraron ${CMCOURIER_WHEEL_COUNT}."

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
# 8. Generar install.sh desde installer/templates/install.sh.tmpl
# ---------------------------------------------------------------------------
step "Generando install.sh desde la plantilla"
render_template() {
  local tmpl_file="$1" out_file="$2" content
  content="$(<"$tmpl_file")"
  content="${content//@@PYTHON_VERSION@@/$PYTHON_VERSION}"
  content="${content//@@PROJECT_VERSION@@/$PROJECT_VERSION}"
  content="${content//@@PYTHON_VERSION_NODOT@@/${PYTHON_VERSION//./}}"
  printf '%s\n' "$content" > "$out_file"
}
render_template "${REPO_ROOT}/installer/templates/install.sh.tmpl" "${BUNDLE_DIR}/install.sh"
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
1. Python ${PYTHON_VERSION} x86_64 con el módulo venv (Debian/Ubuntu: paquete
   python3-venv). Si no está en PATH como "python${PYTHON_VERSION}" ni como
   "python3", apuntalo con la variable PYTHON antes de correr install.sh:
       PYTHON=/ruta/al/interprete bash install.sh
2. glibc >= 2.28 (RHEL/Rocky 8+, Ubuntu 20.04+, Debian 10+). Los wheels del
   bundle no instalan en distros más viejas.
3. unixODBC (libodbc.so.2) instalado en el sistema — pyodbc lo necesita, no
   viene en el wheel.
4. Driver ODBC de IBM i Access (o equivalente) si hay una fuente AS400/RVI.
5. Microsoft ODBC Driver 18 for SQL Server si hay una fuente MSSQL
   (connections.<alias>.kind: mssql en la config).
6. Un DSN configurado (unixODBC: /etc/odbc.ini) para cada fuente ODBC que
   lo use.

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
