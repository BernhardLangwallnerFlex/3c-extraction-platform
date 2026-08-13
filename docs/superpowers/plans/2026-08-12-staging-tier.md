# Staging Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each of the three products a test API + worker pair so a change can be verified by 3C's developers against a real URL, then promoted to production as the identical image.

**Architecture:** A shared bash library derives every tier-dependent name and setting from `(product, tier)`. `provision_product.sh` and `deploy.sh` gain an optional `tier` argument that sources it; a new `promote.sh` re-points production at an image already running on test. Shell logic is tested from pytest by running the scripts against a stub `az` on `PATH` and a throwaway git repo.

**Tech Stack:** bash 3.2 (macOS default), Azure CLI (`az`), Azure Container Apps, pytest 9.x, RQ/Redis.

**Spec:** `docs/superpowers/specs/2026-08-12-staging-tier-design.md`

> **Post-implementation correction.** Tasks 2 and 3 below specify a dry-run env var named
> `DRY_RUN`. The final review found that a single generic name shared by two scripts is a
> production hazard: a value exported to preview a provision silently no-ops a subsequent
> `deploy.sh`, which exits 0 having built and deployed nothing. The shipped code therefore
> uses **`PROVISION_DRY_RUN`** and **`DEPLOY_DRY_RUN`**. Task 2's and Task 3's text below is
> left as the historical record; the operational commands in Task 6 use the correct names.

## Global Constraints

- Target bash 3.2 — macOS ships it. No `declare -A`, no `${var,,}`, no `mapfile`. The existing `provision_product.sh` already documents the bash-3.2-safe empty-array idiom `${arr[@]+"${arr[@]}"}`; keep using it.
- Resource group is `rg-3c-invoice`; ACA environment `cae-3c-invoice`; ACR `cr3cinvoice`. Never hardcode these in more than one place per script.
- Valid tiers are exactly `prod` and `test`. `prod` is the default everywhere, so every existing invocation keeps working unchanged.
- One image repo per product (`3cix-<product>`), one tag per build, shared by both tiers. **Never build a `-test` image variant.**
- Destructive or outward-facing scripts are dry-run by default and require `--apply`, matching `scripts/cutover_vcc_domain.sh` and `scripts/purge_blob_backlog.py`.
- Never pass `latest` as a deploy tag — redeploying the same tag does not create a new ACA revision.
- Tests live under `tests/`, run with `.venv/bin/python -m pytest tests/`. No linter is configured.
- Commit after every task. Work on a feature branch off `main`.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/lib/tier.sh` (create) | Sole source of truth for tier-dependent names and settings |
| `scripts/provision_product.sh` (modify) | Creates an app pair for a given product+tier |
| `deploy.sh` (modify) | Builds an image and points a product+tier's apps at it |
| `scripts/promote.sh` (create) | Re-points production at an image verified on test |
| `test_api.py` (modify) | End-to-end smoke test; make the test file env-overridable |
| `tests/scripts/test_tier_naming.py` (create) | Unit tests for the naming library |
| `tests/scripts/test_provision_config.py` (create) | Asserts tier-specific config differences |
| `tests/scripts/test_promote_guards.py` (create) | Asserts each `promote.sh` guard rejects |

---

### Task 1: Tier naming library

Every downstream script needs the same names. Deriving them in one place is what stops `deploy.sh` and `promote.sh` from disagreeing about what `ca-api-bps-test` is called.

**Files:**
- Create: `scripts/lib/tier.sh`
- Create: `tests/scripts/__init__.py` (empty)
- Test: `tests/scripts/test_tier_naming.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a sourceable shell function `resolve_tier_names <product> [tier]` that sets these variables in the caller's scope: `TIER`, `API_APP`, `WORKER_APP`, `QUEUE_NAME`, `IMAGE_REPO`, `AZURE_INPUT_PREFIX`, `AZURE_OUTPUT_PREFIX`, `API_HOSTNAME`, `SENTRY_ENVIRONMENT_VALUE`, `CLEANUP_ARTIFACTS_VALUE`, `API_MIN_REPLICAS`, `API_MAX_REPLICAS`, `WORKER_MIN_REPLICAS`, `WORKER_MAX_REPLICAS`. Returns 1 on an unknown tier.

- [ ] **Step 1: Write the failing test**

Create `tests/scripts/__init__.py` as an empty file, then `tests/scripts/test_tier_naming.py`:

```python
"""Unit tests for scripts/lib/tier.sh.

The library is pure name derivation with no az calls, so it can be sourced in a
bash subprocess and its exported variables read back as KEY=VALUE lines.
"""
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib" / "tier.sh"

_VARS = [
    "TIER", "API_APP", "WORKER_APP", "QUEUE_NAME", "IMAGE_REPO",
    "AZURE_INPUT_PREFIX", "AZURE_OUTPUT_PREFIX", "API_HOSTNAME",
    "SENTRY_ENVIRONMENT_VALUE", "CLEANUP_ARTIFACTS_VALUE",
    "API_MIN_REPLICAS", "API_MAX_REPLICAS",
    "WORKER_MIN_REPLICAS", "WORKER_MAX_REPLICAS",
]


def resolve(product, tier=None):
    """Source the library, call resolve_tier_names, return the vars as a dict."""
    call = f'resolve_tier_names "{product}"' + (f' "{tier}"' if tier else "")
    echoes = "\n".join(f'echo "{v}=${v}"' for v in _VARS)
    script = f'set -euo pipefail\nsource "{LIB}"\n{call}\n{echoes}\n'
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"tier.sh failed: {proc.stderr}")
    return dict(line.split("=", 1) for line in proc.stdout.strip().splitlines())


def test_prod_is_the_default_tier():
    assert resolve("bps")["TIER"] == "prod"


def test_prod_names_match_the_live_apps():
    r = resolve("bps", "prod")
    assert r["API_APP"] == "ca-api-bps"
    assert r["WORKER_APP"] == "ca-worker-bps"
    assert r["QUEUE_NAME"] == "jobs-bps"
    assert r["AZURE_INPUT_PREFIX"] == "az://invoices/uploads-bps/"
    assert r["AZURE_OUTPUT_PREFIX"] == "az://invoices/processed-bps/"
    assert r["API_HOSTNAME"] == "3cbps.flex-capital-scale.com"


def test_test_tier_suffixes_every_name():
    r = resolve("bps", "test")
    assert r["API_APP"] == "ca-api-bps-test"
    assert r["WORKER_APP"] == "ca-worker-bps-test"
    assert r["QUEUE_NAME"] == "jobs-bps-test"
    assert r["AZURE_INPUT_PREFIX"] == "az://invoices/uploads-bps-test/"
    assert r["AZURE_OUTPUT_PREFIX"] == "az://invoices/processed-bps-test/"
    assert r["API_HOSTNAME"] == "3cbps-test.flex-capital-scale.com"


def test_image_repo_is_shared_across_tiers():
    """One image serves both tiers — this is what makes promote-by-digest honest."""
    assert resolve("bps", "prod")["IMAGE_REPO"] == "3cix-bps"
    assert resolve("bps", "test")["IMAGE_REPO"] == "3cix-bps"


def test_tier_specific_settings():
    prod, test = resolve("bps", "prod"), resolve("bps", "test")
    assert prod["SENTRY_ENVIRONMENT_VALUE"] == "production"
    assert test["SENTRY_ENVIRONMENT_VALUE"] == "staging"
    assert prod["CLEANUP_ARTIFACTS_VALUE"] == "true"
    assert test["CLEANUP_ARTIFACTS_VALUE"] == "false"
    assert prod["API_MIN_REPLICAS"] == "1"
    assert test["API_MIN_REPLICAS"] == "0"
    assert prod["WORKER_MAX_REPLICAS"] == "5"
    assert test["WORKER_MAX_REPLICAS"] == "2"


def test_worker_always_scales_to_zero():
    for tier in ("prod", "test"):
        assert resolve("bps", tier)["WORKER_MIN_REPLICAS"] == "0"


@pytest.mark.parametrize("product", ["vetcostcheck", "bps", "sanierer"])
def test_all_three_products_resolve(product):
    r = resolve(product, "test")
    assert r["API_APP"] == f"ca-api-{product}-test"
    assert r["API_HOSTNAME"] == f"3c{product}-test.flex-capital-scale.com"


def test_unknown_tier_is_rejected():
    with pytest.raises(AssertionError, match="unknown tier"):
        resolve("bps", "staging")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/scripts/test_tier_naming.py -v`
