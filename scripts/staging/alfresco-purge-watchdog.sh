#!/usr/bin/env bash
# Alfresco staging contentstore watchdog.
#
# Polls the alfresco-data Docker volume and deletes blob files from the
# contentstore when total size exceeds THRESHOLD_GB. Designed for staging
# throughput rigs where uploaded content is disposable.
#
# Trade-off: file-level deletion is FAST (~ms) but leaves orphaned rows
# in postgres + dangling refs in the Solr index. Alfresco tolerates this
# silently for write paths; do NOT use against any repo whose content you
# need to read back. To fully reset metadata + index, run:
#   docker compose -f alfresco-compose.yml down -v && docker compose ... up -d
#
# Usage (must run as root — /var/lib/docker is root-owned):
#   sudo bash alfresco-purge-watchdog.sh
#
# Configurable via env vars:
#   VOLUME_NAME    docker volume name           (default: staging_alfresco-data)
#   THRESHOLD_GB   purge trigger in GB          (default: 30)
#   INTERVAL_S     polling interval in seconds  (default: 30)
#   DRY_RUN        1 = log only, don't delete   (default: 0)
#
# Stop:
#   Ctrl-C if foreground, or: sudo pkill -f alfresco-purge-watchdog

set -euo pipefail

VOLUME_NAME="${VOLUME_NAME:-staging_alfresco-data}"
THRESHOLD_GB="${THRESHOLD_GB:-30}"
INTERVAL_S="${INTERVAL_S:-30}"
DRY_RUN="${DRY_RUN:-0}"

VOLUME_ROOT="/var/lib/docker/volumes/${VOLUME_NAME}/_data"
CONTENTSTORE="${VOLUME_ROOT}/contentstore"

log() { printf '[%s] [watchdog] %s\n' "$(date +%H:%M:%S)" "$*"; }

if [ ! -d "$VOLUME_ROOT" ]; then
  log "ERROR: docker volume not found at $VOLUME_ROOT"
  log "       check VOLUME_NAME and that the alfresco stack is up"
  exit 1
fi

threshold_bytes=$((THRESHOLD_GB * 1024 * 1024 * 1024))
log "starting — volume=$VOLUME_NAME threshold=${THRESHOLD_GB}GB interval=${INTERVAL_S}s dry_run=${DRY_RUN}"

while true; do
  if [ -d "$CONTENTSTORE" ]; then
    current_bytes=$(du -sb "$CONTENTSTORE" 2>/dev/null | awk '{print $1}')
  else
    current_bytes=0
  fi
  current_gb=$(awk "BEGIN{printf \"%.2f\", $current_bytes/1024/1024/1024}")

  if [ "$current_bytes" -gt "$threshold_bytes" ]; then
    file_count=$(find "$CONTENTSTORE" -type f 2>/dev/null | wc -l)
    if [ "$DRY_RUN" = "1" ]; then
      log "OVER threshold (${current_gb}GB > ${THRESHOLD_GB}GB) — DRY RUN, would delete $file_count files"
    else
      log "OVER threshold (${current_gb}GB > ${THRESHOLD_GB}GB) — purging $file_count files"
      find "$CONTENTSTORE" -mindepth 1 -type f -delete
      find "$CONTENTSTORE" -mindepth 1 -type d -empty -delete 2>/dev/null || true
      after_bytes=$(du -sb "$CONTENTSTORE" 2>/dev/null | awk '{print $1}')
      after_gb=$(awk "BEGIN{printf \"%.2f\", $after_bytes/1024/1024/1024}")
      log "purged → ${after_gb}GB"
    fi
  else
    log "ok ${current_gb}GB / ${THRESHOLD_GB}GB"
  fi

  sleep "$INTERVAL_S"
done
