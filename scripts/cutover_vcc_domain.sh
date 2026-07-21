#!/usr/bin/env bash
#
# cutover_vcc_domain.sh — move the VCC custom domain from the OLD monolith
# (ca-invoice-api) to the NEW per-product app (ca-api-vetcostcheck).
#
# Does the AZURE side only (unbind old → bind new + managed cert). The DNS
# CNAME edit is manual at your registrar and MUST be done FIRST — this script
# refuses to run until it sees the CNAME already pointing at the new app.
#
# SAFE BY DEFAULT: with no flag it only checks preconditions and prints the
# plan. Pass --go to actually mutate. Run AFTER BUSINESS HOURS — there is an
# unavoidable few-minute outage (one environment allows the hostname on only
# one app, so the old binding must be removed before the new one is added).
#
# Usage:
#   ./scripts/cutover_vcc_domain.sh          # dry-run: preconditions + plan
#   ./scripts/cutover_vcc_domain.sh --go     # execute the cutover
#
# Recommended sequence:
#   1. (day before) lower the CNAME TTL at the registrar to ~300s.
#   2. (after hours) at registrar: repoint CNAME ->  <NEW_FQDN printed below>.
#   3. wait for propagation, then run this with --go.
#   4. run ./scripts/verify_vcc_domain_cutover.sh — expect all ✅.
#
# Rollback: repoint CNAME back to the old app FQDN, then
#   az containerapp hostname delete -n ca-api-vetcostcheck ... --yes
#   az containerapp hostname add   -n ca-invoice-api ...
#   az containerapp hostname bind  -n ca-invoice-api ... -e cae-3c-invoice -v CNAME

set -euo pipefail

DOMAIN="3cvetcostcheck.flex-capital-scale.com"
RG="rg-3c-invoice"
ENVIRONMENT="cae-3c-invoice"
NEW_APP="ca-api-vetcostcheck"
OLD_APP="ca-invoice-api"

GO=0
[[ "${1:-}" == "--go" ]] && GO=1

die() { echo "ERROR: $*" >&2; exit 1; }

command -v az  >/dev/null || die "az CLI not found"
command -v dig >/dev/null || die "dig not found"
az account show >/dev/null 2>&1 || die "not logged in (az login)"

NEW_FQDN=$(az containerapp show -n "$NEW_APP" -g "$RG" --query "properties.configuration.ingress.fqdn" -o tsv)
VERIF=$(az containerapp show -n "$NEW_APP" -g "$RG" --query "properties.customDomainVerificationId" -o tsv)

echo "== VCC domain cutover =="
echo "Domain : $DOMAIN"
echo "From   : $OLD_APP"
echo "To     : $NEW_APP  ($NEW_FQDN)"
echo "Mode   : $([[ $GO -eq 1 ]] && echo 'EXECUTE (--go)' || echo 'DRY-RUN (pass --go to execute)')"
echo

# --- Preconditions ------------------------------------------------------------
CNAME=$(dig +short CNAME "$DOMAIN" | sed 's/\.$//')
ASUID=$(dig +short TXT "asuid.$DOMAIN" | tr -d '"')

echo "[precondition] CNAME     -> ${CNAME:-<none>}"
echo "[precondition] asuid TXT -> ${ASUID:-<none>}"

[[ "$ASUID" == "$VERIF" ]] || die "asuid.$DOMAIN TXT ($ASUID) != env verification id ($VERIF). Fix the TXT record first."

if [[ "$CNAME" != "$NEW_FQDN" ]]; then
  echo
  echo "STOP: the CNAME does not yet point to the new app."
  echo "At your DNS registrar, set:"
  echo "    $DOMAIN.  CNAME  $NEW_FQDN"
  echo "(leave asuid.$DOMAIN TXT = $VERIF unchanged)"
  echo "Re-run this script once the new CNAME has propagated."
  exit 1
fi
echo "[precondition] ✅ CNAME already points to the new app; asuid TXT valid."
echo

# --- Plan ---------------------------------------------------------------------
echo "Plan:"
echo "  1. az containerapp hostname delete -n $OLD_APP -g $RG --hostname $DOMAIN --yes"
echo "  2. az containerapp hostname add    -n $NEW_APP -g $RG --hostname $DOMAIN"
echo "  3. az containerapp hostname bind   -n $NEW_APP -g $RG --hostname $DOMAIN -e $ENVIRONMENT -v CNAME"
echo

if [[ $GO -ne 1 ]]; then
  echo "DRY-RUN complete. No changes made. Re-run with --go to execute."
  exit 0
fi

# --- Execute ------------------------------------------------------------------
echo "==> [1/3] Removing hostname from $OLD_APP (brief outage starts) ..."
az containerapp hostname delete -n "$OLD_APP" -g "$RG" --hostname "$DOMAIN" --yes

echo "==> [2/3] Adding hostname to $NEW_APP (verifies via asuid TXT) ..."
az containerapp hostname add -n "$NEW_APP" -g "$RG" --hostname "$DOMAIN"

echo "==> [3/3] Binding managed certificate on $NEW_APP (CNAME validation; provisions in minutes) ..."
az containerapp hostname bind -n "$NEW_APP" -g "$RG" --hostname "$DOMAIN" -e "$ENVIRONMENT" -v CNAME

echo
echo "==> Binding submitted. Managed cert provisioning can take a few minutes."
echo "    Verify with:  ./scripts/verify_vcc_domain_cutover.sh"
