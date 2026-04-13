# Azure Deployment Plan — 3C Invoice Extraction

## Context

The system is currently dep
: FastAPI API, RQ worker, and managed Redis. We're migrating to Azure for better scaling and invoicing. Azure Blob Storage and Azure OpenAI are already in use.

---

## Architecture Overview

```
                  ┌──────────────────────────────────────────────────┐
                  │  Azure Container Apps Environment (cae-3c-invoice)│
                  │                                                    │
  HTTPS           │  ┌─────────────────┐    ┌──────────────────────┐  │
  ───────────────►│  │  ca-invoice-api  │    │  ca-invoice-worker   │  │
  3cvetcostcheck. │  │  0.5 vCPU / 1 GB│    │  2 vCPU / 4 GB      │  │
  flex-capital-   │  │  FastAPI/uvicorn │    │  python jobs/worker  │  │
  scale.com       │  │  min:1 max:3     │    │  min:1 max:5         │  │
                  │  └────────┬────────┘    └──────────┬───────────┘  │
                  └───────────┼────────────────────────┼──────────────┘
                              │                        │
                    ┌─────────▼────────────────────────▼─────────┐
                    │  Azure Cache for Redis (redis-3c-invoice-v2)  │
                    │  Basic C0 · 250 MB · TLS on port 6380      │
                    └────────────────────────────────────────────┘
                              │                        │
              ┌───────────────▼──┐        ┌────────────▼──────────┐
              │  Azure Blob      │        │  Azure OpenAI         │
              │  Storage         │        │  (3cinfoextraction)   │
              │  (3cixstorage)   │        │  gpt-4.1 / gpt-4o    │
              │  EXISTING        │        │  EXISTING             │
              └──────────────────┘        └───────────────────────┘
```

| Component | Azure Service | SKU | Est. Cost/mo |
|-----------|--------------|-----|-------------|
| API | Container Apps | 0.5 vCPU / 1 GiB, min 1 | ~€25 |
| Worker | Container Apps | 2 vCPU / 4 GiB, min 0 (KEDA) | ~€40-100* |
| Redis | Azure Cache for Redis | Basic C0 (250 MB) | ~€15 |
| Container Registry | ACR | Basic | ~€5 |
| Log Analytics | Log Analytics Workspace | Pay-per-GB | ~€5-10 |
| Blob Storage | Already exists | — | existing |
| Azure OpenAI | Already exists | — | existing |
| **Total new** | | | **~€90-155/mo** |

*Worker cost depends on usage. Scales to 0 when queue is empty (1200s cooldown).
At ~40% utilization during business hours, expect ~€40-60. Full-time would be ~€100.

---

## Why These Services

### Azure Container Apps (ACA) over alternatives

- **vs App Service**: Can't independently scale API and worker from one plan. ACA natively supports multiple container apps with independent scaling in one environment.
- **vs ACI**: No built-in scaling, no managed ingress, no health probes. Manual orchestration required.
- **vs AKS**: Massive operational overhead (cluster mgmt, node pools, Helm) not justified for 2 containers.
- **ACA wins**: Serverless billing, independent scaling, built-in HTTPS ingress, KEDA-based autoscaling on Redis queue length, native ACR integration.

### Azure Cache for Redis over alternatives

- **vs Service Bus / Queue Storage**: Would require replacing RQ entirely (rewriting job queue layer, status tracking, result retrieval). RQ depends on Redis in 3 call sites. Zero code changes with managed Redis.
- Basic C1 (1 GB) is plenty — RQ stores job metadata (~5 KB/job) and results (~50 KB JSON).

---

## Resource Naming

```
Resource Group:        rg-3c-invoice
Location:              germanywestcentral
Container Registry:    cr3cinvoice
ACA Environment:       cae-3c-invoice
API Container App:     ca-invoice-api
Worker Container App:  ca-invoice-worker
Redis Cache:           redis-3c-invoice-v2
Log Analytics:         law-3c-invoice
```

---

## Phase 1: Provision Infrastructure (CLI)

All commands use `az` CLI. Run `az login` first.

### 1.1 Resource Group

```bash
RG="rg-3c-invoice"
LOCATION="germanywestcentral"

az group create --name $RG --location $LOCATION
```

### 1.2 Azure Container Registry

