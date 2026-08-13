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
# older known-good tag can be restored. Every other guard still applies, and
# --force-rollback additionally requires the image to actually exist in ACR
# (checked via `az acr manifest show`), since the skipped guard is what would
# otherwise have caught a mistyped tag.

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
else
  # --force-rollback skips the "ran on test" guard above. Nothing else looks
  # at ACR, so without this check a typo'd tag would only surface once
  # `az containerapp update` runs, creating a production revision that can
  # never pull. Verify the image is a real, pullable manifest first.
  if MANIFEST_ERR="$(az acr manifest show -r "$ACR_NAME" -n "${IMAGE_REPO}:${TAG}" 2>&1 >/dev/null)"; then
    : # confirmed present in ACR — proceed
  else
    if echo "$MANIFEST_ERR" | grep -qiE "not found|unknown|does not exist|404"; then
      echo "ERROR: ${IMAGE_REPO}:${TAG} was not found in ACR (${ACR_NAME})." >&2
      echo "       Check that the tag is correct." >&2
    else
      echo "ERROR: could not verify ${IMAGE_REPO}:${TAG} exists in ACR (${ACR_NAME}) — the query itself failed." >&2
      echo "       This looks like an ACR/auth problem, not necessarily a bad tag — check 'az login' and retry." >&2
    fi
    echo "$MANIFEST_ERR" >&2
    echo "Refusing --force-rollback with an unverified image." >&2
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
