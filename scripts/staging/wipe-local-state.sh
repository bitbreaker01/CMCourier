#!/usr/bin/env bash
#
# Wipe local CMCourier staging state — tracking SQLite DB + observability
# logs. Run BEFORE re-executing a deterministic smoke pipeline so the
# idempotency layer is fresh.
#
# Default targets the paths used by ``sample/config-staging.yaml`` (the
# local-staging-simulation runbook). Override via env vars when you
# point at a different config.
#
# Use case: between deterministic smoke runs you need to reset
# ``migration_log`` (the idempotency store) so the pipeline doesn't
# short-circuit every doc as "already uploaded". Pair this with
# ``wipe-alfresco-docs.sh`` to wipe both sides of the system.
#
# Usage:
#   bash scripts/staging/wipe-local-state.sh
#
# Environment overrides:
#   TRACKING_DB   default sample/staging-tracking.db
#   LOG_DIR       default sample/logs
#   STAGING_TMP   default sample/staging_tmp  (assembly leftovers, S4)
#
# Use ``--dry-run`` to print what would be removed without touching the
# filesystem.

set -euo pipefail

TRACKING_DB="${TRACKING_DB:-sample/*.db}"
LOG_DIR="${LOG_DIR:-sample/logs}"
STAGING_TMP="${STAGING_TMP:-sample/staging_tmp}"

DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# *//'
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

remove() {
  local path="$1"
  if [[ ! -e "${path}" ]] && [[ ! -L "${path}" ]]; then
    printf "  · %-40s (absent — nothing to do)\n" "${path}"
    return
  fi
  if [[ "${DRY_RUN}" == "true" ]]; then
    printf "  ⊘ %-40s (--dry-run, kept)\n" "${path}"
    return
  fi
  rm -rf "${path}"
  printf "  ✓ %-40s removed\n" "${path}"
}

echo "→ Wiping local CMCourier staging state${DRY_RUN:+ (DRY RUN)}"
echo

# 1. SQLite tracking DB + WAL sidecars. These three travel together —
#    leaving any one behind corrupts WAL recovery on next open.
remove "${TRACKING_DB}"
remove "${TRACKING_DB}-wal"
remove "${TRACKING_DB}-shm"

# 2. Observability logs (network, metrics, system jsonl rotations).
if [[ -d "${LOG_DIR}" ]] && [[ "${DRY_RUN}" != "true" ]]; then
  shopt -s nullglob
  for f in "${LOG_DIR}"/*; do
    rm -rf "${f}"
    printf "  ✓ %-40s removed\n" "${f}"
  done
  shopt -u nullglob
  # If the directory is now empty, leave it — the next pipeline run
  # opens it and won't have to mkdir.
elif [[ "${DRY_RUN}" == "true" ]] && [[ -d "${LOG_DIR}" ]]; then
  shopt -s nullglob
  for f in "${LOG_DIR}"/*; do
    printf "  ⊘ %-40s (--dry-run, kept)\n" "${f}"
  done
  shopt -u nullglob
else
  printf "  · %-40s (absent or empty)\n" "${LOG_DIR}"
fi

# 3. Stale S4 temp PDFs. These should be cleaned up by S7 in a healthy
#    run, but a crashed pipeline can leave assembled PDFs behind. Safe to
#    drop between smokes.
remove "${STAGING_TMP}"

echo
if [[ "${DRY_RUN}" == "true" ]]; then
  echo "✓ Dry-run complete. Re-run without --dry-run to apply."
else
  echo "✓ Local state wiped. Next pipeline run starts from a clean tracking DB."
fi
