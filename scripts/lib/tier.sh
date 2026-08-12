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
