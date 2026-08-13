#!/usr/bin/env bash
# Shared helper for re-pointing a Container App at an image.
#
# Sourced by deploy.sh and scripts/promote.sh. Both update a product's API and
# worker as two separate `az` calls, so both have the same two hazards:
#
#   1. Azure intermittently answers a containerapp update with a transient 5xx
#      ("Service Unavailable"), sometimes AFTER the update has already been
#      applied. On 2026-08-13 one such blip aborted a test deploy midway.
#   2. When an update does fail, `set -e` aborts between the two calls and
#      leaves the pair split across two images. An API serving new code while
#      its worker runs old code produces results that look wrong rather than
#      absent — the worst kind of failure to leave undiagnosed.
#
# Retrying is safe: `az containerapp update --image` is idempotent, and
# re-applying the image an app already runs is a no-op.

# Newline-separated, not a bash array: bash 3.2 (the macOS default) errors on
# empty array expansion under `set -u`.
UPDATED_APPS=""

# What the caller calls the thing that ends up split. promote.sh overrides this
# with "PRODUCTION" — the same message should read louder there.
UPDATE_SUBJECT="${UPDATE_SUBJECT:-the tier}"

AZ_UPDATE_MAX_ATTEMPTS="${AZ_UPDATE_MAX_ATTEMPTS:-3}"

report_partial_update() {
  local status=$?
  # Not `[[ ... ]] && return` — a false test yields status 1, which `set -e`
  # would treat as a failure inside the trap itself.
  if [[ "$status" == "0" ]]; then
    return 0
  fi

  echo "" >&2
  echo "==> FAILED (exit ${status})." >&2
  if [[ -n "$UPDATED_APPS" ]]; then
    echo "==> These apps DID receive the new image before the failure:" >&2
    # Unquoted on purpose: split the newline-separated list. App names never
    # contain whitespace.
    printf '      %s\n' $UPDATED_APPS >&2
    echo "==> Everything else still runs its previous image — ${UPDATE_SUBJECT} IS SPLIT." >&2
    echo "==> Re-run the same command to finish. Updating an app to the image it" >&2
    echo "==> already runs is a no-op, so re-running is safe." >&2
  else
    echo "==> No app was updated." >&2
  fi
  return "$status"
}

install_partial_update_trap() {
  trap report_partial_update EXIT
}

# update_app_with_retry <app> <image> <resource-group>
update_app_with_retry() {
  local app="$1"
  local image="$2"
  local rg="$3"
  local attempt

  for (( attempt = 1; attempt <= AZ_UPDATE_MAX_ATTEMPTS; attempt++ )); do
    if az containerapp update --name "$app" --resource-group "$rg" --image "$image"; then
      UPDATED_APPS="${UPDATED_APPS}${app}"$'\n'
      return 0
    fi
    if (( attempt < AZ_UPDATE_MAX_ATTEMPTS )); then
      echo "==> WARNING: updating ${app} failed (attempt ${attempt}/${AZ_UPDATE_MAX_ATTEMPTS}); retrying in $(( attempt * 15 ))s..." >&2
      sleep $(( attempt * 15 ))
    fi
  done

  echo "ERROR: ${app} did not accept image ${image} after ${AZ_UPDATE_MAX_ATTEMPTS} attempts" >&2
  return 1
}
