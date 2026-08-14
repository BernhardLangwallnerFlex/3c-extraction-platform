"""infra/lifecycle-policy.json covers every prefix the pipeline actually writes.

The failure this guards against: someone adds a fourth product, provisioning
gives it `uploads-<new>/` and `processed-<new>/`, and nothing expires them —
silently, because a missing lifecycle rule has no symptom until an audit. The
prefixes are read from scripts/lib/tier.sh, the same source of truth the deploy
and provisioning scripts use, so the two cannot drift apart unnoticed.
"""
import json
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
POLICY_FILE = REPO / "infra" / "lifecycle-policy.json"
TIER_LIB = REPO / "scripts" / "lib" / "tier.sh"

RETENTION_DAYS = 14
# Azure caps prefixMatch at 10 entries per rule.
MAX_PREFIXES_PER_RULE = 10

PRODUCTS = sorted(
    p.name for p in (REPO / "products").iterdir()
    if p.is_dir() and (p / "product.py").exists()
)
TIERS = ["prod", "test"]


@pytest.fixture(scope="module")
def policy():
    return json.loads(POLICY_FILE.read_text())


@pytest.fixture(scope="module")
def all_prefixes(policy):
    return [p for rule in policy["rules"]
            for p in rule["definition"]["filters"]["prefixMatch"]]


def tier_prefixes(product, tier):
    """Ask tier.sh directly, in the blob-path form a lifecycle rule needs."""
    out = subprocess.run(
        ["bash", "-c",
         f'source "{TIER_LIB}"; resolve_tier_names {product} {tier}; '
         f'echo "$AZURE_INPUT_PREFIX"; echo "$AZURE_OUTPUT_PREFIX"'],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    # az://invoices/uploads-bps/ -> invoices/uploads-bps/
    return [u.replace("az://", "") for u in out]


def test_three_products_are_discovered():
    # Guards the guard: if this list silently emptied, every test below would
    # pass vacuously.
    assert PRODUCTS == ["bps", "sanierer", "vetcostcheck"]


@pytest.mark.parametrize("product", PRODUCTS)
@pytest.mark.parametrize("tier", TIERS)
def test_every_product_and_tier_prefix_is_covered(product, tier, all_prefixes):
    for prefix in tier_prefixes(product, tier):
        assert prefix in all_prefixes, (
            f"{product}/{tier} writes to {prefix}, which no lifecycle rule covers — "
            f"those blobs would never expire"
        )


def test_legacy_prefixes_are_covered(all_prefixes):
    # Pre-cutover blobs from ca-invoice-api, which no product config names.
    assert "invoices/uploads/" in all_prefixes
    assert "invoices/processed/" in all_prefixes


def test_no_prefix_is_claimed_by_two_rules(all_prefixes):
    duplicates = {p for p in all_prefixes if all_prefixes.count(p) > 1}
    assert not duplicates, f"prefixes in more than one rule: {duplicates}"


def test_every_rule_is_enabled_and_expires_at_the_agreed_retention(policy):
    for rule in policy["rules"]:
        assert rule["enabled"], f"{rule['name']} is disabled and would do nothing"
        delete = rule["definition"]["actions"]["baseBlob"]["delete"]
        assert delete["daysAfterModificationGreaterThan"] == RETENTION_DAYS


def test_rules_stay_within_azures_prefix_limit(policy):
    for rule in policy["rules"]:
        count = len(rule["definition"]["filters"]["prefixMatch"])
        assert count <= MAX_PREFIXES_PER_RULE, (
            f"{rule['name']} has {count} prefixes; Azure rejects more than "
            f"{MAX_PREFIXES_PER_RULE} — split it into another rule"
        )


def test_prefixes_are_container_qualified_and_directory_scoped(all_prefixes):
    for prefix in all_prefixes:
        # Without the container name Azure matches nothing; without the trailing
        # slash "uploads-bps" would also swallow "uploads-bps-test".
        assert prefix.startswith("invoices/"), prefix
        assert prefix.endswith("/"), prefix
