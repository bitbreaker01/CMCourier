#!/usr/bin/env bash
#
# Register and activate the cmcourier:cmcourierBacModel custom content model
# in the staging Alfresco. Idempotent: safe to re-run; no-op when the model
# is already ACTIVE.
#
# Usage (from this directory, on the staging host):
#   bash register-model.sh
#
# Environment overrides:
#   ALFRESCO_BASE  default http://localhost:8080
#   ALFRESCO_USER  default admin
#   ALFRESCO_PASS  default admin
#
# WHY this exists: Alfresco 23.x Community does NOT pick up custom models
# from Spring contexts in the shared classpath any more (that path silently
# does nothing while reserving the namespace, blocking subsequent uploads).
# The only working path on Community is the `cmm/upload` webscript, which
# accepts a ZIP containing the model XML and stores the model in Postgres.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_XML="${SCRIPT_DIR}/cmcourier-model.xml"

# Must match `name="..."` inside cmcourier-model.xml (after the `cmcourier:`
# prefix). Globally unique, NOT just unique within the namespace.
MODEL_NAME="cmcourierBacModel"

ALFRESCO_BASE="${ALFRESCO_BASE:-http://localhost:8080}"
ALFRESCO_USER="${ALFRESCO_USER:-admin}"
ALFRESCO_PASS="${ALFRESCO_PASS:-admin}"

CMM_V1="${ALFRESCO_BASE}/alfresco/api/-default-/private/alfresco/versions/1/cmm"
CMM_UPLOAD="${ALFRESCO_BASE}/alfresco/service/api/cmm/upload"
CMIS_BASE="${ALFRESCO_BASE}/alfresco/api/-default-/public/cmis/versions/1.1/browser"
AUTH=( -u "${ALFRESCO_USER}:${ALFRESCO_PASS}" )

require() { command -v "$1" >/dev/null || { echo "missing required command: $1" >&2; exit 1; }; }
require curl
require zip
require python3

if [[ ! -f "${MODEL_XML}" ]]; then
  echo "Model XML not found at ${MODEL_XML}" >&2
  exit 1
fi

current_status() {
  curl -sS "${AUTH[@]}" -o /tmp/cmm-status.$$.json -w "%{http_code}" "${CMM_V1}/${MODEL_NAME}"
}

extract_status() {
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("entry",{}).get("status","UNKNOWN"))' "$1"
}

echo "==> Checking current state of model ${MODEL_NAME}..."
HTTP=$(current_status || true)
case "${HTTP}" in
  200)
    STATUS=$(extract_status /tmp/cmm-status.$$.json)
    rm -f /tmp/cmm-status.$$.json
    if [[ "${STATUS}" == "ACTIVE" ]]; then
      echo "Model already ACTIVE — nothing to do."
      exit 0
    fi
    echo "Model exists with status=${STATUS}; will activate."
    ;;
  404)
    rm -f /tmp/cmm-status.$$.json
    echo "Model not present yet; will upload."

    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "${TMP_DIR}"' EXIT
    cp "${MODEL_XML}" "${TMP_DIR}/cmcourier-model.xml"
    ( cd "${TMP_DIR}" && zip -q cmcourier-model.zip cmcourier-model.xml )

    echo "==> POST ${CMM_UPLOAD}"
    UPLOAD_RESP=$(curl -sS "${AUTH[@]}" -w "\n%{http_code}" -F "filedata=@${TMP_DIR}/cmcourier-model.zip" "${CMM_UPLOAD}")
    UPLOAD_CODE="${UPLOAD_RESP##*$'\n'}"
    UPLOAD_BODY="${UPLOAD_RESP%$'\n'*}"
    if [[ "${UPLOAD_CODE}" != "200" ]]; then
      echo "Upload failed (HTTP ${UPLOAD_CODE}):" >&2
      echo "${UPLOAD_BODY}" >&2
      exit 1
    fi
    echo "Upload OK: ${UPLOAD_BODY}"
    ;;
  *)
    cat /tmp/cmm-status.$$.json >&2
    rm -f /tmp/cmm-status.$$.json
    echo "Unexpected HTTP ${HTTP} from ${CMM_V1}/${MODEL_NAME}" >&2
    exit 1
    ;;
esac

echo "==> PUT ${CMM_V1}/${MODEL_NAME}?select=status status=ACTIVE"
ACTIVATE_CODE=$(curl -sS "${AUTH[@]}" -o /tmp/cmm-activate.$$.json -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -X PUT "${CMM_V1}/${MODEL_NAME}?select=status" \
  -d '{"status":"ACTIVE"}')
if [[ "${ACTIVATE_CODE}" != "200" ]]; then
  echo "Activate failed (HTTP ${ACTIVATE_CODE}):" >&2
  cat /tmp/cmm-activate.$$.json >&2
  rm -f /tmp/cmm-activate.$$.json
  exit 1
fi
rm -f /tmp/cmm-activate.$$.json
echo "Activate OK."

echo "==> Verifying via CMIS typeDefinition for D:cmcourier:bacDoc"
TYPEDEF_CODE=$(curl -sS "${AUTH[@]}" -o /tmp/cmm-typedef.$$.json -w "%{http_code}" \
  "${CMIS_BASE}?cmisselector=typeDefinition&typeId=D:cmcourier:bacDoc")
if [[ "${TYPEDEF_CODE}" != "200" ]]; then
  echo "WARN: CMIS still reports HTTP ${TYPEDEF_CODE} for D:cmcourier:bacDoc."
  echo "      The dictionary cache may need a restart of the alfresco container"
  echo "      to flush. Run:  docker compose -f alfresco-compose.yml restart alfresco"
else
  # heredoc en vez de -c '...': el f-string necesita comillas adentro y
  # cualquier variante rompía el quoting de bash (SyntaxError que, con
  # set -e, abortaba el script aunque el registro hubiera salido bien).
  python3 - /tmp/cmm-typedef.$$.json <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
props = d.get("propertyDefinitions", {})
cmc = sorted(k for k in props if k.startswith("cmcourier:"))
print(f"  type id      : {d.get('id')}")
print(f"  parent       : {d.get('parentId')}")
print(f"  queryable    : {d.get('queryable')}")
print(f"  cmcourier:*  : {len(cmc)} properties:")
for k in cmc:
    p = props[k]
    print(f"    - {k:35} type={p.get('propertyType'):8} required={p.get('required')}")
PYEOF
fi
rm -f /tmp/cmm-typedef.$$.json

echo
echo "Done. Custom model cmcourier:${MODEL_NAME} is ACTIVE."
