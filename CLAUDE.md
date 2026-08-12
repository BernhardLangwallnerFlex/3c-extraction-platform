# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Veterinary invoice data extraction pipeline for 3C. Accepts PDF invoices (often multi-invoice PDFs), splits them into sub-documents, performs OCR, then uses LLM vision+text to extract structured JSON (line items, totals, sender/recipient, animals, GOT codes, etc.). Domain language is German.

## Local Development

Copy `.env.example` to `.env` and fill in API keys (Mistral, Azure OpenAI, Azure Doc Intel).

**Docker mode (full stack):**
```bash
docker compose up --build        # API (port 8000) + worker + Redis
python test_api.py               # uploads a PDF and polls until done
```

**Native mode (faster iteration, hot-reload):**
```bash
docker compose up redis           # Redis only
# Terminal 1 — API (auto-reloads on code changes):
STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 uvicorn core.api.main:app --host 0.0.0.0 --port 8000 --reload --log-level debug
# Terminal 2 — Worker (restart manually after code changes):
STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 python -m core.jobs.worker
# Terminal 3 — Test:
python test_api.py
```

Output files land in `./temp/` when using local storage.

## Commands

```bash
# Ad-hoc processing script (edit main.py to set file paths)
python main.py

# Deploy to Azure Container Apps (builds in ACR + updates both apps)
./deploy.sh [tag]
```

Tests: `.venv/bin/python -m pytest tests/` (pytest 9.x). No linter configured. Python 3.11+ (Dockerfile uses 3.11-slim).

## Architecture

### Processing Pipeline

The core flow is: **Upload PDF -> OCR -> Analyze/Split -> Extract per sub-document -> JSON output**

1. **`invoice.py` — `Invoice` class**: Orchestrates the entire pipeline for one input file. Key methods run in sequence:
   - `extract_markdown()` — OCR via pluggable engine, produces per-page markdown
   - `analyze_document()` — LLM call (OpenAI) to identify which pages belong to which sub-invoice and detect animal info
   - `split_document_into_invoices()` — splits PDF by page ranges from analysis, creates sub-PDFs + concatenated images + markdown, uploads all artifacts to storage
   - `extract_data_from_subdocuments(processor)` — runs LLM extraction on each subdocument via a processor, stores final JSON

2. **`jobs/tasks.py` — `process_file(file_id)`**: Production entry point. Wires up storage, OCR engine, and processor from env vars, then runs the Invoice pipeline. Called by the RQ worker.

### API Layer (`api/`)

FastAPI app with API key auth (`X-Api-Key` header, checked against `INVOICE_API_KEY` env var).

- `POST /upload` — saves file to storage, returns `file_id`
- `POST /process` — enqueues `process_file` on RQ, returns `job_id`
- `GET /job/{job_id}` — polls job status/result from Redis
- `GET /healthz` — health check (no auth)

### Storage (`storage/`)

`StorageBackend` protocol with three implementations: `LocalStorage`, `S3Storage` (`s3://` URIs), `AzureBlobStorage` (`az://` URIs). Selected by `STORAGE_BACKEND` env var (`local`/`s3`/`azure`). All file I/O goes through this abstraction.

`storage/file_storage.py` handles upload persistence and file_id -> storage key resolution.

### OCR Engines (`ocr/`)

All inherit from `BaseOCREngine`. Production engine is `DualOCRProcessor` (`ocr/ocr_dual.py`) which runs Mistral OCR and Azure Document Intelligence in parallel, merging their outputs per page. If one engine fails (after 3 retries), it gracefully degrades to single-engine mode and fires a Sentry warning. Others exist for experimentation (Tesseract, Google Vision, Docling).

### LLM Processors (`processors/`)

Production uses `AzureInvoiceProcessor` (Azure OpenAI). `GPTInvoiceProcessor` (direct OpenAI) also exists. Both send vision+text prompts and parse JSON responses. The extraction prompt and schema are defined in `prompt_building/prompt_building.py` and `configs/extraction_config.json`.

### Prompt Building (`prompt_building/`)

`build_prompt_for_analyze_document()` — analysis/splitting prompt (from config template).
`get_full_prompt()` — hardcoded German extraction prompt with full JSON schema.
`build_prompt_from_config()` — config-driven extraction prompt.

## Key Environment Variables

