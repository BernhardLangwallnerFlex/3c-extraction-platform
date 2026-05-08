# Multi-Product Extraction Platform — Design

**Date**: 2026-05-07
**Author**: Bernhard Langwallner
**Status**: Approved (pending review of this spec)

## Context

The current repository implements a veterinary invoice extraction pipeline ("vetcostcheck") deployed as one API + one worker on Azure Container Apps. The pipeline is intentionally generic in shape — OCR → analyze/split → per-subdocument extraction — with vet-specific behavior concentrated in prompts and one entity-association field.

We want to extend the system to handle two additional document types in the same insurance domain (Hausrat / Wohngebäude): **BPS** and **Sanierer**. Each new product must run as its own pair of Container Apps with no operational dependencies on the others, but should reuse as much of the existing code as practical without going through pip-package publication.

The variance per product is narrow: the extraction prompt + output JSON schema definitely vary; the analysis/splitting prompt and per-subdocument entity association *may* vary (to be confirmed against real data later). Everything else — OCR engines, storage, queue mechanics, API surface, splitting logic, deployment scaffolding — is shared.

## Decision

**Adopt a monorepo layout with a shared `core/` library and per-product subdirectories under `products/`. One parameterized Dockerfile produces three product-specific images. Each product deploys to its own pair of Azure Container Apps within the existing shared environment.**

Two approaches were considered and rejected:

- *Single image with `PRODUCT` env var* — maximally shared but the resulting image bundles all products' code, weakening operational independence and growing with each new product.
- *Separate repos linked by git submodule* — strongest isolation but disproportionate ceremony for a single-developer team; loses the ability to refactor `core/` and a product callsite in the same PR.

The chosen approach gives image-level operational independence (the requirement) while keeping `core/` editable as a normal Python package and avoiding submodule overhead.

## Repo Layout

The repository is renamed from its current vet-specific name to `3c-extraction-platform` (or similar product-neutral name) via `gh repo rename`, preserving full git history.

```
information_extraction/                   (working dir name unchanged; repo renamed on GitHub)
├── core/                                 # the shared "framework"
│   ├── api/                              # FastAPI app factory + routes
│   ├── jobs/                             # RQ worker entry, tasks.py
│   ├── ocr/                              # DualOCR + engines
│   ├── storage/                          # backends + file_storage
│   ├── processors/                       # Azure/GPT processors
│   ├── prompt_building/                  # generic prompt assembly helpers
│   ├── pipeline.py                       # was invoice.py, renamed; takes a ProductConfig
│   ├── product.py                        # ProductConfig dataclass/protocol
│   ├── config.py
│   ├── utils.py
│   └── __init__.py
├── products/
│   ├── vetcostcheck/
│   │   ├── product.py                    # exports CONFIG: ProductConfig
│   │   ├── extract_prompt.py             # German vet extraction prompt
│   │   ├── extract_schema.json
│   │   ├── analyze_overrides.py          # invoice_animals etc.
│   │   └── __init__.py
│   ├── bps/
│   │   ├── product.py
│   │   ├── prompts/                      # populated when domain work begins
│   │   └── __init__.py
│   └── sanierer/
│       └── (mirror of bps)
├── Dockerfile                            # ONE generic Dockerfile, takes PRODUCT build-arg
├── deploy.sh                             # ./deploy.sh <product> [tag]; supports "all"
├── docker-compose.yml                    # local dev — driven by PRODUCT env var
├── scripts/
│   ├── regression_check.py               # Phase 1 migration gate
│   └── provision_product.sh              # one-time per-product Azure provisioning
├── tests/
│   ├── core/                             # tests against core that don't need a product
│   └── products/                         # per-product smoke tests
└── docs/
    └── superpowers/
        └── specs/
            └── 2026-05-07-multi-product-extraction-design.md   (this file)
```

**Module boundaries**

- `core/` knows nothing about veterinary, BPS, or Sanierer. It only knows the `ProductConfig` type. All vet-specific behavior currently in `prompt_building/prompt_building.py` (the hardcoded German extraction prompt and the `invoice_animals` analyze schema field) moves into `products/vetcostcheck/`.
- `products/<name>/` is small by design — a `ProductConfig` instance, prompt files, optional analyze overrides. No imports from other products, ever.
- A product never edits `core/`. If it needs something `core/` doesn't expose, the missing capability is added to `core/` as a generic extension point.