```bash
ACR_NAME="cr3cinvoice"

az acr create \
  --resource-group $RG \
  --name $ACR_NAME \
  --sku Basic \
  --admin-enabled true
```

### 1.3 Azure Cache for Redis

Takes ~15 minutes to provision.

```bash
REDIS_NAME="redis-3c-invoice-v2"

az redis create \
  --resource-group $RG \
  --name $REDIS_NAME \
  --location $LOCATION \
  --sku Basic \
  --vm-size c0 \
  --redis-version 6 \
  --enable-non-ssl-port false
```

### 1.4 Log Analytics + Container Apps Environment

```bash
az monitor log-analytics workspace create \
  --resource-group $RG \
  --workspace-name law-3c-invoice \
  --location $LOCATION

LAW_ID=$(az monitor log-analytics workspace show \
  --resource-group $RG \
  --workspace-name law-3c-invoice \
  --query customerId -o tsv)

LAW_KEY=$(az monitor log-analytics workspace get-shared-keys \
  --resource-group $RG \
  --workspace-name law-3c-invoice \
  --query primarySharedKey -o tsv)

az containerapp env create \
  --name cae-3c-invoice \
  --resource-group $RG \
  --location $LOCATION \
  --logs-workspace-id "$LAW_ID" \
  --logs-workspace-key "$LAW_KEY"
```

---

## Phase 2: Build & Push Docker Image

Use `az acr build` to build remotely in Azure (avoids pushing a 2+ GB image over local internet).

```bash
ACR_NAME="cr3cinvoice"

# From the project root:
az acr build --registry $ACR_NAME --image invoice-app:v1 .
```

This uses the existing `Dockerfile` (updated to use `requirements.prod.txt` — see Code Changes below).

---

## Phase 3: Get Redis Connection String

```bash
REDIS_NAME="redis-3c-invoice-v2"
RG="rg-3c-invoice"

REDIS_HOST=$(az redis show --name $REDIS_NAME --resource-group $RG --query hostName -o tsv)
REDIS_KEY=$(az redis list-keys --name $REDIS_NAME --resource-group $RG --query primaryKey -o tsv)

# Azure Cache for Redis requires TLS (port 6380)
# The ?ssl_cert_reqs=none handles Azure's managed TLS certificates with redis-py
REDIS_URL="rediss://:${REDIS_KEY}@${REDIS_HOST}:6380/0?ssl_cert_reqs=none"

echo "REDIS_URL=$REDIS_URL"
```

---

## Phase 4: Deploy Container Apps

### 4.1 Deploy API

```bash
RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
ACR_SERVER=$(az acr show --name $ACR_NAME --query loginServer -o tsv)
ACR_PASS=$(az acr credential show --name $ACR_NAME --query "passwords[0].value" -o tsv)

# Step 1: Create the app with plain env vars
az containerapp create \
  --name ca-invoice-api \
  --resource-group $RG \
  --environment cae-3c-invoice \
  --image "${ACR_SERVER}/invoice-app:v1" \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_NAME" \
  --registry-password "$ACR_PASS" \
  --target-port 8000 \
  --ingress external \
  --transport http \
  --cpu 0.5 \
  --memory 1.0Gi \
  --min-replicas 1 \
  --max-replicas 3 \
  --env-vars \
    STORAGE_BACKEND=azure \
    AZURE_STORAGE_ACCOUNT_NAME=3cixstorage \
    AZURE_INPUT_PREFIX="az://invoices/uploads/" \
    AZURE_OUTPUT_PREFIX="az://invoices/processed/" \
    RQ_QUEUE_NAME=invoice-jobs \
    AZURE_ENDPOINT="https://3cinfoextraction.cognitiveservices.azure.com/" \
    AZURE_OPENAI_API_VERSION="2024-12-01-preview" \
    OPENAI_TEXT_MODEL=gpt-4.1 \
    OPENAI_VISION_MODEL=gpt-4o

# Step 2: Set secrets
az containerapp secret set \
  --name ca-invoice-api \
  --resource-group $RG \
  --secrets \
    redis-url="${REDIS_URL}" \
    azure-storage-key="<YOUR_AZURE_STORAGE_ACCOUNT_KEY>" \
    openai-api-key="<YOUR_OPENAI_API_KEY>" \
    vision-agent-key="<YOUR_VISION_AGENT_API_KEY>" \
    invoice-api-key="<YOUR_INVOICE_API_KEY>" \
    azure-openai-key="<YOUR_AZURE_OPENAI_KEY>"

# Step 3: Bind secrets to env vars
az containerapp update \
  --name ca-invoice-api \
  --resource-group $RG \
  --set-env-vars \
    REDIS_URL=secretref:redis-url \
    AZURE_STORAGE_ACCOUNT_KEY=secretref:azure-storage-key \
    OPENAI_API_KEY=secretref:openai-api-key \
    VISION_AGENT_API_KEY=secretref:vision-agent-key \
    INVOICE_API_KEY=secretref:invoice-api-key \
    AZURE_OPENAI_KEY=secretref:azure-openai-key
```

