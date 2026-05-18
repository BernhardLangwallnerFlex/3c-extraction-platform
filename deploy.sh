#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./deploy.sh <product> [tag]
#   ./deploy.sh all [tag]
#
# Example:
#   ./deploy.sh vetcostcheck v20260507
#   ./deploy.sh all v20260507

PRODUCT="${1:?Usage: ./deploy.sh <product|all> [tag]}"
IMAGE_TAG="${2:-latest}"

RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
PRODUCTS=("vetcostcheck" "bps" "sanierer")

deploy_one() {
  local product="$1"
  local tag="$2"

  if [[ ! -d "products/${product}" ]]; then
    echo "ERROR: products/${product}/ does not exist" >&2
    return 1
  fi

  local acr_server image_repo image
  acr_server=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
  image_repo="3cix-${product}"
  image="${acr_server}/${image_repo}:${tag}"

  echo "==> [${product}] Building image ${image_repo}:${tag} in ACR..."
  az acr build \
    --registry "$ACR_NAME" \
    --image "${image_repo}:${tag}" \
    --build-arg "PRODUCT=${product}" \
    .

  for kind in api worker; do
    local app="ca-${kind}-${product}"
    if ! az containerapp show --name "$app" --resource-group "$RG" >/dev/null 2>&1; then
      echo "==> [${product}] SKIP: ${app} does not exist yet (run scripts/provision_product.sh ${product} first)"
      continue
    fi
    echo "==> [${product}] Updating ${app}..."
    az containerapp update --name "$app" --resource-group "$RG" --image "$image"
  done
}

if [[ "$PRODUCT" == "all" ]]; then
  for p in "${PRODUCTS[@]}"; do
    if [[ -d "products/${p}" ]]; then
      deploy_one "$p" "$IMAGE_TAG"
    fi
  done
else
  deploy_one "$PRODUCT" "$IMAGE_TAG"
fi

echo ""
echo "==> Done."
