"""Guards on scripts/promote.sh.

`az` is stubbed by a script on PATH that reports a fixed image for the test
app, so the guards can be exercised without touching Azure. Git guards run
against a throwaway repo created per test.
"""
import pathlib
import subprocess
import textwrap
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "promote.sh"

DEPLOYED_ON_TEST = "cr3cinvoice.azurecr.io/3cix-bps:v20260812a"


@pytest.fixture
def stub_az(tmp_path):
    """A fake `az` that echoes the image supposedly deployed on the test app,
    reports the ACR manifest as present for any tag, and — when AZ_STUB_LOG is
    set in the environment — appends the argv of every invocation to that log
    (one arg per line, blank line between calls) so tests can assert on
    mutating calls like `containerapp update`.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "az_calls.log"
    az = bin_dir / "az"
    az.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        if [[ -n "${{AZ_STUB_LOG:-}}" ]]; then
          {{
            for a in "$@"; do printf '%s\\n' "$a"; done
            printf '\\n'
          }} >> "$AZ_STUB_LOG"
        fi
        # Only the image query is used by the "ran on test" guard.
        for arg in "$@"; do
          if [[ "$arg" == *"containers[0].image"* ]]; then
            echo "{DEPLOYED_ON_TEST}"
            exit 0
          fi
        done
        exit 0
    """))
    az.chmod(0o755)
    return types.SimpleNamespace(bin_dir=bin_dir, log_path=log_path)


@pytest.fixture
def stub_az_missing_manifest(tmp_path):
    """A fake `az` where `acr manifest show` reports the manifest as absent."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    az = bin_dir / "az"
    az.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        for arg in "$@"; do
          if [[ "$arg" == "manifest" ]]; then
            echo "az acr manifest show: MANIFEST_UNKNOWN: manifest tag not found" >&2
            exit 1
          fi
        done
        exit 0
    """))
    az.chmod(0o755)
    return bin_dir


@pytest.fixture
def git_repo(tmp_path):
    """A clean git repo on main, so git guards pass unless a test dirties it."""
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


def promote(git_repo, stub_az, *args):
    env = {
        "PATH": f"{stub_az.bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(git_repo.parent),
        "AZ_STUB_LOG": str(stub_az.log_path),
    }
    return subprocess.run(
        [str(SCRIPT), *args], cwd=git_repo,
        capture_output=True, text=True, env=env,
    )


def _calls_from_log(log_path):
    """Parse the az-invocation log into a list of argv lists."""
    if not log_path.exists():
        return []
    raw = log_path.read_text()
    calls = []
    for block in raw.split("\n\n"):
        lines = [l for l in block.splitlines() if l != ""]
        if lines:
            calls.append(lines)
    return calls


def test_dry_run_is_the_default(git_repo, stub_az):
    proc = promote(git_repo, stub_az, "bps", "v20260812a")
    assert proc.returncode == 0
    assert "DRY RUN" in proc.stdout

    lines = proc.stdout.splitlines()
    marker_idx = next(i for i, l in enumerate(lines) if l.startswith("Would promote"))
    after = lines[marker_idx + 1:]
    indented = [l[2:] for l in after if l.startswith("  ")]

    assert indented == ["ca-api-bps", "ca-worker-bps"]
    assert "-test" not in "\n".join(after)


def test_rejects_a_tag_not_deployed_on_test(git_repo, stub_az):
    """The load-bearing guard: you cannot promote what was never tested."""
    proc = promote(git_repo, stub_az, "bps", "v20260812b")
    assert proc.returncode != 0
    assert "not the image currently on test" in proc.stderr


def test_rejects_latest(git_repo, stub_az):
    proc = promote(git_repo, stub_az, "bps", "latest")
    assert proc.returncode != 0
    assert "latest" in proc.stderr


def test_rejects_a_dirty_tree(git_repo, stub_az):
    (git_repo / "f.txt").write_text("modified\n")
    proc = promote(git_repo, stub_az, "bps", "v20260812a")
    assert proc.returncode != 0
    assert "uncommitted changes" in proc.stderr


def test_rejects_a_non_main_branch(git_repo, stub_az):
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=git_repo, check=True)
    proc = promote(git_repo, stub_az, "bps", "v20260812a")
    assert proc.returncode != 0
    assert "main" in proc.stderr


def test_force_rollback_skips_only_the_test_image_guard(git_repo, stub_az):
    """Rollback to an older tag is legitimate; the other guards still apply."""
    proc = promote(git_repo, stub_az, "bps", "v20260101a", "--force-rollback")
    assert proc.returncode == 0
    assert "DRY RUN" in proc.stdout

    (git_repo / "f.txt").write_text("dirty\n")
    proc = promote(git_repo, stub_az, "bps", "v20260101a", "--force-rollback")
    assert proc.returncode != 0
    assert "uncommitted changes" in proc.stderr


def test_force_rollback_refuses_an_image_missing_from_acr(git_repo, stub_az_missing_manifest):
    """--force-rollback still must not accept an unverifiable/typo'd tag."""
    env = {
        "PATH": f"{stub_az_missing_manifest}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(git_repo.parent),
    }
    proc = subprocess.run(
        [str(SCRIPT), "bps", "v20250101-typo", "--force-rollback"],
        cwd=git_repo, capture_output=True, text=True, env=env,
    )
    assert proc.returncode != 0
    assert "not found" in proc.stderr.lower()


def test_apply_updates_exactly_the_prod_apps_with_the_right_image(git_repo, stub_az):
    proc = promote(git_repo, stub_az, "bps", "v20260812a", "--apply")
    assert proc.returncode == 0, proc.stderr

    calls = _calls_from_log(stub_az.log_path)
    update_calls = [c for c in calls if len(c) >= 2 and c[0] == "containerapp" and c[1] == "update"]
    assert len(update_calls) == 2

    def arg_after(call, flag):
        return call[call.index(flag) + 1]

    names = {arg_after(c, "--name") for c in update_calls}
    assert names == {"ca-api-bps", "ca-worker-bps"}

    for c in update_calls:
        assert arg_after(c, "--image") == "cr3cinvoice.azurecr.io/3cix-bps:v20260812a"