### 4.2 Deploy Worker

```bash
# Step 1: Create the worker (no ingress, more resources, different command)
az containerapp create \
  --name ca-invoice-worker \
  --resource-group $RG \
  --environment cae-3c-invoice \
  --image "${ACR_SERVER}/invoice-app:v1" \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_NAME" \
  --registry-password "$ACR_PASS" \
  --cpu 2 \
  --memory 4.0Gi \
  --min-replicas 0 \
  --max-replicas 5 \
  --cooldown-period 1200 \
  --scale-rule-name redis-queue \
  --scale-rule-type redis \
  --scale-rule-metadata \
    "hostFromEnv=REDIS_URL" \
    "listName=rq:queue:invoice-jobs" \
    "listLength=1" \
    "enableTLS=true" \
  --scale-rule-auth "password=redis-url" \
  --command "python" "jobs/worker.py" \
  --env-vars \
    STORAGE_BACKEND=azure \
    AZURE_STORAGE_ACCOUNT_NAME=3cixstorage \
    AZURE_INPUT_PREFIX="az://invoices/uploads/" \
    AZURE_OUTPUT_PREFIX="az://invoices/processed/" \
    RQ_QUEUE_NAME=invoice-jobs \
    AZURE_ENDPOINT="https://3cinfoextraction.cognitiveservices.azure.com/" \
    AZURE_OPENAI_API_VERSION="2024-12-01-preview" \
    OPENAI_TEXT_MODEL=gpt-4.1 \
    OPENAI_VISION_MODEL=gpt-4o

# Step 2: Set secrets (same values, separate app)
az containerapp secret set \
  --name ca-invoice-worker \
  --resource-group $RG \
  --secrets \
    redis-url="${REDIS_URL}" \
    azure-storage-key="<YOUR_AZURE_STORAGE_ACCOUNT_KEY>" \
    openai-api-key="<YOUR_OPENAI_API_KEY>" \
    vision-agent-key="<YOUR_VISION_AGENT_API_KEY>" \
    azure-openai-key="<YOUR_AZURE_OPENAI_KEY>"

# Step 3: Bind secrets to env vars
az containerapp update \
  --name ca-invoice-worker \
  --resource-group $RG \
  --set-env-vars \
    REDIS_URL=secretref:redis-url \
    AZURE_STORAGE_ACCOUNT_KEY=secretref:azure-storage-key \
    OPENAI_API_KEY=secretref:openai-api-key \
    VISION_AGENT_API_KEY=secretref:vision-agent-key \
    AZURE_OPENAI_KEY=secretref:azure-openai-key
```

---

## Phase 5: Custom Domain Setup

Domain: `3cvetcostcheck.flex-capital-scale.com`

### 5.1 Get the verification and CNAME values

```bash
# Get the default FQDN of the API app
API_FQDN=$(az containerapp show \
  --name ca-invoice-api \
  --resource-group $RG \
  --query "properties.configuration.ingress.fqdn" -o tsv)

echo "Default URL: https://$API_FQDN"

# Get the ACA environment's custom domain verification ID
VERIFICATION_ID=$(az containerapp env show \
  --name cae-3c-invoice \
  --resource-group $RG \
  --query "properties.customDomainConfiguration.customDomainVerificationId" -o tsv)

echo "Verification ID: $VERIFICATION_ID"
```

### 5.2 Configure DNS (at your DNS provider for flex-capital-scale.com)

Add these records:

| Type | Name | Value |
|------|------|-------|
| TXT | `asuid.3cvetcostcheck` | `<VERIFICATION_ID from above>` |
| CNAME | `3cvetcostcheck` | `<API_FQDN from above>` |