## ProductConfig Interface

```python
# core/product.py
from dataclasses import dataclass
from typing import Callable

@dataclass
class ProductConfig:
    name: str                                       # "vetcostcheck" | "bps" | "sanierer"

    # Extraction stage — REQUIRED, varies per product
    extract_prompt_builder: Callable[..., str]      # signature TBD in implementation plan
    extract_output_schema: dict                     # JSON schema for the extracted record

    # Analysis/splitting stage — OPTIONAL, falls back to core defaults
    analyze_prompt_builder: Callable[..., str] | None = None
    analyze_output_schema: dict | None = None
```

- `core/` ships a generic analyze prompt + schema (page-range boundaries by document-number-like signals, no entity association). Each product can override later when real data informs the call.
- The vet `ProductConfig` migrates the existing hardcoded extraction prompt and `invoice_animals` analyze override into `products/vetcostcheck/`. Today's behavior is preserved bit-for-bit.
- Schemas live as JSON files alongside prompts (`products/vetcostcheck/extract_schema.json`). The config-driven extraction path becomes the only path; the legacy `get_full_prompt()` / `build_prompt_from_config()` split in `prompt_building/` is retired (closes one TODO.md item).
- Each product's entrypoint imports its own `CONFIG` and hands it to `core`. The Dockerfile only `COPY`s one product directory, so the running image contains exactly one product. A `PRODUCT_NAME` env var is set by the Container App (matches the directory name) and read by `core` to load `products.<name>.product:CONFIG`.

A new product is approximately 30 lines of Python plus prompt and schema files.

## Naming Conventions

| Concern | Pattern | Examples |
|---|---|---|
| Python package dir | lowercase ASCII | `products/vetcostcheck/`, `products/bps/`, `products/sanierer/` |
| `ProductConfig.name` | matches dir | `"vetcostcheck"`, `"bps"`, `"sanierer"` |
| ACR repo | `3cix-<product>` | `3cix-vetcostcheck:v20260507` |
| Container App (API) | `ca-api-<product>` | `ca-api-vetcostcheck` |
| Container App (worker) | `ca-worker-<product>` | `ca-worker-vetcostcheck` |
| RQ queue | `jobs-<product>` | `jobs-vetcostcheck`, `jobs-bps`, `jobs-sanierer` |
| Storage prefix | `<product>/` | `vetcostcheck/<file_id>/...` |
| Public domain | `3c<product>.flex-capital-scale.com` | `3cvetcostcheck...`, `3cbps...`, `3csanierer...` |
| Sentry project | `3cix-<product>` | one project per product |

The existing `ca-invoice-api` and `ca-invoice-worker` are renamed implicitly through Phase 2 cutover (Container Apps cannot be renamed in place).

## Deployment Topology

```
cae-3c-invoice (existing Container Apps environment, name retained as cosmetic wart)
├── ca-api-vetcostcheck      ← 3cvetcostcheck.flex-capital-scale.com
├── ca-worker-vetcostcheck   ← KEDA scaler on jobs-vetcostcheck queue
├── ca-api-bps               ← 3cbps.flex-capital-scale.com
├── ca-worker-bps            ← KEDA scaler on jobs-bps queue
├── ca-api-sanierer          ← 3csanierer.flex-capital-scale.com
└── ca-worker-sanierer       ← KEDA scaler on jobs-sanierer queue

acr3cinfoextraction (existing ACR)
├── 3cix-vetcostcheck:<tag>
├── 3cix-bps:<tag>
└── 3cix-sanierer:<tag>

redis-3c-invoice-v2 (existing, Basic C0) — SHARED
   queues: jobs-vetcostcheck, jobs-bps, jobs-sanierer (RQ key namespacing keeps them isolated)

3cixstorage (existing Azure Blob, single account/container) — SHARED
   prefixes: vetcostcheck/<file_id>/..., bps/..., sanierer/...

Sentry: separate project per product (lazy creation as each product comes online)
```

