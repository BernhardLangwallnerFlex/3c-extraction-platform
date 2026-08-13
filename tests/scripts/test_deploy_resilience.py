"""deploy.sh survives a transient `az` failure, and shouts when it can't.

Azure intermittently answers a containerapp update with a 5xx even when the
update lands. Because a product's API and worker are two separate `az` calls,
an unretried blip leaves the tier split — an API serving new code while its
worker runs old code, which produces results that look wrong rather than
absent. These tests drive the real script against a stubbed `az`.
"""
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy.sh"

# `sleep` is stubbed out alongside `az` so the retry backoff costs no wall clock.
_AZ_STUB = r"""#!/usr/bin/env bash
case "$1 $2" in
  "acr show")          echo "cr3cinvoice.azurecr.io"; exit 0 ;;
  "acr build")         exit 0 ;;
  "containerapp show") exit 0 ;;
  "containerapp update")
    app=""
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "--name" ]]; then app="$2"; fi
      shift
    done
    echo "$app" >> "$CALL_LOG"
    if [[ "$app" == "$FAIL_APP" ]]; then
      seen=$(grep -c "^${app}$" "$CALL_LOG")
      if [[ "$seen" -le "$FAIL_TIMES" ]]; then
        echo "ERROR: Service Unavailable" >&2
        exit 1
      fi
    fi
    exit 0 ;;
esac
exit 0
"""

_SLEEP_STUB = "#!/usr/bin/env bash\nexit 0\n"


@pytest.fixture
def stub_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("az", _AZ_STUB), ("sleep", _SLEEP_STUB)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    return bin_dir


def run_deploy(stub_bin, tmp_path, *args, fail_app="", fail_times=0):
    call_log = tmp_path / "calls.log"
    call_log.write_text("")
    proc = subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True, text=True, cwd=REPO,
        env={
            "PATH": f"{stub_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "CALL_LOG": str(call_log),
            "FAIL_APP": fail_app,
            "FAIL_TIMES": str(fail_times),
        },
    )
    calls = [l for l in call_log.read_text().splitlines() if l]
    return proc, calls


def test_transient_failure_is_retried_and_the_deploy_succeeds(stub_bin, tmp_path):
    # Exactly the failure we hit in production: one 5xx on the API update.
    proc, calls = run_deploy(
        stub_bin, tmp_path, "bps", "v1", "test",
        fail_app="ca-api-bps-test", fail_times=1,
    )

    assert proc.returncode == 0, proc.stderr
    # Two attempts on the flaky app, one on the worker — and the worker was
    # still reached, which is the point.
    assert calls == ["ca-api-bps-test", "ca-api-bps-test", "ca-worker-bps-test"]


def test_retries_are_capped(stub_bin, tmp_path):
    proc, calls = run_deploy(
        stub_bin, tmp_path, "bps", "v1", "test",
        fail_app="ca-api-bps-test", fail_times=99,
    )

    assert proc.returncode != 0
    assert calls.count("ca-api-bps-test") == 3
    # The worker must NOT be updated when the API never took the image.
    assert "ca-worker-bps-test" not in calls


def test_a_split_tier_is_reported_loudly(stub_bin, tmp_path):
    # API succeeds, worker is unfixably broken -> the exact split state that a
    # silent failure would hide.
    proc, _ = run_deploy(
        stub_bin, tmp_path, "bps", "v1", "test",
        fail_app="ca-worker-bps-test", fail_times=99,
    )

    assert proc.returncode != 0
    assert "the tier IS SPLIT" in proc.stderr
    assert "ca-api-bps-test" in proc.stderr       # what did land
    assert "re-running is safe" in proc.stderr


def test_clean_run_says_nothing_about_splits(stub_bin, tmp_path):
    proc, calls = run_deploy(stub_bin, tmp_path, "bps", "v1", "test")

    assert proc.returncode == 0
    assert calls == ["ca-api-bps-test", "ca-worker-bps-test"]
    assert "SPLIT" not in proc.stderr
    assert "FAILED" not in proc.stderr


def test_failure_on_first_product_does_not_touch_later_products(stub_bin, tmp_path):
    # `all` deploys three products in sequence. Aborting on the first must not
    # leave two more half-deployed behind it.
    proc, calls = run_deploy(
        stub_bin, tmp_path, "all", "v1", "test",
        fail_app="ca-api-vetcostcheck-test", fail_times=99,
    )

    assert proc.returncode != 0
    assert not [c for c in calls if "bps" in c or "sanierer" in c]
