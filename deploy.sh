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

PRODUCT="${1:?Usage: ./deploy.sh <product|all> [tag] [tier]}"
IMAGE_TAG="${2:-latest}"
TIER_ARG="${3:-prod}"

RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
PRODUCTS=("vetcostcheck" "bps" "sanierer")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/tier.sh
source "${SCRIPT_DIR}/scripts/lib/tier.sh"

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
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    acr_server="cr3cinvoice.azurecr.io"
  else
    acr_server=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
  fi
  image="${acr_server}/${IMAGE_REPO}:${tag}"

  if [[ "${DRY_RUN:-0}" == "1" ]]; then
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
    az containerapp update --name "$app" --resource-group "$RG" --image "$image"
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
