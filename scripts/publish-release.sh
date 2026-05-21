#!/usr/bin/env bash
#
# publish-release.sh — publica un release de CMCourier en GitHub.
#
# Arma el export bundle (scripts/export-bundle.sh) y lo adjunta a un
# GitHub Release nuevo, taggeado con la versión de pyproject.toml. El
# ZIP NO se commitea al repo — vive solo en el Release. Así los binarios
# no ensucian el historial de git.
#
# Requiere: gh (GitHub CLI) autenticado — https://cli.github.com/
#
# Uso:
#   bash scripts/publish-release.sh             # publica el release
#   bash scripts/publish-release.sh --draft     # crea el release como borrador
#
set -euo pipefail

DRAFT_FLAG=""
case "${1:-}" in
  --draft) DRAFT_FLAG="--draft" ;;
  "")      ;;
  *) echo "ERROR: argumento desconocido: $1" >&2; exit 2 ;;
esac

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

command -v gh >/dev/null || {
  echo "ERROR: gh (GitHub CLI) no está instalado — https://cli.github.com/" >&2
  exit 1
}
gh auth status >/dev/null 2>&1 || {
  echo "ERROR: gh no está autenticado. Corré 'gh auth login'." >&2
  exit 1
}

VERSION="$(grep -E '^version[[:space:]]*=' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
[[ -n "${VERSION}" ]] || { echo "ERROR: no se pudo leer la versión de pyproject.toml" >&2; exit 1; }
TAG="v${VERSION}"

if gh release view "${TAG}" >/dev/null 2>&1; then
  echo "ERROR: el release ${TAG} ya existe. Bumpeá la versión o borralo:" >&2
  echo "       gh release delete ${TAG}" >&2
  exit 1
fi

# 1. Armar el export bundle (deja el ZIP en releases/, gitignoreado).
echo "==> Armando el export bundle…"
bash scripts/export-bundle.sh
ZIP="releases/cmcourier-export-${VERSION}.zip"
[[ -f "${ZIP}" ]] || { echo "ERROR: no se generó ${ZIP}" >&2; exit 1; }

# 2. Crear el GitHub Release y adjuntar el ZIP.
echo "==> Publicando el GitHub Release ${TAG}…"
gh release create "${TAG}" "${ZIP}" \
  ${DRAFT_FLAG} \
  --title "CMCourier ${VERSION}" \
  --notes "Export bundle de CMCourier ${VERSION}. Changelog completo en \`CHANGELOG.md\`."

echo "✓ Release ${TAG} publicado con ${ZIP}"