Expected: every test FAILS — `tier.sh failed: ... No such file or directory`.

- [ ] **Step 3: Write the implementation**

Create `scripts/lib/tier.sh`:

```bash
#!/usr/bin/env bash
# Tier-dependent naming and configuration — the single source of truth.
#
# Sourced by provision_product.sh, deploy.sh and promote.sh so they can never
# disagree about what an app, queue or blob prefix is called.
#
# Usage:  source scripts/lib/tier.sh
#         resolve_tier_names <product> [tier]     # tier defaults to "prod"
#
# bash 3.2 compatible (macOS default): no associative arrays, no ${var,,}.

resolve_tier_names() {
  local product="${1:?resolve_tier_names: product required}"
  local tier="${2:-prod}"

  local suffix
  if [[ "$tier" == "prod" ]]; then
    suffix=""
  elif [[ "$tier" == "test" ]]; then
    suffix="-test"
  else
    echo "ERROR: unknown tier '${tier}' (expected 'prod' or 'test')" >&2
    return 1
  fi

  TIER="$tier"
  API_APP="ca-api-${product}${suffix}"
  WORKER_APP="ca-worker-${product}${suffix}"
  QUEUE_NAME="jobs-${product}${suffix}"
  AZURE_INPUT_PREFIX="az://invoices/uploads-${product}${suffix}/"
  AZURE_OUTPUT_PREFIX="az://invoices/processed-${product}${suffix}/"
  API_HOSTNAME="3c${product}${suffix}.flex-capital-scale.com"

  # Deliberately NOT suffixed: one image repo serves both tiers so that
  # promotion re-points prod at the exact digest that ran on test.
  IMAGE_REPO="3cix-${product}"

  # Worker scales to zero on both tiers; only its ceiling differs, capping how
  # much shared Azure OpenAI quota a test run can take from production.
  WORKER_MIN_REPLICAS=0

  if [[ "$tier" == "test" ]]; then
    SENTRY_ENVIRONMENT_VALUE="staging"
    CLEANUP_ARTIFACTS_VALUE="false"   # keep test artifacts inspectable
    API_MIN_REPLICAS=0                # near-free; costs a cold start
    API_MAX_REPLICAS=2
    WORKER_MAX_REPLICAS=2
  else
    SENTRY_ENVIRONMENT_VALUE="production"
    CLEANUP_ARTIFACTS_VALUE="true"
    API_MIN_REPLICAS=1
    API_MAX_REPLICAS=3
    WORKER_MAX_REPLICAS=5
  fi
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/scripts/test_tier_naming.py -v`
Expected: 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/tier.sh tests/scripts/__init__.py tests/scripts/test_tier_naming.py
git commit -m "feat: add tier naming library as the single source of truth

Derives app names, queue, blob prefixes, hostname and tier-specific scaling
from (product, tier) so provision/deploy/promote cannot disagree. The image
repo is deliberately NOT tier-suffixed: one image serves both tiers, which is
what makes promote-by-digest honest."
```

---

### Task 2: Tier support in `provision_product.sh`

**Files:**
- Modify: `scripts/provision_product.sh`
- Test: `tests/scripts/test_provision_config.py`

**Interfaces:**
- Consumes: `resolve_tier_names` from Task 1.
- Produces: `scripts/provision_product.sh <product> <image-tag> [tier]`. When `DRY_RUN=1` it prints the resolved configuration as `KEY=VALUE` lines and exits 0 without calling `az`.

- [ ] **Step 1: Write the failing test**

Create `tests/scripts/test_provision_config.py`:

```python
"""Tier-specific configuration emitted by provision_product.sh.

Runs the script with DRY_RUN=1, which prints resolved config and exits before
any az call. Required secrets are stubbed with dummy values because the script
validates their presence up front.
"""
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "provision_product.sh"

_STUB_ENV = {
    "DRY_RUN": "1",
    "REDIS_URL": "rediss://stub",
    "KEDA_REDIS_HOST": "stub:6380",
    "REDIS_PASSWORD": "stub",
    "AZURE_STORAGE_ACCOUNT_NAME": "stub",
    "AZURE_STORAGE_ACCOUNT_KEY": "stub",
    "AZURE_ENDPOINT": "https://stub/",
    "AZURE_OPENAI_KEY": "stub",
    "AZURE_OPENAI_API_VERSION": "2024-12-01-preview",
    "MISTRAL_API_KEY": "stub",
    "AZURE_DOCINTEL_ENDPOINT": "https://stub/",
    "AZURE_DOCINTEL_KEY": "stub",
    "INVOICE_API_KEY": "stub-key",
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
}


def provision(product, tag, tier=None):
    args = [str(SCRIPT), product, tag] + ([tier] if tier else [])
    proc = subprocess.run(args, capture_output=True, text=True, env=dict(_STUB_ENV))
    assert proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}"
    out = {}
    for line in proc.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_defaults_to_prod():
    r = provision("bps", "v1")
    assert r["API_APP"] == "ca-api-bps"
    assert r["SENTRY_ENVIRONMENT"] == "production"


