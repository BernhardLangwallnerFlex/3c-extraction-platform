#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/apply_lifecycle_policy.sh [--apply] [--force]
#
# Applies infra/lifecycle-policy.json to the invoices storage account: a 14-day
# expiry on every uploads-*/processed-* prefix, both tiers, plus the pre-cutover
# legacy prefixes.
#
# Dry-run by default (matching promote.sh and purge_blob_backlog.py).
#
# Why this is needed at all: `Pipeline.cleanup_storage_artifacts()` deletes a
# job's upload and intermediates but deliberately KEEPS `extracted_data_*.json`,
# and deliberately keeps everything for a job where no subdocument came back
# 100. The test tier additionally runs CLEANUP_ARTIFACTS=false. Without this
# policy none of that ever expires.
#
# Azure replaces the account's management policy wholesale — there is no merge.
# The script therefore refuses to overwrite an existing policy that differs from
# this file unless --force is given, so a rule someone added in the portal
# cannot be silently discarded.

APPLY=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    *) echo "ERROR: unknown option '${arg}'" >&2; exit 1 ;;
  esac
done

ACCOUNT="${STORAGE_ACCOUNT_NAME:-3cixstorage}"
RG="${STORAGE_RESOURCE_GROUP:-3c_information_extraction}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY_FILE="${SCRIPT_DIR}/../infra/lifecycle-policy.json"

if [[ ! -f "$POLICY_FILE" ]]; then
  echo "ERROR: ${POLICY_FILE} not found" >&2
  exit 1
fi

if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$POLICY_FILE" 2>/dev/null; then
  echo "ERROR: ${POLICY_FILE} is not valid JSON" >&2
  exit 1
fi

echo "Account: ${ACCOUNT} (resource group ${RG})"
echo "Policy:  ${POLICY_FILE}"
echo ""
echo "Rules to apply:"
python3 - "$POLICY_FILE" <<'PY'
import json, sys
policy = json.load(open(sys.argv[1]))
for rule in policy["rules"]:
    days = rule["definition"]["actions"]["baseBlob"]["delete"]["daysAfterModificationGreaterThan"]
    prefixes = rule["definition"]["filters"]["prefixMatch"]
    state = "enabled" if rule.get("enabled") else "DISABLED"
    print(f"  {rule['name']} ({state}) — delete {days}d after last modification")
    for p in prefixes:
        print(f"      {p}")
PY
echo ""

# --- Guard: don't silently clobber a policy someone else put there ----------
EXISTING="$(az storage account management-policy show \
  --account-name "$ACCOUNT" --resource-group "$RG" \
  --query "policy" -o json 2>/dev/null || true)"

if [[ -n "$EXISTING" && "$EXISTING" != "null" ]]; then
  if python3 - "$POLICY_FILE" <<PY
import json, sys
existing = json.loads('''${EXISTING}''')
desired = json.load(open(sys.argv[1]))
sys.exit(0 if existing.get("rules") == desired.get("rules") else 1)
PY
  then
    echo "==> A policy is already applied and matches this file. Nothing to do."
    exit 0
  fi

  echo "WARNING: a DIFFERENT management policy is already applied to ${ACCOUNT}." >&2
  echo "         Applying this file REPLACES it wholesale — Azure does not merge." >&2
  echo "         Current policy:" >&2
  echo "$EXISTING" | python3 -c "import json,sys; print('\n'.join('           ' + r.get('name','?') for r in json.load(sys.stdin).get('rules',[])))" >&2
  if [[ "$FORCE" -eq 0 ]]; then
    echo "         Re-run with --force to replace it." >&2
    exit 1
  fi
  echo "         --force given; replacing." >&2
fi

if [[ "$APPLY" -eq 0 ]]; then
  echo "== DRY RUN — no changes made. Re-run with --apply to execute. =="
  exit 0
fi

echo "==> Applying policy to ${ACCOUNT}..."
az storage account management-policy create \
  --account-name "$ACCOUNT" \
  --resource-group "$RG" \
  --policy "@${POLICY_FILE}" \
  -o none

echo ""
echo "==> Applied. Rules now live:"
az storage account management-policy show \
  --account-name "$ACCOUNT" --resource-group "$RG" \
  --query "policy.rules[].{name:name, enabled:enabled}" -o table

echo ""
echo "NOTE: Azure evaluates lifecycle rules once per day, so nothing is deleted"
echo "      immediately. Blob soft delete (7 days) still covers what it removes."
