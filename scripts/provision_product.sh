#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/provision_product.sh <product> <image-tag>
#
# Provisions ca-api-<product> and ca-worker-<product> in the existing
# cae-3c-invoice environment. Idempotent: if both apps already exist, exits.
#
# Required env (in your shell or .env):
#   REDIS_URL                       — full rediss:// URL (app connection)
#   KEDA_REDIS_HOST                 — host:port (e.g. redis-3c-invoice-v2.redis.cache.windows.net:6380)
#   REDIS_PASSWORD                  — Redis access key (KEDA scaler auth)
#   AZURE_STORAGE_ACCOUNT_NAME
#   AZURE_STORAGE_ACCOUNT_KEY
#   AZURE_ENDPOINT
#   AZURE_OPENAI_KEY
#   AZURE_OPENAI_API_VERSION
#   MISTRAL_API_KEY
#   AZURE_DOCINTEL_ENDPOINT
#   AZURE_DOCINTEL_KEY
#
# Optional env:
#   INVOICE_API_KEY                 — generated if unset
#   SENTRY_DSN                      — Sentry disabled if unset (secret/env omitted)

PRODUCT="${1:?Usage: scripts/provision_product.sh <product> <image-tag>}"
IMAGE_TAG="${2:?Usage: scripts/provision_product.sh <product> <image-tag>}"

RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
ENV_NAME="cae-3c-invoice"

API_APP="ca-api-${PRODUCT}"
WORKER_APP="ca-worker-${PRODUCT}"
QUEUE_NAME="jobs-${PRODUCT}"
IMAGE_REPO="3cix-${PRODUCT}"

# Idempotency: skip if both apps exist
if az containerapp show --name "$API_APP" --resource-group "$RG" >/dev/null 2>&1 \
   && az containerapp show --name "$WORKER_APP" --resource-group "$RG" >/dev/null 2>&1; then
  echo "==> Both ${API_APP} and ${WORKER_APP} already exist. Nothing to do."
  exit 0
fi

# Required env values
: "${REDIS_URL:?Set REDIS_URL in your shell or .env before running}"
: "${KEDA_REDIS_HOST:?Set KEDA_REDIS_HOST (host:port for the Redis cache)}"
: "${REDIS_PASSWORD:?Set REDIS_PASSWORD (Redis access key for KEDA auth)}"
: "${AZURE_STORAGE_ACCOUNT_NAME:?}"
: "${AZURE_STORAGE_ACCOUNT_KEY:?}"
: "${AZURE_ENDPOINT:?}"
: "${AZURE_OPENAI_KEY:?}"
: "${AZURE_OPENAI_API_VERSION:?}"
: "${MISTRAL_API_KEY:?}"
: "${AZURE_DOCINTEL_ENDPOINT:?}"
: "${AZURE_DOCINTEL_KEY:?}"

# Per-product values (caller may override)
INVOICE_API_KEY="${INVOICE_API_KEY:-$(openssl rand -hex 32)}"
SENTRY_DSN="${SENTRY_DSN:-}"

# Sentry is optional. Azure Container Apps rejects empty secret values, and the
# app (core/api/main.py, core/jobs/worker.py) only calls sentry_sdk.init when
# SENTRY_DSN is non-empty. So wire the sentry secret + env var only when set.
# Empty-array expansion below uses the bash 3.2-safe idiom ${arr[@]+"${arr[@]}"}
# (no outer quotes — an empty array must yield zero args, not one empty string).
SENTRY_SECRET_ARG=()
SENTRY_ENV_ARG=()
if [[ -n "$SENTRY_DSN" ]]; then
  SENTRY_SECRET_ARG=(sentry-dsn="$SENTRY_DSN")
  SENTRY_ENV_ARG=(SENTRY_DSN=secretref:sentry-dsn)
fi

# Per-product Azure Blob prefixes (matches existing convention: blob container "invoices")
AZURE_INPUT_PREFIX="az://invoices/uploads-${PRODUCT}/"
AZURE_OUTPUT_PREFIX="az://invoices/processed-${PRODUCT}/"

# Resolve ACR
ACR_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
ACR_PASS=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)
IMAGE="${ACR_SERVER}/${IMAGE_REPO}:${IMAGE_TAG}"

