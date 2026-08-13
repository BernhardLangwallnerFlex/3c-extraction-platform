"""promote.sh survives a transient `az` failure, and shouts when it can't.

Same hazard as deploy.sh — production's API and worker are two separate `az`
calls — but here a split pair is a split PRODUCTION, so the message has to say
that word. Shared implementation lives in scripts/lib/az_update.sh.
"""
import pathlib
import subprocess
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "promote.sh"

DEPLOYED_ON_TEST = "cr3cinvoice.azurecr.io/3cix-bps:v20260812a"
PROMOTABLE_TAG = "v20260812a"


@pytest.fixture
def stub_bin(tmp_path):
    """`az` that satisfies the guards but can be told to fail specific updates.

    `sleep` is stubbed too so the retry backoff costs no wall clock.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    az = bin_dir / "az"
    az.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # The "ran on test" guard reads the image off the test app.
        for arg in "$@"; do
          if [[ "$arg" == *"containers[0].image"* ]]; then
            echo "{DEPLOYED_ON_TEST}"
            exit 0
          fi
        done

        if [[ "$1 $2" == "containerapp update" ]]; then
          app=""
          while [[ $# -gt 0 ]]; do
            if [[ "$1" == "--name" ]]; then app="$2"; fi
            shift
          done
          echo "$app" >> "$CALL_LOG"
          if [[ "$app" == "$FAIL_APP" ]]; then
            seen=$(grep -c "^${{app}}$" "$CALL_LOG")
            if [[ "$seen" -le "$FAIL_TIMES" ]]; then
              echo "ERROR: Service Unavailable" >&2
              exit 1
            fi
          fi
        fi
        exit 0
    """))
    az.chmod(0o755)

    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)

    return bin_dir


@pytest.fixture
def git_repo(tmp_path):
    """A clean repo on main, so promote.sh's git guards pass."""
    work = tmp_path / "work"
    work.mkdir()
    run = lambda *a: subprocess.run(a, cwd=work, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "Test")
    (work / "f.txt").write_text("hello\n")
    run("git", "add", "f.txt")
    run("git", "commit", "-q", "-m", "init")
    return work


def run_promote(stub_bin, git_repo, tmp_path, *args, fail_app="", fail_times=0):
    call_log = tmp_path / "calls.log"
    call_log.write_text("")
    proc = subprocess.run(
        [str(SCRIPT), *args], cwd=git_repo,
        capture_output=True, text=True,
        env={
            "PATH": f"{stub_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(git_repo.parent),
            "CALL_LOG": str(call_log),
            "FAIL_APP": fail_app,
            "FAIL_TIMES": str(fail_times),
        },
    )
    calls = [l for l in call_log.read_text().splitlines() if l]
    return proc, calls


def test_transient_failure_is_retried(stub_bin, git_repo, tmp_path):
    proc, calls = run_promote(
        stub_bin, git_repo, tmp_path, "bps", PROMOTABLE_TAG, "--apply",
        fail_app="ca-api-bps", fail_times=1,
    )

    assert proc.returncode == 0, proc.stderr
    assert calls == ["ca-api-bps", "ca-api-bps", "ca-worker-bps"]


def test_split_production_is_named_as_production(stub_bin, git_repo, tmp_path):
    # API promoted, worker stuck: production now serves new code from an old
    # worker. The operator must not have to infer that from a stack trace.
    proc, _ = run_promote(
        stub_bin, git_repo, tmp_path, "bps", PROMOTABLE_TAG, "--apply",
        fail_app="ca-worker-bps", fail_times=99,
    )

    assert proc.returncode != 0
    assert "PRODUCTION IS SPLIT" in proc.stderr
    assert "ca-api-bps" in proc.stderr
    assert "re-running is safe" in proc.stderr


def test_clean_promote_is_quiet(stub_bin, git_repo, tmp_path):
    proc, calls = run_promote(
        stub_bin, git_repo, tmp_path, "bps", PROMOTABLE_TAG, "--apply",
    )

    assert proc.returncode == 0
    assert calls == ["ca-api-bps", "ca-worker-bps"]
    assert "SPLIT" not in proc.stderr


def test_a_guard_failure_does_not_report_a_split(stub_bin, git_repo, tmp_path):
    # The trap is armed only past the guards. A rejected tag must produce its
    # own message and nothing that reads like a second, unrelated failure.
    proc, calls = run_promote(
        stub_bin, git_repo, tmp_path, "bps", "v20260812b", "--apply",
    )

    assert proc.returncode != 0
    assert "not the image currently on test" in proc.stderr
    assert "SPLIT" not in proc.stderr
    assert "No app was updated" not in proc.stderr
    assert calls == []


def test_dry_run_updates_nothing(stub_bin, git_repo, tmp_path):
    proc, calls = run_promote(stub_bin, git_repo, tmp_path, "bps", PROMOTABLE_TAG)

    assert proc.returncode == 0
    assert "DRY RUN" in proc.stdout
    assert calls == []
