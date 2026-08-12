"""Tier-specific configuration emitted by provision_product.sh.

Runs the script with PROVISION_DRY_RUN=1, which prints resolved config and
exits before any az call (including the idempotency probes and ACR lookup).
Required secrets are stubbed with dummy values anyway, since dry-run's
behaviour should not depend on whether a real `az` happens to be on PATH.
"""
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "provision_product.sh"

_STUB_ENV = {
    "PROVISION_DRY_RUN": "1",
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
