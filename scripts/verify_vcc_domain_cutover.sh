#!/usr/bin/env bash
#
# verify_vcc_domain_cutover.sh — READ-ONLY cutover diagnostic for the VCC
# custom domain. Changes NOTHING. Run it before the cutover (expect: still on
# the OLD app), then again after (expect: on the NEW app) and diff the verdicts.
#
# Background: the VCC custom domain currently CNAMEs to the OLD pre-split
# monolith (ca-invoice-api). The cutover repoints it to the new per-product
# app (ca-api-vetcostcheck). The domain-verification TXT (asuid) is per
# ENVIRONMENT, so it does NOT change (both apps are in cae-3c-invoice).
#
# Usage:
#   ./scripts/verify_vcc_domain_cutover.sh [domain]
# Requires: az (logged in), dig, curl, shasum.

set -uo pipefail

DOMAIN="${1:-3cvetcostcheck.flex-capital-scale.com}"
RG="rg-3c-invoice"
NEW_APP="ca-api-vetcostcheck"
OLD_APP="ca-invoice-api"

pass=0; fail=0
ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
bad()  { echo "  ❌ $1"; fail=$((fail+1)); }
info() { echo "  •  $1"; }

echo "== VCC domain cutover check =="
echo "Domain: $DOMAIN"
echo "Time:   $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo

# --- Facts from Azure (read-only) ---------------------------------------------
NEW_FQDN=$(az containerapp show -n "$NEW_APP" -g "$RG" --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null)
NEW_VERIF=$(az containerapp show -n "$NEW_APP" -g "$RG" --query "properties.customDomainVerificationId" -o tsv 2>/dev/null)
OLD_FQDN=$(az containerapp show -n "$OLD_APP" -g "$RG" --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null)
# Which app currently has the hostname bound?
NEW_BOUND=$(az containerapp show -n "$NEW_APP" -g "$RG" --query "properties.configuration.ingress.customDomains[?name=='$DOMAIN'] | length(@)" -o tsv 2>/dev/null)
OLD_BOUND=$(az containerapp show -n "$OLD_APP" -g "$RG" --query "properties.configuration.ingress.customDomains[?name=='$DOMAIN'] | length(@)" -o tsv 2>/dev/null)

echo "[Azure] new app  $NEW_APP -> $NEW_FQDN"
echo "[Azure] old app  $OLD_APP -> $OLD_FQDN"
echo "[Azure] env domain-verification id: $NEW_VERIF"
echo

# --- DNS ----------------------------------------------------------------------
echo "[DNS]"
CNAME=$(dig +short CNAME "$DOMAIN" | sed 's/\.$//')
info "CNAME -> ${CNAME:-<none>}"
ASUID=$(dig +short TXT "asuid.$DOMAIN" | tr -d '"')
info "asuid TXT -> ${ASUID:-<none>}"
if [[ "$CNAME" == "$NEW_FQDN" ]]; then
  ok "CNAME points to NEW app"
elif [[ "$CNAME" == "$OLD_FQDN" ]]; then
  bad "CNAME still points to OLD app ($OLD_APP)"
else
  bad "CNAME points somewhere unexpected"
fi
if [[ "$ASUID" == "$NEW_VERIF" ]]; then
  ok "asuid TXT matches environment verification id (valid for new app too)"
else
  bad "asuid TXT does not match — new-app binding will fail verification"
fi
echo

# --- Azure binding ------------------------------------------------------------
echo "[Binding]"
if [[ "${NEW_BOUND:-0}" == "1" ]]; then ok "hostname bound to NEW app"; else bad "hostname NOT bound to NEW app"; fi
if [[ "${OLD_BOUND:-0}" == "1" ]]; then bad "hostname STILL bound to OLD app (must be removed — one env, one binding)"; else ok "hostname not bound to OLD app"; fi
echo

# --- Live HTTP + which app answers -------------------------------------------
echo "[HTTP]"
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "https://$DOMAIN/healthz" 2>/dev/null)
[[ "$CODE" == "200" ]] && ok "https://$DOMAIN/healthz -> 200" || bad "https://$DOMAIN/healthz -> ${CODE:-no-response}"

# Structural discriminator: does the domain serve the SAME OpenAPI as the new app?
dom_hash=$(curl -s --max-time 20 "https://$DOMAIN/openapi.json" 2>/dev/null | shasum | awk '{print $1}')
new_hash=$(curl -s --max-time 20 "https://$NEW_FQDN/openapi.json" 2>/dev/null | shasum | awk '{print $1}')
old_hash=$(curl -s --max-time 20 "https://$OLD_FQDN/openapi.json" 2>/dev/null | shasum | awk '{print $1}')
info "openapi.json sha  domain=$dom_hash"
info "openapi.json sha  new=$new_hash  old=$old_hash"
if [[ -n "$dom_hash" && "$dom_hash" == "$new_hash" ]]; then
  ok "domain serves the NEW app's API (openapi matches ca-api-vetcostcheck)"
elif [[ -n "$dom_hash" && "$dom_hash" == "$old_hash" ]]; then
  bad "domain still serves the OLD app's API (openapi matches ca-invoice-api)"
else
  info "openapi comparison inconclusive (endpoint may be auth-gated); rely on CNAME + binding checks"
fi
echo

# --- Verdict ------------------------------------------------------------------
echo "== Verdict: $pass ok, $fail issue(s) =="
if [[ "$fail" -eq 0 ]]; then
  echo "CUTOVER COMPLETE — domain is on the new VCC app."
else
  echo "NOT cut over yet (or in progress). See ❌ items above."
fi