- `STORAGE_BACKEND` — `local`, `s3`, or `azure`
- `CLEANUP_ARTIFACTS` — `true` (default) deletes the upload and per-subdocument artifacts from storage after a successful job; the result JSON survives and is expired by the container's 14-day lifecycle rule. Set to `false` for local work so artifacts stay inspectable.
- `AZURE_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_API_VERSION` — for Azure OpenAI processor
- `MISTRAL_API_KEY` — for Mistral OCR
- `AZURE_DOCINTEL_ENDPOINT`, `AZURE_DOCINTEL_KEY` — for Azure Document Intelligence OCR
- `AZURE_STORAGE_ACCOUNT_NAME`, `AZURE_STORAGE_ACCOUNT_KEY` — for Azure blob storage
- `REDIS_URL` — Redis connection for RQ job queue
- `INVOICE_API_KEY` — API authentication (also accepts `INVOICE_API_KEYS` for comma-separated list)
- `RQ_QUEUE_NAME` — defaults to `invoice-jobs`
- `SENTRY_DSN` — optional, enables error tracking and OCR degradation alerts

See `.env.example` for the full list with defaults.

## Deployment
Deployed on Azure Container Apps (API + worker) with Azure Cache for Redis (Basic C0), Azure Blob Storage, and Azure OpenAI. See `azure_deployment_plan.md` for full infrastructure details and `deploy.sh` for the deployment script. The Dockerfile sets `PYTHONPATH=/app`.

Worker is configured with `min-replicas 0` and KEDA scaling on Redis queue length (1200s cooldown). It scales to zero when idle and wakes on first enqueued job.

### Two-tier deploys (prod + test)

Each product has a production app pair and a test app pair, sharing one Container Apps environment and one ACR image repo. Test is production's name with a `-test` suffix on everything *except* the image repo:

- **Production:** `ca-api-<product>` / `ca-worker-<product>`, queue `jobs-<product>`, domain `3c<product>.flex-capital-scale.com`, blob prefixes `uploads-<product>/` and `processed-<product>/`.
- **Test:** `ca-api-<product>-test` / `ca-worker-<product>-test`, queue `jobs-<product>-test`, domain `3c<product>-test.flex-capital-scale.com`, blob prefixes `uploads-<product>-test/` and `processed-<product>-test/`.
- **Image repo is not tiered** — both tiers pull `3cix-<product>` from ACR. That's what lets promotion re-point prod at the exact digest that ran on test instead of rebuilding.

`scripts/lib/tier.sh` (`resolve_tier_names <product> [tier]`, tier defaults to `prod`) is the single source of truth for all of the above; `deploy.sh`, `promote.sh` and `scripts/provision_product.sh` all source it, so they can't disagree.

**Workflow** — merge to `main` first, so what you promote is what you tested:

```bash
./deploy.sh bps v20260812a test              # build in ACR, point the -test pair at it
# ...verify against the test app/domain...
scripts/promote.sh bps v20260812a            # dry run: prints what would change
scripts/promote.sh bps v20260812a --apply    # re-points the production pair at that same image
```

`promote.sh` never builds — it only re-points production at an image already in ACR — and refuses to run unless: the tag isn't `latest`, the working tree is clean, you're on `main`, and the tag is the one currently deployed on the product's `-test` app. That last guard is the point: it makes "production only ever runs what test ran" a property of the tooling, not a habit — which is also why merging before deploying to test matters, otherwise the two could diverge.

Rollback: `scripts/promote.sh <product> <previous-tag> --apply --force-rollback` skips only the "must be on test" guard; the `latest`, dirty-tree and `main`-branch guards still apply.

**The test tier is not provisioned yet.** The tooling above is merged and works, but no `ca-api-*-test` / `ca-worker-*-test` app exists and no `*-test.flex-capital-scale.com` hostname resolves. Create a test pair first with `scripts/provision_product.sh <product> <tag> test` (`DRY_RUN=1` prints the resolved config without calling `az`) before the workflow above has anything to deploy to.

**Important:** `deploy.sh` defaults to the `latest` tag. Redeploying with the same tag won't create a new revision — use a unique tag like `./deploy.sh bps v20260812a` to force a new revision.

## Resilience

All external API calls (Mistral OCR, Azure Document Intelligence, Azure OpenAI) use tenacity retries: 3 attempts with exponential backoff (2s–30s). Retry attempts are logged via structlog.

- **DualOCR fallback:** If one OCR engine fails after retries, the pipeline continues with the other engine's output. A `sentry_sdk.capture_message` (level=warning) fires so you can alert on degradation even when jobs succeed. Only raises if both engines fail.
- **RQ job retry:** Jobs are enqueued with `Retry(max=2)` — if the entire pipeline fails, RQ re-enqueues up to 2 more times.
- **Retry helper:** `utils.log_retry` is the shared tenacity `before_sleep` callback. Retried functions: `ocr_mistral_v2._process_image`, `ocr_azure_docintel.extract_text`, `processors.azure_processor._call_openai`, `invoice._call_analyze_llm`.