def test_test_tier_config():
    r = provision("bps", "v1", "test")
    assert r["API_APP"] == "ca-api-bps-test"
    assert r["QUEUE_NAME"] == "jobs-bps-test"
    assert r["SENTRY_ENVIRONMENT"] == "staging"
    assert r["CLEANUP_ARTIFACTS"] == "false"
    assert r["API_MIN_REPLICAS"] == "0"


def test_sentry_environment_is_always_set():
    """Regression: it was previously unset, so the app defaulted to production."""
    for tier in (None, "prod", "test"):
        assert provision("bps", "v1", tier).get("SENTRY_ENVIRONMENT")


def test_image_is_the_same_repo_on_both_tiers():
    assert provision("bps", "v9", "prod")["IMAGE"].endswith("3cix-bps:v9")
    assert provision("bps", "v9", "test")["IMAGE"].endswith("3cix-bps:v9")


def test_rejects_unknown_tier():
    proc = subprocess.run(
        [str(SCRIPT), "bps", "v1", "staging"],
        capture_output=True, text=True, env=dict(_STUB_ENV),
    )
    assert proc.returncode != 0
    assert "unknown tier" in proc.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/scripts/test_provision_config.py -v`
Expected: FAIL — the script ignores the third argument and prints no `KEY=VALUE` config.

- [ ] **Step 3: Modify the script**

In `scripts/provision_product.sh`, replace the argument parsing and name-derivation block (everything from `PRODUCT="${1:?...}"` down to and including the `IMAGE_REPO=` / `AZURE_INPUT_PREFIX=` / `AZURE_OUTPUT_PREFIX=` assignments) with:

```bash
PRODUCT="${1:?Usage: scripts/provision_product.sh <product> <image-tag> [tier]}"
IMAGE_TAG="${2:?Usage: scripts/provision_product.sh <product> <image-tag> [tier]}"
TIER_ARG="${3:-prod}"

RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
ENV_NAME="cae-3c-invoice"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/tier.sh
source "${SCRIPT_DIR}/lib/tier.sh"
resolve_tier_names "$PRODUCT" "$TIER_ARG"
```

Then, immediately after the required-env validation block (`: "${AZURE_DOCINTEL_KEY:?}"`) and after `INVOICE_API_KEY` is defaulted, insert the dry-run escape hatch:

```bash
ACR_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv 2>/dev/null || echo "cr3cinvoice.azurecr.io")
IMAGE="${ACR_SERVER}/${IMAGE_REPO}:${IMAGE_TAG}"

# Dry run: print the resolved configuration and stop before mutating anything.
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "TIER=${TIER}"
  echo "API_APP=${API_APP}"
  echo "WORKER_APP=${WORKER_APP}"
  echo "QUEUE_NAME=${QUEUE_NAME}"
  echo "IMAGE=${IMAGE}"
  echo "API_HOSTNAME=${API_HOSTNAME}"
  echo "AZURE_INPUT_PREFIX=${AZURE_INPUT_PREFIX}"
  echo "AZURE_OUTPUT_PREFIX=${AZURE_OUTPUT_PREFIX}"
  echo "SENTRY_ENVIRONMENT=${SENTRY_ENVIRONMENT_VALUE}"
  echo "CLEANUP_ARTIFACTS=${CLEANUP_ARTIFACTS_VALUE}"
  echo "API_MIN_REPLICAS=${API_MIN_REPLICAS}"
  echo "API_MAX_REPLICAS=${API_MAX_REPLICAS}"
  echo "WORKER_MIN_REPLICAS=${WORKER_MIN_REPLICAS}"
  echo "WORKER_MAX_REPLICAS=${WORKER_MAX_REPLICAS}"
  exit 0
fi

ACR_PASS=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)
```

Delete the now-duplicated `ACR_SERVER=` / `ACR_PASS=` / `IMAGE=` lines further down.

In both `az containerapp create` calls, replace the hardcoded replica flags and add the two new env vars:

- API: `--min-replicas "$API_MIN_REPLICAS" --max-replicas "$API_MAX_REPLICAS"`
- Worker: `--min-replicas "$WORKER_MIN_REPLICAS" --max-replicas "$WORKER_MAX_REPLICAS"`
- Both `--env-vars` blocks gain:
  ```
    SENTRY_ENVIRONMENT="$SENTRY_ENVIRONMENT_VALUE" \
    CLEANUP_ARTIFACTS="$CLEANUP_ARTIFACTS_VALUE" \
  ```
- The KEDA scaler `az containerapp update` at the end: replace `--min-replicas 0 --max-replicas 5` with `--min-replicas "$WORKER_MIN_REPLICAS" --max-replicas "$WORKER_MAX_REPLICAS"`.

Finally update the closing hint to use the resolved hostname:

```bash
echo "Next: map ${API_HOSTNAME} to ${API_APP} (custom domain + managed cert)"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/scripts/test_provision_config.py -v`
Expected: 5 tests PASS.

- [ ] **Step 5: Verify no regression in the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 65 tests pass — 51 pre-existing, 9 from Task 1, 5 from this task.

- [ ] **Step 6: Commit**

```bash
git add scripts/provision_product.sh tests/scripts/test_provision_config.py
git commit -m "feat: tier argument for provision_product.sh

Adds an optional third arg (prod|test, default prod) and a DRY_RUN=1 mode that
prints resolved config without calling az. Also sets SENTRY_ENVIRONMENT
explicitly on both tiers — it was previously unset, so core/api/main.py
silently fell back to 'production'."
```

---

### Task 3: Tier support in `deploy.sh`

**Files:**
- Modify: `deploy.sh`
- Test: `tests/scripts/test_deploy_targets.py` (create)

**Interfaces:**
- Consumes: `resolve_tier_names` from Task 1.
- Produces: `./deploy.sh <product|all> [tag] [tier]`. With `DRY_RUN=1` it prints `BUILD=<image>` and one `TARGET=<app>` line per app, and makes no `az` calls.

- [ ] **Step 1: Write the failing test**

Create `tests/scripts/test_deploy_targets.py`:

```python
"""deploy.sh targets the right apps for a tier and always builds one image."""
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy.sh"


def deploy(*args):
    proc = subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True, text=True, cwd=REPO,
        env={"DRY_RUN": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}"
    builds = [l.split("=", 1)[1] for l in proc.stdout.splitlines() if l.startswith("BUILD=")]
    targets = [l.split("=", 1)[1] for l in proc.stdout.splitlines() if l.startswith("TARGET=")]
    return builds, targets


def test_prod_is_the_default():
    _, targets = deploy("bps", "v1")
    assert targets == ["ca-api-bps", "ca-worker-bps"]


def test_test_tier_targets_the_test_pair():
    _, targets = deploy("bps", "v1", "test")
    assert targets == ["ca-api-bps-test", "ca-worker-bps-test"]


