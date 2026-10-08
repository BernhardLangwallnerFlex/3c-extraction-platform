# Network access — ingress IP allowlist

This config lives **only in Azure**, not in this repo. It was invisible to
`grep` until surveyed on 2026-09-06; this file exists so the next person
doesn't have to rediscover it from `az`.

## Current state (2026-09-07)

All six API apps carry a **byte-identical 103-rule allowlist**, verified equal:

| App | Ingress | Rules |
|---|---|---|
| `ca-api-<product>` (prod ×3) | external | 103 |
| `ca-api-<product>-test` (×3) | external | 103 — **applied 2026-09-07** |
| `ca-worker-*` (×6) | internal | n/a |
| `ca-vetcostcheck-ui` | external | 0 — open to the internet |

Enforcement is Container Apps native ingress access restrictions. Every rule is
`action: Allow` and there is no explicit Deny — **the first Allow rule
implicitly denies everything else**, so the list *is* the policy. Corollary:
emptying the list does not lock things down, it opens them up.

Rules are app-wide, so `/healthz` is fenced too. Blocked callers get
`403 RBAC: access denied`.

This is the **only** network layer. `3cixstorage` is `defaultAction: Allow`
with no IP rules and `redis-3c-invoice-v2` has `publicNetworkAccess: Enabled`
with no firewall rules — both are key-auth only. Do not assume otherwise.

## What the 103 are

| Group | Count | Purpose |
|---|---|---|
| Real callers | 11 | 3C Heilbronn + Erfurt, Flex office, OVH addresses, 2 home office, env static IP |
| `azure-monitor-*` | 31 | `ApplicationInsightsAvailability` agents for the `healthz-ping` webtest |
| `aca-ui-outbound-*` | 61 | egress IPs of `ca-vetcostcheck-ui` |

Full inventory with per-IP whois attribution:
`../garagenhub-extractor/docs/ip-allowlist.md`.

## Why the test tier got the full 103 verbatim

`ca-vetcostcheck-ui` drives **both** tiers over public hostnames
(`VETCOSTCHECK_TEST_API_URL` etc. → `3c<product>-test.flex-capital-scale.com`,
all bound and resolving). Keeping the UI's path to test as realistic as prod's
was the explicit call, so test got prod's list unchanged rather than a
trimmed one. That means test also inherits the 31 monitor ranges it has no
webtest for, and the 61 UI egress rules.

Verified after applying — allowlisted office IP → `200`, outside IP → `403`, on
all three test hostnames; custom domains, managed certs, target port and
external ingress all intact.

## TODO: clean up IP handling

**The list is append-only and it shows.** 61 of 103 rules are one architectural
choice's scar tissue: the UI calls the APIs' *public* FQDNs, ACA egress IPs are
not stable, and every drift appended another `/32` instead of updating the
existing entry. That is now duplicated across six apps.

At least one rule is already stale: `allowed-home-office` is
`79.224.138.129/32`, a Deutsche Telekom **dynamic** address
(`dip0.t-ipconnect.de`). It had already rotated when checked on 2026-09-06 —
the same line was on `79.224.130.176` and got a `403` from prod. Nobody
noticed, because the failure mode is a silent 403 for one person.

**The fix already exists in the sibling repo.** `garagenhub-extractor` has
`infra/allowlist.txt` (source of truth, one line per rule with owner and
reason) and `scripts/allowlist.py`, which reconciles Azure to that file —
adding, updating **and removing** rules, with a plan/`--apply` split. It was
written specifically because this deployment grew to 103 rules.

Port it here:

- Copy `scripts/allowlist.py`; it already defaults to `rg-3c-invoice` and takes
  `--app`, so it should need little more than a multi-app loop.
- Create `infra/allowlist.txt` from the current 103, with the groups above as
  comment blocks and an owner + reason per rule.
- Collapse what is collapsible: `51.38.123.200–203` → `51.38.123.200/30`.
- Decide the UI question rather than inheriting it. Either give the environment
  a stable egress (VNet + NAT gateway) so one rule replaces all 61, or accept
  the churn deliberately. Today it is neither — it is accreted.
- Re-check the OVH entries (`162.19.197.17`, `51.38.123.200/30`) are still live
  callers before carrying them forward. **Update 2026-09-07:** 3C is building a
  proxy/gateway so tester laptops — home office included — all egress through
  one address, believed to be `51.38.123.201` (already allowlisted, hence the
  OVH block). Under test. If it holds, the per-tester dynamic rules stop being
  needed and the churn problem largely goes away on the human-caller side.
  Wait for the outcome before investing in this list; it does nothing for the
  61 `aca-ui-outbound-*` rules, which are a separate problem.
- Then wire it into `deploy.sh` / `scripts/provision_product.sh` so a new
  product pair gets the allowlist at provision time instead of by hand.

Until that lands, changes must be made to **all six apps** or the tiers drift.

## Applying by hand in the meantime

103 rules × `az containerapp ingress access-restriction set` is ~5 minutes per
app. One ARM PATCH with the complete ingress object is a single call:

```bash
SUB=c3f253c2-c792-4a6e-878e-c5d49e589d6c
az rest --method PATCH \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/rg-3c-invoice/providers/Microsoft.App/containerApps/<app>?api-version=2025-01-01" \
  --body @patch.json
```

Build `patch.json` as `{"properties":{"configuration":{"ingress": <full ingress
object>}}}` — take the app's existing ingress from `az containerapp show`, drop
the read-only `fqdn`, and set `ipSecurityRestrictions`. Send the **whole**
ingress object, not just the restrictions: arrays are replaced wholesale, so a
partial body risks dropping `customDomains` and its managed certificate binding.

Verify both directions. A `200` from your own IP only proves you are on the
list — you need a request from an unlisted network to prove the deny works.

## Two known gaps

- `ca-vetcostcheck-ui` has **zero** IP restrictions. The UI in front of the
  fenced APIs is itself open to the internet; it is guarded only by its own
  shared password.
- `aca-environment-static` (`131.189.138.236/32`) allows the whole
  `cae-3c-invoice` environment's egress — an environment now shared with the
  Garagenhub apps. Broader than the name suggests.