**Important**: Remove the existing CNAME pointing to Render first.

### 5.3 Bind the custom domain with managed certificate

```bash
# After DNS propagation (may take a few minutes):
az containerapp hostname add \
  --name ca-invoice-api \
  --resource-group $RG \
  --hostname 3cvetcostcheck.flex-capital-scale.com

# Bind a free managed TLS certificate
az containerapp hostname bind \
  --name ca-invoice-api \
  --resource-group $RG \
  --hostname 3cvetcostcheck.flex-capital-scale.com \
  --environment cae-3c-invoice \
  --validation-method CNAME
```

---

## Phase 6: Health Probes

```bash
# Export the app config as YAML, add probes, re-import
az containerapp show \
  --name ca-invoice-api \
  --resource-group $RG \
  -o yaml > /tmp/api-app.yaml
```

Add to the container spec in the YAML:

```yaml
probes:
  - type: Liveness
    httpGet:
      path: /healthz
      port: 8000
    initialDelaySeconds: 10
    periodSeconds: 30
  - type: Readiness
    httpGet:
      path: /healthz
      port: 8000
    initialDelaySeconds: 5
    periodSeconds: 10
```

Then apply:

```bash
az containerapp update \
  --name ca-invoice-api \
  --resource-group $RG \
  --yaml /tmp/api-app.yaml
```

---

## Code Changes Required

### 1. Switch tasks.py to AzureInvoiceProcessor

In `jobs/tasks.py`, replace the GPTInvoiceProcessor with AzureInvoiceProcessor:

```python
# BEFORE (current):
from processors.gpt_processor import GPTInvoiceProcessor
processor = GPTInvoiceProcessor(
    name="gpt_processor",
    api_key=os.getenv("OPENAI_API_KEY"),
    model=os.getenv("OPENAI_TEXT_MODEL", "gpt-4"),
    vision_model=os.getenv("OPENAI_VISION_MODEL", "gpt-4o"),
)

# AFTER:
from processors.azure_processor import AzureInvoiceProcessor
processor = AzureInvoiceProcessor(
    name="azure_processor",
    api_key=os.getenv("AZURE_OPENAI_KEY"),
    model=os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1"),
    vision_model=os.getenv("OPENAI_VISION_MODEL", "gpt-4o"),
    azure_endpoint=os.getenv("AZURE_ENDPOINT"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
)
```

Also switch `invoice.py:analyze_document()` which directly uses OpenAI client — it should use Azure OpenAI:

```python
# BEFORE:
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# AFTER:
from openai import AzureOpenAI
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_KEY"),
    azure_endpoint=os.getenv("AZURE_ENDPOINT"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
)
```

### 2. Redis SSL Compatibility

Azure Cache for Redis requires TLS. The `rediss://` URL scheme with `?ssl_cert_reqs=none` parameter should work with `redis-py 7.1.0` out of the box via `Redis.from_url()`. No code changes needed — the URL parameter handles it.

Call sites (for reference, no changes required):
- `jobs/worker.py:8` — `Redis.from_url(os.environ["REDIS_URL"])`
- `api/routes/process.py:16` — `Redis.from_url(redis_url)`
- `api/routes/job.py:13` — `Redis.from_url(os.environ["REDIS_URL"])`

### 3. Create requirements.prod.txt

Strip unused dependencies from the production image. Remove:
- `boto3` (S3 — no longer needed)
- `matplotlib` (only for notebooks)
- `streamlit`-adjacent packages (`altair`, `pydeck`, etc.)
- `mistralai` (unused OCR alternative)
- Google Vision packages (`google-cloud-vision`, etc.)
- `faker`, `polyfactory` (testing only)
- `xlsxwriter`, `openpyxl` (not used in pipeline)
- Notebook packages (`ipython`, `jupyter`, etc.)

Keep everything needed for the core pipeline: `fastapi`, `uvicorn`, `redis`, `rq`, `openai`, `pymupdf`, `pillow`, `pydantic`, `python-dotenv`, `landingai-ade`, `docling`, `azure-storage-blob`, `transformers`, `opencv-python-headless`, etc.

### 4. Update Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.prod.txt .
RUN pip install --no-cache-dir -r requirements.prod.txt