def test_builds_one_untiered_image():
    builds, _ = deploy("bps", "v1", "test")
    assert len(builds) == 1
    assert builds[0].endswith("3cix-bps:v1")
    assert "-test" not in builds[0]


def test_all_covers_three_products_on_the_given_tier():
    _, targets = deploy("all", "v1", "test")
    assert targets == [
        "ca-api-vetcostcheck-test", "ca-worker-vetcostcheck-test",
        "ca-api-bps-test", "ca-worker-bps-test",
        "ca-api-sanierer-test", "ca-worker-sanierer-test",
    ]


def test_all_builds_one_image_per_product():
    builds, _ = deploy("all", "v1", "test")
    assert builds == [
        "cr3cinvoice.azurecr.io/3cix-vetcostcheck:v1",
        "cr3cinvoice.azurecr.io/3cix-bps:v1",
        "cr3cinvoice.azurecr.io/3cix-sanierer:v1",
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/scripts/test_deploy_targets.py -v`
Expected: FAIL — no `BUILD=`/`TARGET=` output.

- [ ] **Step 3: Modify `deploy.sh`**

Replace the header and `deploy_one` with:

```bash
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
```

Then update the dispatch block to pass the tier:

```bash
if [[ "$PRODUCT" == "all" ]]; then
  for p in "${PRODUCTS[@]}"; do
    if [[ -d "products/${p}" ]]; then
      deploy_one "$p" "$IMAGE_TAG" "$TIER_ARG"
    fi
  done
else
  deploy_one "$PRODUCT" "$IMAGE_TAG" "$TIER_ARG"
fi
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/scripts/test_deploy_targets.py -v`
Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add deploy.sh tests/scripts/test_deploy_targets.py
git commit -m "feat: tier argument for deploy.sh

Optional third arg (prod|test, default prod). Builds one untiered image repo
per product regardless of tier; only the target app names change."
```

---

### Task 4: `promote.sh` with guards

The load-bearing piece. Guard 1 is what turns "production only runs what test ran" from a habit into a property of the tooling.

**Files:**
- Create: `scripts/promote.sh`
- Test: `tests/scripts/test_promote_guards.py`

**Interfaces:**
- Consumes: `resolve_tier_names` from Task 1.
- Produces: `scripts/promote.sh <product> <tag> [--apply] [--force-rollback]`. Exits non-zero with a message on `stderr` when a guard rejects. Dry-run by default.

- [ ] **Step 1: Write the failing test**

Create `tests/scripts/test_promote_guards.py`:

```python
"""Guards on scripts/promote.sh.

`az` is stubbed by a script on PATH that reports a fixed image for the test
app, so the guards can be exercised without touching Azure. Git guards run
against a throwaway repo created per test.
"""
import os
import pathlib
import subprocess
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "promote.sh"

DEPLOYED_ON_TEST = "cr3cinvoice.azurecr.io/3cix-bps:v20260812a"


@pytest.fixture
def stub_az(tmp_path):
    """A fake `az` that echoes the image supposedly deployed on the test app."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    az = bin_dir / "az"
    az.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Only the image query is used by the guards.
        for arg in "$@"; do
          if [[ "$arg" == *"containers[0].image"* ]]; then
            echo "{DEPLOYED_ON_TEST}"
            exit 0
          fi
        done
        exit 0
    """))
    az.chmod(0o755)
    return bin_dir


@pytest.fixture
def git_repo(tmp_path):
    """A clean git repo on main, so git guards pass unless a test dirties it."""
    work = tmp_path / "work"
    work.mkdir()
    run = lambda *a: subprocess.run(a, cwd=work, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "Test")
    (work / "f.txt").write_text("hello\n")
    run("git", "add", "f.txt")
    run("git", "commit", "-q", "-m", "init")
    return work


def promote(git_repo, stub_az, *args):
    env = {
        "PATH": f"{stub_az}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(git_repo.parent),
    }
    return subprocess.run(
        [str(SCRIPT), *args], cwd=git_repo,
        capture_output=True, text=True, env=env,
    )


def test_dry_run_is_the_default(git_repo, stub_az):
    proc = promote(git_repo, stub_az, "bps", "v20260812a")
    assert proc.returncode == 0
    assert "DRY RUN" in proc.stdout
    assert "ca-api-bps" in proc.stdout


def test_rejects_a_tag_not_deployed_on_test(git_repo, stub_az):
    """The load-bearing guard: you cannot promote what was never tested."""
    proc = promote(git_repo, stub_az, "bps", "v20260812b")
    assert proc.returncode != 0
    assert "not the image currently on test" in proc.stderr


def test_rejects_latest(git_repo, stub_az):
    proc = promote(git_repo, stub_az, "bps", "latest")
    assert proc.returncode != 0
    assert "latest" in proc.stderr


def test_rejects_a_dirty_tree(git_repo, stub_az):
    (git_repo / "f.txt").write_text("modified\n")
    proc = promote(git_repo, stub_az, "bps", "v20260812a")
    assert proc.returncode != 0
    assert "uncommitted changes" in proc.stderr


def test_rejects_a_non_main_branch(git_repo, stub_az):
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=git_repo, check=True)
    proc = promote(git_repo, stub_az, "bps", "v20260812a")
    assert proc.returncode != 0
    assert "main" in proc.stderr


def test_force_rollback_skips_only_the_test_image_guard(git_repo, stub_az):
    """Rollback to an older tag is legitimate; the other guards still apply."""
    proc = promote(git_repo, stub_az, "bps", "v20260101a", "--force-rollback")
    assert proc.returncode == 0
    assert "DRY RUN" in proc.stdout

    (git_repo / "f.txt").write_text("dirty\n")
    proc = promote(git_repo, stub_az, "bps", "v20260101a", "--force-rollback")
    assert proc.returncode != 0
    assert "uncommitted changes" in proc.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/scripts/test_promote_guards.py -v`
Expected: all FAIL — `promote.sh` does not exist.

- [ ] **Step 3: Write the implementation**

Create `scripts/promote.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/promote.sh <product> <tag> [--apply] [--force-rollback]
#
# Re-points the production app pair at an image ALREADY IN ACR. Never builds:
# promotion must ship the exact digest that ran on test.
#
# Dry-run by default (matching cutover_vcc_domain.sh and purge_blob_backlog.py).
# Pass --apply to execute.
#
# --force-rollback skips ONLY the "tag must be what is on test" guard, so an
# older known-good tag can be restored. Every other guard still applies.

PRODUCT="${1:?Usage: scripts/promote.sh <product> <tag> [--apply] [--force-rollback]}"
TAG="${2:?Usage: scripts/promote.sh <product> <tag> [--apply] [--force-rollback]}"
shift 2

APPLY=0
FORCE_ROLLBACK=0
for arg in "$@"; do
  case "$arg" in
    --apply)          APPLY=1 ;;
    --force-rollback) FORCE_ROLLBACK=1 ;;
    *) echo "ERROR: unknown option '${arg}'" >&2; exit 1 ;;
  esac
done

RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/tier.sh
source "${SCRIPT_DIR}/lib/tier.sh"

# --- Guard: never promote "latest" ---------------------------------------
# Re-deploying an identical tag does not create a new ACA revision, so a
# "successful" promote would silently ship nothing.
if [[ "$TAG" == "latest" ]]; then
  echo "ERROR: refusing to promote the 'latest' tag — it does not create a new revision." >&2
  echo "       Use a unique tag, e.g. v$(date +%Y%m%d)a" >&2
  exit 1
fi

# --- Guard: clean tree ----------------------------------------------------
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  echo "ERROR: working tree has uncommitted changes. Commit or stash before promoting." >&2
  exit 1
fi

# --- Guard: on main -------------------------------------------------------
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [[ "$BRANCH" != "main" ]]; then
  echo "ERROR: on branch '${BRANCH}'. Promote from main only." >&2
  exit 1
fi

# --- Resolve both tiers ---------------------------------------------------
resolve_tier_names "$PRODUCT" "test"
TEST_API_APP="$API_APP"

resolve_tier_names "$PRODUCT" "prod"
PROD_API_APP="$API_APP"
PROD_WORKER_APP="$WORKER_APP"

ACR_SERVER="${ACR_NAME}.azurecr.io"
IMAGE="${ACR_SERVER}/${IMAGE_REPO}:${TAG}"

# --- Guard: the tag must be what is running on test -----------------------
# This is the guard that matters. It makes "production only ever runs what test
# ran" a property of the tooling rather than a habit.
if [[ "$FORCE_ROLLBACK" -eq 0 ]]; then
  DEPLOYED_ON_TEST="$(az containerapp show \
    --name "$TEST_API_APP" --resource-group "$RG" \
    --query "properties.template.containers[0].image" -o tsv 2>/dev/null || true)"

  if [[ -z "$DEPLOYED_ON_TEST" ]]; then
    echo "ERROR: could not read the image on ${TEST_API_APP}." >&2
    echo "       Does the test pair exist? Run scripts/provision_product.sh ${PRODUCT} ${TAG} test" >&2
    exit 1
  fi

  if [[ "$DEPLOYED_ON_TEST" != "$IMAGE" ]]; then
    echo "ERROR: ${IMAGE} is not the image currently on test." >&2
    echo "       ${TEST_API_APP} runs: ${DEPLOYED_ON_TEST}" >&2
    echo "       Deploy this tag to test first, or pass --force-rollback to restore a known-good tag." >&2
    exit 1
  fi
fi

# --- Execute --------------------------------------------------------------
if [[ "$APPLY" -eq 0 ]]; then
  echo "== DRY RUN — no changes made. Re-run with --apply to execute. =="
  echo "Would promote ${IMAGE} to:"
  echo "  ${PROD_API_APP}"
  echo "  ${PROD_WORKER_APP}"
  exit 0
fi

for app in "$PROD_API_APP" "$PROD_WORKER_APP"; do
  echo "==> Promoting ${app} to ${IMAGE}..."
  az containerapp update --name "$app" --resource-group "$RG" --image "$IMAGE"
done

echo ""
echo "==> Promoted ${PRODUCT} to ${TAG}. Active revisions:"
az containerapp revision list --name "$PROD_API_APP" --resource-group "$RG" -o table
echo ""
echo "Next: git tag prod-${PRODUCT}-${TAG} && git push --tags"
```

Make it executable: `chmod +x scripts/promote.sh`

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/scripts/test_promote_guards.py -v`
Expected: 6 tests PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/promote.sh tests/scripts/test_promote_guards.py
git commit -m "feat: add promote.sh — re-point prod at the image verified on test

Dry-run by default. The load-bearing guard reads the image currently deployed
on ca-api-<product>-test and refuses any other tag, so an untested tag cannot
reach production by mistake. Also refuses 'latest', a dirty tree, and a
non-main branch. --force-rollback skips only the test-image guard."
```

---

### Task 5: Make `test_api.py` usable against any tier

Smoke-testing a staging endpoint needs a configurable base URL and test file. `API_BASE` and `INVOICE_API_KEY` are already env-driven; `TEST_FILE` is hardcoded to a vet invoice, which is wrong for BPS and Sanierer.

**Files:**
- Modify: `test_api.py:15`

**Interfaces:**
- Consumes: nothing.
- Produces: `TEST_FILE` env var, defaulting to the current path.

- [ ] **Step 1: Make the change**

Replace line 15 of `test_api.py`:

```python
TEST_FILE = os.getenv("TEST_FILE", "3C_testdaten_pdf/testrechnung_01_bulldogge.pdf")
```

- [ ] **Step 2: Verify it still runs with defaults**

Run: `.venv/bin/python -c "import ast,sys; ast.parse(open('test_api.py').read()); print('parses OK')"`
Expected: `parses OK`

- [ ] **Step 3: Commit**

```bash
git add test_api.py
git commit -m "feat: make test_api.py TEST_FILE env-overridable

Smoke-testing the BPS and Sanierer endpoints needs a non-vet PDF. API_BASE and
INVOICE_API_KEY were already env-driven."
```

---

### Task 6: Provision the three test pairs

Operational. No new code — this exercises Task 2 against real Azure.

**Files:** none (infrastructure).

**Interfaces:**
- Consumes: `scripts/provision_product.sh` from Task 2.
- Produces: six Container Apps, three queues, three generated API keys.

- [ ] **Step 1: Confirm Azure auth and that the names are free**

```bash
az account show --query name -o tsv
az containerapp list -g rg-3c-invoice --query "[?ends_with(name,'-test')].name" -o tsv
```
Expected: an account name, and **no output** from the second command.

- [ ] **Step 1b: Build the image first — the plan originally missed this**

`provision_product.sh` creates the apps pointing at `3cix-<product>:<tag>`. That tag must
already exist in ACR, or the apps come up unable to pull. As first written, this plan's only
`deploy.sh` call was in Task 10, so Task 6 referenced an image nothing had built. Build all
three before provisioning — `deploy.sh` builds in ACR and skips the not-yet-existing apps by
design:

```bash
for p in vetcostcheck bps sanierer; do ./deploy.sh "$p" <tag> test; done
```

Confirm the tags landed before continuing:

```bash
for r in 3cix-vetcostcheck 3cix-bps 3cix-sanierer; do
  az acr repository show-tags -n cr3cinvoice --repository $r --orderby time_desc --top 3 -o tsv
done
```

- [ ] **Step 2: Dry-run each product first**

```bash
set -a; source .env; set +a
for p in vetcostcheck bps sanierer; do
  echo "--- $p ---"
  PROVISION_DRY_RUN=1 scripts/provision_product.sh "$p" v20260813a test
done
```
Expected: for each, `API_APP=ca-api-<p>-test`, `SENTRY_ENVIRONMENT=staging`, `CLEANUP_ARTIFACTS=false`, `API_MIN_REPLICAS=0`.

- [ ] **Step 2b: Derive the four values `.env` does not carry**

`.env` is a *local-dev* file. It does not contain `REDIS_URL`, `KEDA_REDIS_HOST` or
`REDIS_PASSWORD` — those are deployment-time values that have only ever lived in the shell
of whoever ran the original provisioning. `SENTRY_DSN` is also absent, and without it
`SENTRY_ENVIRONMENT=staging` is inert, because `sentry_sdk.init` only runs on a non-empty
DSN. Derive all four from Azure (the Redis pair mirrors `azure_deployment_plan.md` Phase 3;
the DSN mirrors production so staging errors land in the same project, tagged `staging`):

```bash
RG=rg-3c-invoice
RH=$(az redis show --name redis-3c-invoice-v2 -g $RG --query hostName -o tsv)
RK=$(az redis list-keys --name redis-3c-invoice-v2 -g $RG --query primaryKey -o tsv)
export REDIS_URL="rediss://:${RK}@${RH}:6380/0?ssl_cert_reqs=none"
export KEDA_REDIS_HOST="${RH}:6380"
export REDIS_PASSWORD="${RK}"
export SENTRY_DSN=$(az containerapp secret show -n ca-api-bps -g $RG --secret-name sentry-dsn --query value -o tsv)
```

Check each is non-empty before continuing. Print lengths, never values.

- [ ] **Step 3: Provision for real, capturing the generated API keys**

Run one at a time so a failure is easy to attribute. `INVOICE_API_KEY` must be **unset** so the script generates a distinct key per app — do not let the prod key leak into staging.

```bash
set -a; source .env; set +a
unset INVOICE_API_KEY
for p in vetcostcheck bps sanierer; do
  scripts/provision_product.sh "$p" v20260813a test | tee "/tmp/provision-${p}-test.log"
done
```
Expected: each run prints an API FQDN and an `INVOICE_API_KEY:` line. **Save those three keys to your password manager — they are not recoverable from the logs later.**

- [ ] **Step 4: Verify the apps exist with the right scaling**

```bash
az containerapp list -g rg-3c-invoice \
  --query "sort_by([?ends_with(name,'-test')].{name:name,min:properties.template.scale.minReplicas,max:properties.template.scale.maxReplicas}, &name)" -o table
```
Expected: six apps; `ca-api-*-test` at min 0 / max 2, `ca-worker-*-test` at min 0 / max 2.

- [ ] **Step 5: Verify the tier-specific env vars landed**

```bash
for app in ca-api-bps-test ca-worker-bps-test; do
  echo "--- $app ---"
  az containerapp show -n $app -g rg-3c-invoice \
    --query "properties.template.containers[0].env[?name=='SENTRY_ENVIRONMENT'||name=='CLEANUP_ARTIFACTS'||name=='RQ_QUEUE_NAME'].{n:name,v:value}" -o table
done
```
Expected: `SENTRY_ENVIRONMENT=staging`, `CLEANUP_ARTIFACTS=false`, `RQ_QUEUE_NAME=jobs-bps-test`.

- [ ] **Step 6: Commit the provisioning log reference**

No code changed. Record the outcome instead:

```bash
git commit --allow-empty -m "chore: provision three test app pairs

ca-{api,worker}-{vetcostcheck,bps,sanierer}-test in cae-3c-invoice, queues
jobs-<product>-test, API min-replicas 0. API keys stored in the password
manager, not in the repo."
```

---

### Task 7: DNS records and custom domain binding

**Files:** none (DNS + infrastructure).

**Interfaces:**
- Consumes: the six apps from Task 6.
- Produces: three HTTPS hostnames `3c<product>-test.flex-capital-scale.com`.

- [ ] **Step 1: Collect the values needed for DNS**

```bash
VERIFICATION_ID=$(az containerapp env show --name cae-3c-invoice -g rg-3c-invoice \
  --query "properties.customDomainConfiguration.customDomainVerificationId" -o tsv)
echo "TXT value (same for all three): $VERIFICATION_ID"

for p in vetcostcheck bps sanierer; do
  fqdn=$(az containerapp show -n "ca-api-${p}-test" -g rg-3c-invoice \
    --query "properties.configuration.ingress.fqdn" -o tsv)
  echo "CNAME 3c${p}-test  ->  $fqdn"
done
```

- [ ] **Step 2: MAINTAINER ACTION — add six DNS records**

At the DNS provider for `flex-capital-scale.com`, add for each of the three products:

| Type | Name | Value |
|---|---|---|
| TXT | `asuid.3c<product>-test` | the `VERIFICATION_ID` from Step 1 |
| CNAME | `3c<product>-test` | that product's FQDN from Step 1 |

**This step is manual and blocks Step 3.** Everything in Tasks 1–6 and 8–11 is independent of it.

- [ ] **Step 3: Wait for propagation and verify**

```bash
for p in vetcostcheck bps sanierer; do
  echo "--- $p ---"
  dig +short "asuid.3c${p}-test.flex-capital-scale.com" TXT
  dig +short "3c${p}-test.flex-capital-scale.com" CNAME
done
```
Expected: the verification ID and the ACA FQDN for each. Re-run until all six resolve.

- [ ] **Step 4: Bind hostnames and managed certificates**

```bash
for p in vetcostcheck bps sanierer; do
  host="3c${p}-test.flex-capital-scale.com"
  az containerapp hostname add --name "ca-api-${p}-test" -g rg-3c-invoice --hostname "$host"
  az containerapp hostname bind --name "ca-api-${p}-test" -g rg-3c-invoice \
    --hostname "$host" --environment cae-3c-invoice --validation-method CNAME
done
```

- [ ] **Step 5: Verify TLS on all three**

```bash
for p in vetcostcheck bps sanierer; do
  echo -n "3c${p}-test: "
  curl -s -o /dev/null -w "%{http_code}\n" "https://3c${p}-test.flex-capital-scale.com/healthz"
done
```
Expected: `200` for each. A first request after idle may take ~30-60s because staging APIs run at `min-replicas 0` — retry once before treating it as a failure.

- [ ] **Step 6: Commit**

```bash
git commit --allow-empty -m "chore: bind test hostnames with managed TLS

3c{vetcostcheck,bps,sanierer}-test.flex-capital-scale.com now serve /healthz."
```

---

### Task 8: End-to-end smoke test of the test tier

Proves the critical isolation property: a test job must never touch the production queue.

**Files:** none.

**Interfaces:**
- Consumes: Tasks 5, 6, 7.
- Produces: verified isolation between tiers.

- [ ] **Step 1: Record the production queue depth before starting**

```bash
set -a; source .env; set +a
python - <<'PY'
import os, redis
r = redis.from_url(os.environ["REDIS_URL"])
for q in ("jobs-bps", "jobs-bps-test"):
    print(q, r.llen(f"rq:queue:{q}"))
PY
```
Expected: two numbers. Note the `jobs-bps` value.

- [ ] **Step 2: Run a job against the BPS test endpoint**

Use the BPS test API key saved in Task 6 Step 3.

```bash
API_BASE="https://3cbps-test.flex-capital-scale.com" \
INVOICE_API_KEY="<bps test key>" \
TEST_FILE="bps_sanierer_input/BPS_Input/BPS_1.pdf" \
.venv/bin/python test_api.py
```
Expected: upload → job id → polls → `finished` with a result containing `number_of_subdocuments`.

- [ ] **Step 3: Verify the production queue never moved**

Re-run Step 1's snippet.
Expected: `jobs-bps` unchanged from Step 1. **If it moved, stop** — the test worker is consuming production jobs and `RQ_QUEUE_NAME` is wrong on one of the apps.

- [ ] **Step 4: Verify output landed under the test prefix**

```bash
az storage blob list --account-name 3cixstorage --container-name invoices \
  --prefix "processed-bps-test/" --num-results 5 \
  --account-key "$AZURE_STORAGE_ACCOUNT_KEY" --query "[].name" -o tsv
```
Expected: at least one `extracted_data_*.json`. Because `CLEANUP_ARTIFACTS=false` on test, the subdocument artifacts should still be present too.

- [ ] **Step 5: Repeat for vetcostcheck and sanierer**

```bash
API_BASE="https://3cvetcostcheck-test.flex-capital-scale.com" INVOICE_API_KEY="<vcc test key>" \
  TEST_FILE="3C_testdaten_pdf/testrechnung_01_bulldogge.pdf" .venv/bin/python test_api.py

API_BASE="https://3csanierer-test.flex-capital-scale.com" INVOICE_API_KEY="<sanierer test key>" \
  TEST_FILE="bps_sanierer_input/Sanierer_Input/LO_Rechnung.pdf" .venv/bin/python test_api.py
```
Expected: both finish with a populated result.

- [ ] **Step 6: Commit**

```bash
git commit --allow-empty -m "chore: verify test tier end to end

All three test endpoints process a job. Confirmed jobs-<product> production
queues do not move during a test run and output lands under processed-*-test/."
```

---

### Task 9: Declare `SENTRY_ENVIRONMENT` on the six production apps

Verified 2026-08-12: the variable is set on none of the existing apps, so `core/api/main.py` falls back to `"production"`. That happens to be right, but it is undeclared — and once staging exists, an unset value is genuinely ambiguous.

**Files:** none (configuration).

**Interfaces:**
- Consumes: nothing.
- Produces: `SENTRY_ENVIRONMENT=production` on all six production apps.

- [ ] **Step 1: Confirm it is still unset**

```bash
for p in vetcostcheck bps sanierer; do
  for k in api worker; do
    echo -n "ca-${k}-${p}: "
    az containerapp show -n "ca-${k}-${p}" -g rg-3c-invoice \
      --query "properties.template.containers[0].env[?name=='SENTRY_ENVIRONMENT'].value | [0]" -o tsv
    echo "(blank = unset)"
  done
done
```
Expected: blank for all six.

- [ ] **Step 2: Set it**

Each `az containerapp update` creates a new revision, which is a real deployment. Do the workers first so any surprise is contained to background processing.

```bash
for p in vetcostcheck bps sanierer; do
  for k in worker api; do
    echo "==> ca-${k}-${p}"
    az containerapp update -n "ca-${k}-${p}" -g rg-3c-invoice \
      --set-env-vars SENTRY_ENVIRONMENT=production
  done
done
```

- [ ] **Step 3: Verify all six**

Re-run Step 1's loop.
Expected: `production` for all six.

- [ ] **Step 4: Verify production still serves traffic**

```bash
for p in vetcostcheck bps sanierer; do
  echo -n "3c${p}: "
  curl -s -o /dev/null -w "%{http_code}\n" "https://3c${p}.flex-capital-scale.com/healthz"
done
```
Expected: `200` for all three.

- [ ] **Step 5: Commit**

```bash
git commit --allow-empty -m "chore: declare SENTRY_ENVIRONMENT=production on the six prod apps

It was unset, so core/api/main.py fell back to 'production' by default rather
than by intent. provision_product.sh now sets it explicitly for both tiers
(see the tier argument commit); this brings the existing apps in line."
```

---

### Task 10: Redeploy production `sanierer` through the new path

Verified 2026-08-12: `ca-api-sanierer` and `ca-worker-sanierer` still run `3cix-sanierer:v20260530a` — May's image, missing artifact cleanup and everything merged since. Fixing it via test-then-promote doubles as the first real exercise of the new workflow.

**Files:** none (deployment).

**Interfaces:**
- Consumes: Tasks 3, 4, 6, 7, 8.
- Produces: production `sanierer` running a current image.

- [ ] **Step 1: Confirm the stale image and that you are on a clean main**

```bash
az containerapp show -n ca-api-sanierer -g rg-3c-invoice \
  --query "properties.template.containers[0].image" -o tsv
git status --porcelain && git rev-parse --abbrev-ref HEAD
```
Expected: `...3cix-sanierer:v20260530a`; no porcelain output; branch `main`.

- [ ] **Step 2: Build and deploy to test**

```bash
./deploy.sh sanierer v20260813a test
```
Expected: an ACR build, then updates to `ca-api-sanierer-test` and `ca-worker-sanierer-test`.

- [ ] **Step 3: Smoke-test the new image on staging**

```bash
API_BASE="https://3csanierer-test.flex-capital-scale.com" \
INVOICE_API_KEY="<sanierer test key>" \
TEST_FILE="bps_sanierer_input/Sanierer_Input/AR26076770.pdf" \
.venv/bin/python test_api.py
```
Expected: `finished`, with line items in the result.

- [ ] **Step 4: Confirm the guard blocks the wrong tag**

```bash
scripts/promote.sh sanierer v20260530a
```
Expected: **exit non-zero**, "is not the image currently on test". This proves guard 1 works against real Azure.

- [ ] **Step 5: Dry-run the real promotion**

```bash
scripts/promote.sh sanierer v20260813a
```
Expected: `DRY RUN`, listing `ca-api-sanierer` and `ca-worker-sanierer`.

- [ ] **Step 6: Promote**

```bash
scripts/promote.sh sanierer v20260813a --apply
```
Expected: both apps updated; a revision table printed.

- [ ] **Step 7: Verify production**

```bash
az containerapp show -n ca-api-sanierer -g rg-3c-invoice \
  --query "properties.template.containers[0].image" -o tsv
curl -s -o /dev/null -w "%{http_code}\n" https://3csanierer.flex-capital-scale.com/healthz
```
Expected: `...3cix-sanierer:v20260813a` and `200`.

- [ ] **Step 8: Tag the release**

```bash
git tag prod-sanierer-v20260813a
git push --tags
```

- [ ] **Step 9: Commit**

```bash
git commit --allow-empty -m "chore: redeploy prod sanierer off May's image via test-then-promote

ca-{api,worker}-sanierer ran 3cix-sanierer:v20260530a since May, missing
artifact cleanup and every change since. Promoted v20260813a through the new
path; the guard correctly refused v20260530a as not-on-test."
```

---

### Task 11: Documentation

**Files:**
- Modify: `azure_deployment_plan.md` (Current State section)
- Modify: `vetcostcheck_api_doc.md`
- Modify: `CLAUDE.md` (Deployment section)

**Interfaces:**
- Consumes: everything above.
- Produces: docs that describe the two-tier topology.

- [ ] **Step 1: Update `azure_deployment_plan.md`**

In the Current State table, add the three test pairs and update the sanierer row to the promoted tag. Remove the `⚠️ Sanierer is running May's image` warning paragraph (Task 10 fixed it). Under **Deployment**, replace the one-line description with:

```markdown
**Deployment** is two-tier. `./deploy.sh <product|all> <tag> test` builds the image and
points the test pair at it; `scripts/promote.sh <product> <tag> --apply` then re-points the
production pair at that same image. Promotion never rebuilds, and refuses any tag that is
not the one currently running on test. Always pass a unique tag; `latest` won't create a new
revision. Spec: `docs/superpowers/specs/2026-08-12-staging-tier-design.md`.
```

- [ ] **Step 2: Update `vetcostcheck_api_doc.md`**

Add a section near the top documenting the test base URL:

```markdown
## Environments

| Environment | Base URL |
|---|---|
| Production | `https://3cvetcostcheck.flex-capital-scale.com` |
| Test | `https://3cvetcostcheck-test.flex-capital-scale.com` |

The test environment uses a **separate API key** and its own storage, so test uploads never
appear in production. It runs at `min-replicas 0`, so the first request after an idle period
may take up to a minute.
```

- [ ] **Step 3: Update `CLAUDE.md`**

Replace the `## Deployment` section's `deploy.sh` paragraph with:

```markdown
Deployment is two-tier. Each product has a production pair (`ca-api-<p>` / `ca-worker-<p>`,
domain `3c<p>.flex-capital-scale.com`) and a test pair (`ca-api-<p>-test` /
`ca-worker-<p>-test`, domain `3c<p>-test.flex-capital-scale.com`), with separate Redis queues
and blob prefixes.

    ./deploy.sh <product|all> <tag> test      # build + deploy to test
    scripts/promote.sh <product> <tag>        # dry run
    scripts/promote.sh <product> <tag> --apply

Promotion re-points production at the exact image that ran on test — it never rebuilds, and
refuses any tag not currently deployed on the test app. Merge to `main` before deploying to
test, so what you promote is what you tested.

**Important:** `deploy.sh` defaults to the `latest` tag. Redeploying with the same tag won't
create a new revision — use a unique tag like `./deploy.sh bps v20260812a test`.
```

- [ ] **Step 4: Verify no stale claims remain**

```bash
grep -n "v20260530a" azure_deployment_plan.md
grep -rn "deploy.sh <product|all> <tag>$" CLAUDE.md
```
Expected: no output from either.

- [ ] **Step 5: Run the full test suite one final time**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit and push**

```bash
git add azure_deployment_plan.md vetcostcheck_api_doc.md CLAUDE.md
git commit -m "docs: document the two-tier deploy topology

Adds the three test pairs to the Current State table, the test base URL to the
VCC API doc, and the deploy/promote workflow to CLAUDE.md. Drops the stale
'sanierer runs May's image' warning."
git push origin main
```

---

## Known risks for the operational tasks (6-10)

Surfaced by the final whole-branch review of the Tasks 1-5 tooling. None block, but read
before running anything against live Azure.

- **Nothing in the tooling has ever executed an `az` mutation.** Every test stubs `az` or
  short-circuits before the first real call. Task 6 is the first live execution — run the
  `PROVISION_DRY_RUN=1` preview and read it before applying, and expect the first
  `az containerapp create` to be where a real argument error would surface.
- **`promote.sh --apply` is not atomic.** It updates the API app, then the worker. If the
  first succeeds and the second fails, production is split across two images — API on the
  new contract, worker on the old. For a contract change like the subdocument returncode,
  that is exactly the mixed state that produces confusing customer-visible behaviour. Watch
  the second `az` call complete; if it does not, re-run rather than assuming.
- **The tooling guarantees prod runs the same image as test — not that test ran `main`.**
  `deploy.sh` has no clean-tree or branch guard, so an image built from a feature branch can
  sit on test and then be promoted from a clean `main`. Merging first is a habit, not a
  mechanism. Highest-risk moment is Task 10, the first real use of the path.
- **`promote.sh --force-rollback`'s error wording** distinguishes "tag missing" from "ACR
  unreachable" by matching `az` error text that was never validated against live ACR. Both
  paths refuse (fail-closed), so the safety property holds; only the message may mislead.
- **`promote.sh` compares tags, not digests.** ACA stores the tag in the prod template, so
  an overwritten tag in ACR could let a replica restart pull different bits. The unique-tag
  convention makes this safe in practice — do not reuse a tag.
- **Shared Azure OpenAI quota** is capped by `WORKER_MAX_REPLICAS=2` on test but not
  eliminated. A large test batch during business hours can slow production extraction.

## Deferred dependency — do not lose this

`CLEANUP_ARTIFACTS=false` on the test tier means staging artifacts are never deleted by the
application. The 14-day blob lifecycle rule that would expire them is **still unapplied** —
it is an outstanding item from `docs/superpowers/specs/2026-07-29-artifact-retention-design.md`
and `azure_deployment_plan.md`.

There is no task for it here because there is no rule to modify yet. When that rule is
applied, its prefix list must include `uploads-<product>-test/` and `processed-<product>-test/`
for all three products, or staging blobs accumulate indefinitely. Add this to the retention
work's scope rather than to this plan.

## Rollback

If a promotion turns out bad:

```bash
scripts/promote.sh <product> <previous-tag> --apply --force-rollback
```

`--force-rollback` skips only the "must be on test" guard; the `latest`, dirty-tree and
`main`-branch guards still apply. Production tags are recorded as git tags
(`prod-<product>-<tag>`) from Task 10 onward, so the previous known-good tag is discoverable
with `git tag -l 'prod-<product>-*'`.
