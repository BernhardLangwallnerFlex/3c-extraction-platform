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
