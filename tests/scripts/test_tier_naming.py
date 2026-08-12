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