### Failure isolation in a shared Container Apps Environment

Apps in a shared Container Apps Environment are isolated for the failure modes that matter:

| Layer | Shared? | Blast radius |
|---|---|---|
| Per-app pods/processes | No | A crash in `ca-api-vetcostcheck` only restarts its own pods. |
| Memory/CPU per pod | No | OOM kills only the affected pod. |
| Revisions / deploys | No | A broken bps image does not touch vetcostcheck. |
| Log Analytics workspace | Yes — but logs are tagged by app | Apps don't fail together; logs co-mingle. |
| Environment-level scaling quota | Yes | The one real shared-fate risk: simultaneous heavy load across all three could compete for environment headroom. |
| Microsoft regional incidents | Yes — but separate environments in the same region wouldn't mitigate this anyway | |

The environment-level quota risk is mitigated by setting per-app `max-replicas` caps. Separate environments would triple ops overhead (three environment configs, three Log Analytics workspaces, three KEDA setups) for marginal isolation gain. We accept the shared environment.

### Per-product environment variables

Same set of variables, different values per Container App:

- `PRODUCT_NAME` — identifies which `products.<name>.product:CONFIG` to load; also drives storage prefix and queue name where not set explicitly
- `RQ_QUEUE_NAME` — `jobs-vetcostcheck` etc.
- `INVOICE_API_KEY` — distinct value per product (env var rename to `API_KEY` deferred as cosmetic)
- `SENTRY_DSN` — distinct project per product

Shared infra credentials (Azure OpenAI, Mistral, Doc Intel, Storage account, Redis URL) are identical across all six apps.

### `deploy.sh` rewrite

```bash
./deploy.sh vetcostcheck v20260507    # one product
./deploy.sh bps v20260507
./deploy.sh sanierer v20260507
./deploy.sh all v20260507             # all three, sequenced
```

Each invocation: `az acr build --build-arg PRODUCT=$1 -t 3cix-$1:$2 .` then `az containerapp update --image ...` for both that product's api and worker apps.

## Migration Sequencing

The existing vetcostcheck system must keep running through every step.

### Phase 0 — Repo rename + skeleton *(metadata-only, ~30 min)*

- `gh repo rename 3c-extraction-platform` (or chosen name)
- Update README to reflect multi-product scope
- Create empty `core/` and `products/vetcostcheck/` directories with placeholder `__init__.py` files
- No behavior change; existing pipeline keeps shipping from the same Dockerfile

### Phase 1 — Refactor into `core/` + `products/vetcostcheck/` *(behavior-preserving, the risky phase)*

Done as a thin slice first to derisk the layout, then expanded:

1. **Thin slice**: move `utils.py` and `config.py` into `core/`. Update imports. Build, deploy as `3cix-vetcostcheck:slice` to a throwaway Container App, run `test_api.py`. Confirm parity.
2. **Full move**: relocate `api/`, `jobs/`, `ocr/`, `storage/`, `processors/`, `prompt_building/` (the generic helpers), and `invoice.py` → `core/pipeline.py` into `core/`. Extract the hardcoded German vet extraction prompt and `invoice_animals` analyze field into `products/vetcostcheck/`. Define `ProductConfig` in `core/product.py`. Wire entrypoints to read `PRODUCT_NAME`.
3. **Dockerfile + deploy.sh**: rewrite to be product-parameterized.

**Validation gate**: `scripts/regression_check.py` runs extraction against 3–5 fixed test PDFs and diffs the JSON output against pinned reference files. Output must match. If it doesn't, the refactor introduced a bug — fix before proceeding.

### Phase 2 — Cutover vetcostcheck to new Container Apps *(zero-downtime via drain)*

1. Provision new apps via `scripts/provision_product.sh vetcostcheck` (creates `ca-api-vetcostcheck` and `ca-worker-vetcostcheck` with KEDA scaler, ingress, secrets, env vars). They run alongside the existing `ca-invoice-api` / `ca-invoice-worker` and read from `jobs-vetcostcheck`.
2. Map `3cvetcostcheck.flex-capital-scale.com` to the new API. New jobs flow into `jobs-vetcostcheck`.
3. Old worker drains `invoice-jobs`. Once empty (and after a 24–48h soak watching Sentry), delete `ca-invoice-api` and `ca-invoice-worker`.

