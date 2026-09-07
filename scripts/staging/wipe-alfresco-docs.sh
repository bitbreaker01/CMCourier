# Wipe all documents under /cmcourier-staging/<IDRVI_FOLDER> on the
# remote Alfresco staging. Idempotent — empty folders return 0 deletes.
#
# Removes ONLY the document content. Folders themselves stay so the
# doctor cm-targets pre-flight (038) keeps passing without needing to
# re-pre-create them. If you also want to drop the folders, do it via
# the Alfresco Share UI or extend this script — see the "Drop folders
# too" block at the bottom (commented out).
#
# Use case: between deterministic smoke runs of the staging dataset.
# Because ``cmcourier mock rvabrep`` is seed-deterministic, re-running
# the pipeline produces docs with the SAME ``cmis:name`` values; Alfresco
# rejects those re-uploads with HTTP 409 ``contentAlreadyExists`` (it
# enforces unique names per folder). This script gets Alfresco back to a
# virgin state so the next ``pipeline run`` lands cleanly.
#
# Usage (from any directory):
#   bash scripts/staging/wipe-alfresco-docs.sh
#
# Environment overrides:
#   ALFRESCO_HOST    default testserver  (Tailscale name)
#   ALFRESCO_PORT    default 8080
#   ALFRESCO_USER    default admin
#   ALFRESCO_PASS    default admin
#   STAGING_PARENT   default cmcourier-staging
#
# Exit codes:
#   0 — wipe completed (deletes may be 0 if everything was already empty)
#   1 — couldn't reach Alfresco

set -euo pipefail

ALFRESCO_HOST="${ALFRESCO_HOST:-testserver}"
ALFRESCO_PORT="${ALFRESCO_PORT:-8080}"
ALFRESCO_USER="${ALFRESCO_USER:-admin}"
ALFRESCO_PASS="${ALFRESCO_PASS:-admin}"
STAGING_PARENT="${STAGING_PARENT:-cmcourier-staging}"

BASE="http://${ALFRESCO_HOST}:${ALFRESCO_PORT}/alfresco/api/-default-/public/cmis/versions/1.1/browser"
AUTH="${ALFRESCO_USER}:${ALFRESCO_PASS}"

# Sanity probe — fail loud if Alfresco isn't reachable.

echo "→ Probing ${BASE} ..."
http_code=$(curl -sS -o /dev/null -w "%{http_code}" -u "${AUTH}" \
  --connect-timeout 5 \
  "${BASE}?cmisselector=repositoryInfo" 2>/dev/null || echo "000")
if [[ "${http_code}" != "200" ]]; then
  echo "✗ Alfresco unreachable at ${BASE} (HTTP ${http_code}). Check ALFRESCO_HOST / Tailscale / container." >&2
  exit 1
fi
echo "  reachable (HTTP 200)."

echo "→ Listando carpetas bajo /${STAGING_PARENT} ..."

# 1. Recolección de IDs (guardamos en archivo temporal)
tmp_ids=$(mktemp)
echo "→ Escaneando archivos para borrar..."

folders=$(curl -sS -u "${AUTH}" "${BASE}/root/${STAGING_PARENT}?cmisselector=children" | \
python3 -c "import sys, json; d=json.load(sys.stdin); [print(o['object']['properties']['cmis:name']['value']) for o in d.get('objects', []) if o['object']['properties']['cmis:baseTypeId']['value'] == 'cmis:folder']")

for folder in $folders; do
    folder_enc=$(python3 -c "import urllib.parse; print(urllib.parse.quote('''$folder'''))")
    curl -sS -u "${AUTH}" "${BASE}/root/${STAGING_PARENT}/${folder_enc}?cmisselector=children" | \
    python3 -c "import sys, json; d=json.load(sys.stdin); [print(o['object']['properties']['cmis:objectId']['value']) for o in d.get('objects', [])]" >> "$tmp_ids"
done

# 2. Configuración del borrado
total_docs=$(grep -c . "$tmp_ids" || echo 0)
if [ "$total_docs" -eq 0 ]; then
    echo "✓ Nada que borrar."
    rm "$tmp_ids"
    exit 0
fi

echo "→ Se encontraron $total_docs documentos."
echo "→ Iniciando borrado (Paralelismo: 10 hilos)..."

current=0
max_jobs=10  # Número de borrados simultáneos

# Función de borrado individual
do_delete() {
    local oid=$1
    curl -sS -u "${AUTH}" -o /dev/null -X POST -F "cmisaction=delete" -F "objectId=${oid}" "${BASE}/root"
}

# 3. Bucle con "barra" de progreso
while IFS= read -r id; do
    [[ -z "$id" ]] && continue

    # Lanzar borrado en segundo plano
    do_delete "$id" &

    current=$((current + 1))

    # Controlar el número de procesos activos
    if (( current % max_jobs == 0 )); then
        wait # Espera a que terminen los 10 actuales antes de seguir
    fi

    # Dibujar la barra/contador en la misma línea
    percent=$(( current * 100 / total_docs ))
    printf "\r   [%-50s] %d%% (%d/%d)" $(printf "#%.0s" $(seq 1 $((percent / 2)))) "$percent" "$current" "$total_docs"

done < "$tmp_ids"
wait # Esperar los últimos procesos
echo -e "\n\n✓ ¡Limpieza completada!"
rm "$tmp_ids"