COPY . .

ENV PYTHONPATH="/app:${PYTHONPATH}"

CMD ["bash", "-lc", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
```

---

## deploy.sh — Redeployment Script

After initial setup, use this script to deploy code changes:

```bash
#!/usr/bin/env bash
set -euo pipefail

RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
IMAGE_TAG="${1:-latest}"

ACR_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
IMAGE="${ACR_SERVER}/invoice-app:${IMAGE_TAG}"

echo "==> Building image in ACR (tag: ${IMAGE_TAG})..."
az acr build --registry "$ACR_NAME" --image "invoice-app:${IMAGE_TAG}" .

echo "==> Updating API container app..."
az containerapp update \
  --name ca-invoice-api \
  --resource-group "$RG" \
  --image "$IMAGE"

echo "==> Updating Worker container app..."
az containerapp update \
  --name ca-invoice-worker \
  --resource-group "$RG" \
  --image "$IMAGE"

echo "==> Active revisions:"
echo "--- API ---"
az containerapp revision list --name ca-invoice-api --resource-group "$RG" -o table
echo "--- Worker ---"
az containerapp revision list --name ca-invoice-worker --resource-group "$RG" -o table

API_FQDN=$(az containerapp show --name ca-invoice-api --resource-group "$RG" \
  --query "properties.configuration.ingress.fqdn" -o tsv)
echo "==> API URL: https://${API_FQDN}"
echo "==> Done."
```

Usage: `./deploy.sh v2` (or `./deploy.sh` for `latest` tag).

---

## Scaling

### Current defaults

| App | vCPU | RAM | Min | Max | Scaling |
|-----|------|-----|-----|-----|---------|
| API | 0.5 | 1 GiB | 1 | 3 | HTTP-based (default) |
| Worker | 2 | 4 GiB | 0 | 5 | KEDA on Redis queue, 1200s cooldown |

The worker scales to 0 when the Redis queue is empty for 1200 seconds (≈3.3 hours).
Jobs enqueued while the worker is at 0 replicas are not lost — they wait in Redis until
KEDA detects the queue item and spins up a replica (~30-60s cold start).

### Upgrade paths

**If worker hits OOM** (check in Log Analytics):
```bash
az containerapp update --name ca-invoice-worker --resource-group $RG \
  --cpu 4 --memory 8.0Gi
```

**If you need more concurrent processing**:
```bash
az containerapp update --name ca-invoice-worker --resource-group $RG \
  --max-replicas 10
```

**If Redis needs HA** (adds replica + SLA):
```bash
az redis update --name redis-3c-invoice-v2 --resource-group $RG --sku Standard
```

---

## Verification Checklist

After deployment, verify end-to-end:

```bash
API_URL="https://3cvetcostcheck.flex-capital-scale.com"
API_KEY="<your INVOICE_API_KEY>"

# 1. Health check
curl -s "$API_URL/healthz"
# Expected: {"status":"ok"}

# 2. Upload a test PDF
curl -s -X POST "$API_URL/upload" \
  -H "X-Api-Key: $API_KEY" \
  -F "file=@test_invoice.pdf"
# Expected: {"file_id":"<uuid>.pdf"}

# 3. Trigger processing
curl -s -X POST "$API_URL/process" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"file_id":"<file_id from step 2>"}'
# Expected: {"job_id":"...","status":"queued","queue":"invoice-jobs"}

# 4. Poll for result
curl -s "$API_URL/job/<job_id>" -H "X-Api-Key: $API_KEY"
# Expected: {"job_id":"...","status":"finished","result":{...}}

# 5. Check worker logs if something fails
az containerapp logs show \
  --name ca-invoice-worker \
  --resource-group rg-3c-invoice \
  --follow
```

---

## Migration Sequence

1. **Provision infra** (Phase 1) — ~20 min (Redis takes longest)
2. **Code changes** — switch to AzureInvoiceProcessor, create requirements.prod.txt
3. **Build & push image** (Phase 2)
4. **Deploy API + Worker** (Phase 3-4)
5. **Test with default ACA URL** — verify end-to-end before DNS switch
6. **DNS cutover** — remove Render CNAME, add Azure CNAME + TXT verification
7. **Bind custom domain** (Phase 5) — managed TLS certificate
8. **Decommission Render** — once everything is verified
