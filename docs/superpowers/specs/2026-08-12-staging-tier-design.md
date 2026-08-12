# Test/staging tier per product — design

**Date:** 2026-08-12
**Products:** vetcostcheck, bps, sanierer
**Goal:** deploy a change to a test endpoint, have 3C's developers verify it against a real
URL, and only then promote the *identical* image to production.

## Problem

Every change goes straight to production. `./deploy.sh <product> <tag>` builds an image and
re-points the live app pair at it; there is no intermediate stage where a contract change
can be validated by the consumer before real traffic hits it. The upcoming subdocument
returncode change (`2026-08-12-returncode-design.md`) alters the API contract for all three
products, which makes the gap concrete.

## Constraint: revision traffic-splitting does not work here

The cheapest-looking option — Azure Container Apps revision labels with weighted traffic —
must be rejected, and the reason is architectural rather than incidental.

The API would split correctly: ACA gives each revision its own FQDN and can weight traffic
between them. The **worker cannot**. It has no ingress; it pulls jobs from
`rq:queue:jobs-<product>` in Redis. Two active worker revisions both drain the *same* queue,
so a "staging" worker revision would consume real production jobs and process them with
untested code, non-deterministically.

Isolating the worker therefore requires a separate queue; a separate queue requires a
separate app with a different `RQ_QUEUE_NAME`. That is a second app pair, which is the design
below.

## Topology

> Verified against the live control plane 2026-08-12: the three production pairs exist as
> described, all three custom domains follow `3c<p>.flex-capital-scale.com`, no `-test` app
> names are taken, and `SENTRY_ENVIRONMENT` is set on none of the apps.

Six new Container Apps in the existing `cae-3c-invoice` environment. Shared: ACA
environment, `redis-3c-invoice-v2`, `cr3cinvoice`, `3cixstorage`, and the `3cinfoextraction`
Azure OpenAI deployment.

| | prod | test |
|---|---|---|
| Apps | `ca-api-<p>` / `ca-worker-<p>` | `ca-api-<p>-test` / `ca-worker-<p>-test` |
| Queue | `jobs-<p>` | `jobs-<p>-test` |
| Blob prefixes | `uploads-<p>/`, `processed-<p>/` | `uploads-<p>-test/`, `processed-<p>-test/` |
| Domain | `3c<p>.flex-capital-scale.com` | `3c<p>-test.flex-capital-scale.com` |
| `INVOICE_API_KEY` | prod key | distinct key |
| Image | `3cix-<p>:<tag>` | **the same** `3cix-<p>:<tag>` |

**One image repo per product, one tag per build, both tiers pull it.** There is deliberately
no `-test` image variant: promotion re-points production at the exact digest that ran on
test. Rebuilding at promotion time (fresh `pip install`, possibly a new base-image layer)
would put bits into production that were never tested, which defeats the tier's purpose.

Test APIs are 3C-facing and get custom domains with managed TLS certificates, exactly as
production does.

## Tier-specific configuration

Beyond names, four settings differ. Two of them are latent defects that the staging tier
would otherwise inherit:

| Setting | prod | test | Why |
|---|---|---|---|
| `SENTRY_ENVIRONMENT` | `production` | `staging` | `provision_product.sh` never sets this today, so `core/api/main.py` falls back to `production`. Unfixed, every staging error would land in the production Sentry stream. |
| `CLEANUP_ARTIFACTS` | `true` | `false` | Failed test runs stay inspectable. |
| API `min-replicas` / `max-replicas` | 1 / 3 | **0** / 2 | Staging will see very little traffic; scale-to-zero makes it near-free. Accepted trade-off: a cold start on the first request after idle. |
| Worker `max-replicas` | 5 | 2 | Caps how much shared Azure OpenAI TPM a test run can take from production. |

`CLEANUP_ARTIFACTS=false` creates a dependency: the 14-day lifecycle rule still pending from
`2026-07-29-artifact-retention-design.md` must also cover the `-test` blob prefixes, or
staging artifacts accumulate indefinitely.

## Script changes

### `scripts/provision_product.sh` — add a tier argument

`scripts/provision_product.sh <product> <image-tag> [tier]`, `tier` defaulting to `prod`.
The script already derives app names, queue name, image repo and blob prefixes from
`$PRODUCT` alone, so this is a suffix plus the tier-specific settings table above. It stays
idempotent (exits when both apps already exist).

### `deploy.sh` — add a tier argument

`./deploy.sh <product|all> [tag] [tier]`, `tier` defaulting to `prod`. It still builds
`3cix-<product>:<tag>`; only the target app names change. Existing invocations keep working
unchanged.

### `scripts/promote.sh` — new

`scripts/promote.sh <product> <tag> [--apply]`. Re-points the production app pair at an
image already in ACR. **Dry-run by default**, matching the convention set by
`scripts/cutover_vcc_domain.sh` and `scripts/purge_blob_backlog.py`.

Guards, in order of importance:

1. **The tag must be the image currently deployed on `ca-api-<product>-test`.** Read it from
   the live app and refuse anything else. This mechanically enforces "production only ever
   runs what test ran" — an untested tag cannot be promoted by mistake.
2. Refuse the `latest` tag. Re-deploying the same tag does not create a new revision, so
   `latest` silently no-ops.
3. Refuse a dirty working tree.
4. Refuse a branch other than `main`.

Promotion performs no build. It is `az containerapp update --image <same image>` against
`ca-api-<product>` and `ca-worker-<product>`.

**Rollback** is `scripts/promote.sh <product> <previous-tag> --apply`. Note that guard 1
blocks this when the previous tag is no longer what is on test; rollback therefore takes an
explicit override flag (`--force-rollback`) which skips guard 1 only.

## Branch workflow

```
git checkout -b feat/<name>          # work
git checkout main && git merge …     # merge first
./deploy.sh bps v20260812a test      # deploy main to test
                                     # 3C verifies against 3cbps-test.…
./scripts/promote.sh bps v20260812a --apply
git tag prod-bps-v20260812a && git push --tags
```

**Merge to `main` before deploying to test**, not after. Deploying a feature branch to test
and merging afterwards means promoting a merge result that was never tested, whenever `main`
moved in between. Merging first keeps "test ran exactly what `main` is". `main` may briefly
contain unverified code; that is safe, because production only advances by explicit
promotion.

Git tags for production releases start here — the repo currently has zero tags, so there is
no record of what shipped when.

## Cost

The staging tier adds effectively no standing cost. Staging workers scale to zero (free when
idle) and staging APIs now do too. Against the ~€103/mo total from `costing.md`, the increment
is the occasional cold-start compute plus test-run LLM tokens.

Separately, the idle legacy `ca-invoice-api` is still at `minReplicas 1`, costing roughly
€11/mo (costing.md's per-API figure) to do nothing. Its worker is already at `minReplicas 0`
and costs nothing. Retiring the API more than covers the test tier.

## Commissioning sequence

1. Refresh `az login` (currently MFA-expired).
2. Extend `provision_product.sh` with tier support.
3. Provision three test pairs from `.env`.
4. **Manual, by the maintainer:** add 3× TXT (`asuid.3c<p>-test`) and 3× CNAME records at the
   DNS provider for `flex-capital-scale.com`.
5. Bind the three hostnames and managed TLS certificates.
6. Smoke-test each test API end to end with `test_api.py`.
7. Add `scripts/promote.sh`; extend `deploy.sh` with the tier argument.
8. Update `azure_deployment_plan.md` (Current State table), `vetcostcheck_api_doc.md` (test
   base URL), and `CLAUDE.md` (deployment section).

Steps 2–3 and 7 are independent of steps 4–5, which block on DNS propagation.

## Verification

- Each test API answers `/healthz` on its custom domain with a valid certificate.
- A job submitted to a test API is processed by `ca-worker-<p>-test` and **not** by the
  production worker — confirm by checking that `rq:queue:jobs-<p>` never grows during a test
  run, and that output lands under `processed-<p>-test/`.
- A staging error appears in Sentry tagged `staging`, not `production`.
- `promote.sh` refuses: a tag not on test, `latest`, a dirty tree, and a non-`main` branch.
- `promote.sh --apply` produces a new production revision whose image digest is identical to
  the one on test.

## Risks

- **Shared Azure OpenAI quota.** A heavy test run can consume TPM that production needs. The
  `max-replicas 2` cap on staging workers limits but does not eliminate this. If it bites,
  the fix is a dedicated staging model deployment.
- **Shared Redis.** Separate queue names give logical isolation, but a runaway test could
  fill the Basic C0 (250 MB) instance. Low risk at these volumes.
- **Cold start on staging APIs.** `min-replicas 0` means 3C's first request after an idle
  period is slow. Accepted deliberately; revisit if testers report timeouts.

## Out of scope

- GitHub Actions or any CI. Scripts first; automation is a separate follow-up.
- A separate staging resource group, ACA environment, or Redis instance.
- A dedicated staging Azure OpenAI deployment (listed above as the mitigation if quota
  contention materialises).
- Retiring the legacy `ca-invoice-*` pair. Pre-existing, tracked in
  `azure_deployment_plan.md`, and independent of this work.

## Included pre-existing fixes

Two live defects are folded into this work because the tier changes touch exactly the code
and apps that carry them.

**Redeploy production `sanierer`.** Verified 2026-08-12: both `ca-api-sanierer` and
`ca-worker-sanierer` still run `3cix-sanierer:v20260530a` — May's image. It therefore lacks
artifact cleanup and every change merged since. It must be redeployed through the new
test-then-promote path, which doubles as the first real exercise of that path.

**Set `SENTRY_ENVIRONMENT` on the six existing apps.** Verified 2026-08-12: the variable is
set on none of them, so `core/api/main.py` falls back to `production`. That is coincidentally
correct for production today, but it is unset rather than intended, and the fix belongs in
`provision_product.sh` for both tiers. Set it explicitly to `production` on the existing six
so the value is declared rather than defaulted.