### Phase 3 — Add `bps` *(template-following)*

Create `products/bps/` with stub prompt + schema. Iterate the prompt against real documents (separate domain work, not in this plan). When ready: `scripts/provision_product.sh bps` to create `ca-api-bps` and `ca-worker-bps`, then `./deploy.sh bps v1` to push the first image. Map `3cbps.flex-capital-scale.com`.

### Phase 4 — Add `sanierer` *(mirror Phase 3)*

Same pattern as Phase 3 (`scripts/provision_product.sh sanierer` then `./deploy.sh sanierer v1`), mapped to `3csanierer.flex-capital-scale.com`.

### Risk notes

- Phase 1 is the only phase that can break vetcostcheck. The thin-slice approach + JSON diff gate is the main safety net.
- Phases 3 and 4 are additive — they cannot affect vetcostcheck or each other.
- Repo rename auto-redirects old clone URLs, but local working directories will need a `git remote set-url origin <new>`.

## Testing

Deliberately minimal, matching the project's current style. No wholesale test-suite introduction.

- **Phase 1 regression gate** (`scripts/regression_check.py`) — runs extraction on 3–5 fixed PDFs and diffs JSON output against pinned references. Run before/after the refactor; output must match. Throwaway scaffolding for the migration; retire when no longer useful.
- **Per-product smoke test** (`tests/products/<name>/test_smoke.py`) — one end-to-end test per product that uploads a representative PDF and asserts the extracted JSON matches the product's schema. Required when adding `bps` and `sanierer`.

## Local Development

`docker-compose.yml` becomes product-aware via a single env var:

```bash
PRODUCT=vetcostcheck docker compose up --build
PRODUCT=bps docker compose up --build
```

The native-mode flow described in CLAUDE.md gets a `PRODUCT_NAME=vetcostcheck` prefix on the API and worker commands. One product at a time per local stack — matches how development actually happens.

## Observability

- **Logs**: Container Apps tags by app name automatically; no extra work.
- **Sentry**: one project per product (`3cix-vetcostcheck`, `3cix-bps`, `3cix-sanierer`). Created lazily as each product comes online. Per-product DSN set on each Container App.
- **KEDA**: one scaler config per worker, pointed at its own queue.

## Open Questions / TBDs

These do not block the plan but are noted for follow-up:

- **TLS / DNS for new domains**: same Azure-managed-cert pattern as the existing vet domain; provision when each product comes online.
- **`INVOICE_API_KEY` → `API_KEY` env var rename**: cosmetic; defer.
- **`cae-3c-invoice` environment rename**: requires recreating all apps in a new environment; defer indefinitely.
- **Sequencing vs. the Redis resilience TODOs** (separate plan, `redis_resilience_plan.md`): independent. Recommend doing the multi-product refactor first since the Redis items are non-urgent.
- **Provisioning approach** *(decided)*: today's setup uses ad-hoc `az` CLI commands documented in `azure_deployment_plan.md`; `deploy.sh` only handles image updates. Multi-product introduces `scripts/provision_product.sh <product>` to capture the per-product API + worker + KEDA + ingress + secrets setup as a reusable script. Not proper IaC, but a clear stepping stone.
- **Migrate provisioning to Bicep** *(deferred follow-up)*: once the multi-product structure has settled, replace `provision_product.sh` with parameterized Bicep modules. Cleaner state management, idempotent applies, drift detection. Out of scope for this work.

## YAGNI Exclusions

Deliberately not designing for, until a concrete need appears:

- Cross-product features (e.g. tying a customer's bps + sanierer claims together)
- Multi-tenant within a product (multiple customers per product with separate keys)
- API versioning
- Shared customer/auth model across products
- Auto-discovery of products at runtime (the explicit `PRODUCT_NAME` env var is enough)

## Next Step

After this spec is reviewed and approved, transition to the `superpowers:writing-plans` skill to produce the detailed implementation plan covering Phases 0–4.
