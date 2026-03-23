#!/usr/bin/env bash
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────
RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
API_APP="ca-invoice-api"
WORKER_APP="ca-invoice-worker"
IMAGE_TAG="${1:-latest}"

# ── Resolve ACR ────────────────────────────────────────────────
ACR_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
IMAGE="${ACR_SERVER}/invoice-app:${IMAGE_TAG}"

# ── Build remotely in ACR ──────────────────────────────────────
echo "==> Building image in ACR (tag: ${IMAGE_TAG})..."
az acr build --registry "$ACR_NAME" --image "invoice-app:${IMAGE_TAG}" .

# ── Update container apps ──────────────────────────────────────
echo "==> Updating API..."
az containerapp update \
  --name "$API_APP" \
  --resource-group "$RG" \
  --image "$IMAGE"

echo "==> Updating Worker..."
az containerapp update \
  --name "$WORKER_APP" \
  --resource-group "$RG" \
  --image "$IMAGE"

# ── Status ─────────────────────────────────────────────────────
echo ""
echo "==> Active revisions:"
echo "--- API ---"
az containerapp revision list --name "$API_APP" --resource-group "$RG" -o table
echo "--- Worker ---"
az containerapp revision list --name "$WORKER_APP" --resource-group "$RG" -o table

API_FQDN=$(az containerapp show --name "$API_APP" --resource-group "$RG" \
  --query "properties.configuration.ingress.fqdn" -o tsv)
echo ""
echo "==> API URL: https://${API_FQDN}"
echo "==> Done."
