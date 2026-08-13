#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./deploy.sh <product|all> [tag] [tier]
#
# tier is "prod" (default) or "test".
#
# Examples:
#   ./deploy.sh vetcostcheck v20260812
#   ./deploy.sh bps v20260812 test
#   ./deploy.sh all v20260812 test
#
# Env:
#   DEPLOY_DRY_RUN=1   — print resolved build/target info and exit, no az calls
#                        (namespaced so it can never be triggered by an
#                        inherited DRY_RUN meant for another script)

PRODUCT="${1:?Usage: ./deploy.sh <product|all> [tag] [tier]}"
IMAGE_TAG="${2:-latest}"
TIER_ARG="${3:-prod}"

RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
PRODUCTS=("vetcostcheck" "bps" "sanierer")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/tier.sh
source "${SCRIPT_DIR}/scripts/lib/tier.sh"

# Newline-separated, not an array: bash 3.2 (macOS default) errors on empty
# array expansion under `set -u`.
UPDATED_APPS=""

# A product's API and worker are two separate `az` calls. `set -e` aborts on the
# first failure, which leaves the tier split across two images — an API serving
# new code while its worker runs old code produces results that look wrong
# rather than absent. Say so loudly; a silent partial deploy is the dangerous one.
report_partial_deploy() {
  local status=$?
  # Not `[[ ... ]] && return` — a false test yields status 1, which `set -e`
  # would treat as a failure inside the trap itself.
  if [[ "$status" == "0" ]]; then
    return 0
  fi
  echo "" >&2
  echo "==> DEPLOY FAILED (exit ${status})." >&2
  if [[ -n "$UPDATED_APPS" ]]; then
    echo "==> These apps DID receive ${IMAGE_TAG} before the failure:" >&2
    printf '      %s\n' $UPDATED_APPS >&2
    echo "==> Everything else is still on its previous image — THE TIER IS SPLIT." >&2
    echo "==> Re-run the same command to finish. Updating to an image an app already" >&2
    echo "==> runs is a no-op, so re-running is safe." >&2
  else
    echo "==> No app was updated." >&2
  fi
  return "$status"
}
trap report_partial_deploy EXIT

# Azure intermittently answers a containerapp update with a transient 5xx
# ("Service Unavailable") — sometimes after the update has already been applied.
# Retrying is safe because `az containerapp update --image` is idempotent.
update_app_with_retry() {
  local app="$1"
  local image="$2"
  local attempt
  local max_attempts=3

  for (( attempt = 1; attempt <= max_attempts; attempt++ )); do
    if az containerapp update --name "$app" --resource-group "$RG" --image "$image"; then
      UPDATED_APPS="${UPDATED_APPS}${app}"$'\n'
      return 0
    fi
    if (( attempt < max_attempts )); then
      echo "==> WARNING: updating ${app} failed (attempt ${attempt}/${max_attempts}); retrying in $(( attempt * 15 ))s..." >&2
      sleep $(( attempt * 15 ))
    fi
  done

  echo "ERROR: ${app} did not accept image ${image} after ${max_attempts} attempts" >&2
  return 1
}

if [[ "${DEPLOY_DRY_RUN:-0}" == "1" ]]; then
  echo "== DRY RUN — nothing built or deployed =="
fi

deploy_one() {
  local product="$1"
  local tag="$2"
  local tier="$3"

  if [[ ! -d "products/${product}" ]]; then
    echo "ERROR: products/${product}/ does not exist" >&2
    return 1
  fi

  resolve_tier_names "$product" "$tier"

  local acr_server image
  if [[ "${DEPLOY_DRY_RUN:-0}" == "1" ]]; then
    acr_server="cr3cinvoice.azurecr.io"
  else
    acr_server=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
  fi
  image="${acr_server}/${IMAGE_REPO}:${tag}"

  if [[ "${DEPLOY_DRY_RUN:-0}" == "1" ]]; then
    echo "BUILD=${image}"
    echo "TARGET=${API_APP}"
    echo "TARGET=${WORKER_APP}"
    return 0
  fi

  echo "==> [${product}/${tier}] Building image ${IMAGE_REPO}:${tag} in ACR..."
  az acr build \
    --registry "$ACR_NAME" \
    --image "${IMAGE_REPO}:${tag}" \
    --build-arg "PRODUCT=${product}" \
    .

  local app
  for app in "$API_APP" "$WORKER_APP"; do
    if ! az containerapp show --name "$app" --resource-group "$RG" >/dev/null 2>&1; then
      echo "==> [${product}/${tier}] SKIP: ${app} does not exist yet (run scripts/provision_product.sh ${product} ${tag} ${tier} first)"
      continue
    fi
    echo "==> [${product}/${tier}] Updating ${app}..."
    update_app_with_retry "$app" "$image"
  done
}

if [[ "$PRODUCT" == "all" ]]; then
  for p in "${PRODUCTS[@]}"; do
    if [[ -d "products/${p}" ]]; then
      deploy_one "$p" "$IMAGE_TAG" "$TIER_ARG"
    fi
  done
else
  deploy_one "$PRODUCT" "$IMAGE_TAG" "$TIER_ARG"
fi

if [[ "${DEPLOY_DRY_RUN:-0}" != "1" ]]; then
  echo ""
  echo "==> Done."
fi