# --- Provision API ---
echo "==> Creating ${API_APP}..."
az containerapp create \
  --name "$API_APP" \
  --resource-group "$RG" \
  --environment "$ENV_NAME" \
  --image "$IMAGE" \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_NAME" \
  --registry-password "$ACR_PASS" \
  --target-port 8000 \
  --ingress external \
  --transport http \
  --cpu 0.5 --memory 1.0Gi \
  --min-replicas 1 --max-replicas 3 \
  --secrets \
    redis-url="$REDIS_URL" \
    storage-account-key="$AZURE_STORAGE_ACCOUNT_KEY" \
    azure-openai-key="$AZURE_OPENAI_KEY" \
    mistral-api-key="$MISTRAL_API_KEY" \
    azure-docintel-key="$AZURE_DOCINTEL_KEY" \
    invoice-api-key="$INVOICE_API_KEY" \
    ${SENTRY_SECRET_ARG[@]+"${SENTRY_SECRET_ARG[@]}"} \
  --env-vars \
    PRODUCT_NAME="$PRODUCT" \
    RQ_QUEUE_NAME="$QUEUE_NAME" \
    STORAGE_BACKEND=azure \
    AZURE_STORAGE_ACCOUNT_NAME="$AZURE_STORAGE_ACCOUNT_NAME" \
    AZURE_STORAGE_ACCOUNT_KEY=secretref:storage-account-key \
    AZURE_INPUT_PREFIX="$AZURE_INPUT_PREFIX" \
    AZURE_OUTPUT_PREFIX="$AZURE_OUTPUT_PREFIX" \
    AZURE_ENDPOINT="$AZURE_ENDPOINT" \
    AZURE_OPENAI_KEY=secretref:azure-openai-key \
    AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION" \
    MISTRAL_API_KEY=secretref:mistral-api-key \
    AZURE_DOCINTEL_ENDPOINT="$AZURE_DOCINTEL_ENDPOINT" \
    AZURE_DOCINTEL_KEY=secretref:azure-docintel-key \
    REDIS_URL=secretref:redis-url \
    INVOICE_API_KEY=secretref:invoice-api-key \
    ${SENTRY_ENV_ARG[@]+"${SENTRY_ENV_ARG[@]}"}

# --- Provision Worker ---
echo "==> Creating ${WORKER_APP}..."
az containerapp create \
  --name "$WORKER_APP" \
  --resource-group "$RG" \
  --environment "$ENV_NAME" \
  --image "$IMAGE" \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_NAME" \
  --registry-password "$ACR_PASS" \
  --command "python" --args "core/jobs/worker.py" \
  --cpu 2.0 --memory 4.0Gi \
  --min-replicas 0 --max-replicas 5 \
  --secrets \
    redis-url="$REDIS_URL" \
    redis-password="$REDIS_PASSWORD" \
    storage-account-key="$AZURE_STORAGE_ACCOUNT_KEY" \
    azure-openai-key="$AZURE_OPENAI_KEY" \
    mistral-api-key="$MISTRAL_API_KEY" \
    azure-docintel-key="$AZURE_DOCINTEL_KEY" \
    ${SENTRY_SECRET_ARG[@]+"${SENTRY_SECRET_ARG[@]}"} \
  --env-vars \
    PRODUCT_NAME="$PRODUCT" \
    RQ_QUEUE_NAME="$QUEUE_NAME" \
    STORAGE_BACKEND=azure \
    AZURE_STORAGE_ACCOUNT_NAME="$AZURE_STORAGE_ACCOUNT_NAME" \
    AZURE_STORAGE_ACCOUNT_KEY=secretref:storage-account-key \
    AZURE_INPUT_PREFIX="$AZURE_INPUT_PREFIX" \
    AZURE_OUTPUT_PREFIX="$AZURE_OUTPUT_PREFIX" \
    AZURE_ENDPOINT="$AZURE_ENDPOINT" \
    AZURE_OPENAI_KEY=secretref:azure-openai-key \
    AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION" \
    MISTRAL_API_KEY=secretref:mistral-api-key \
    AZURE_DOCINTEL_ENDPOINT="$AZURE_DOCINTEL_ENDPOINT" \
    AZURE_DOCINTEL_KEY=secretref:azure-docintel-key \
    REDIS_URL=secretref:redis-url \
    KEDA_REDIS_HOST="$KEDA_REDIS_HOST" \
    ${SENTRY_ENV_ARG[@]+"${SENTRY_ENV_ARG[@]}"}

# --- KEDA scaler on Redis queue length ---
# Matches production (ca-invoice-worker): plain KEDA_REDIS_HOST env var + redis-password secret.
echo "==> Adding KEDA Redis scaler to ${WORKER_APP}..."
az containerapp update \
  --name "$WORKER_APP" \
  --resource-group "$RG" \
  --scale-rule-name "redis-queue" \
  --scale-rule-type "redis" \
  --scale-rule-metadata \
    "addressFromEnv=KEDA_REDIS_HOST" \
    "listName=rq:queue:${QUEUE_NAME}" \
    "listLength=1" \
    "enableTLS=true" \
  --scale-rule-auth "password=redis-password" \
  --min-replicas 0 --max-replicas 5

API_FQDN=$(az containerapp show --name "$API_APP" --resource-group "$RG" \
  --query "properties.configuration.ingress.fqdn" -o tsv)

echo ""
echo "==> Provisioned ${PRODUCT}:"
echo "    API:    https://${API_FQDN}"
echo "    Worker: ${WORKER_APP}"
echo "    Queue:  ${QUEUE_NAME}"
echo "    INVOICE_API_KEY: ${INVOICE_API_KEY}"
echo ""
echo "Next: map 3c${PRODUCT}.flex-capital-scale.com to ${API_APP} (custom domain + managed cert)"
echo "      Set scale cooldown to 1200s manually if needed:"
echo "        az containerapp update --name ${WORKER_APP} --resource-group ${RG} --scale-rule-name redis-queue --scale-rule-type redis --min-replicas 0 --max-replicas 5"
