#!/usr/bin/env bash
#
# export-bundle.sh — empaqueta el repo en un ZIP para compartir con
# alguien externo, excluyendo lo listado en `.exportignore` (carpetas
# internas del equipo que no van al destinatario).
#
# El repo NO se modifica: el script solo lee y empaqueta.
#
# Uso:
#   bash scripts/export-bundle.sh
#
# Salida: cmcourier-export-<timestamp>.zip en el root del repo.
#
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

IGNORE_FILE=".exportignore"
if [[ ! -f "${IGNORE_FILE}" ]]; then
  echo "error: falta ${IGNORE_FILE} en el root del repo" >&2
  exit 1
fi

OUT="cmcourier-export-$(date +%Y%m%d-%H%M%S).zip"

# Patrones de exclusión: líneas no vacías y no comentadas de .exportignore.
mapfile -t patterns < <(grep -vE '^[[:space:]]*(#|$)' "${IGNORE_FILE}" || true)

# `git ls-files` = solo archivos trackeados — sin __pycache__, sin
# nada gitignoreado (logs/, sample/, .venv, *.zip). Filtramos los que
# caen bajo algún patrón de .exportignore (match por prefijo de path).
included=()
excluded=0
while IFS= read -r f; do
  skip=false
  for p in "${patterns[@]}"; do
    case "${f}" in
      "${p}"*)
        skip=true
        break
        ;;
    esac
  done
  if [[ "${skip}" == "true" ]]; then
    excluded=$((excluded + 1))
  else
    included+=("${f}")
  fi
done < <(git ls-files)

if [[ ${#included[@]} -eq 0 ]]; then
  echo "error: no quedó ningún archivo para exportar" >&2
  exit 1
fi

rm -f "${OUT}"
printf '%s\n' "${included[@]}" | zip -q "${OUT}" -@

echo "✓ Export creado: ${OUT}"
echo "  ${#included[@]} archivos incluidos · ${excluded} excluidos por ${IGNORE_FILE}"
