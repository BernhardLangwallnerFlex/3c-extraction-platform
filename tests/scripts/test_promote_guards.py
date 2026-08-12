"""Guards on scripts/promote.sh.

`az` is stubbed by a script on PATH that reports a fixed image for the test
app, so the guards can be exercised without touching Azure. Git guards run
against a throwaway repo created per test.
"""
import os
import pathlib
import subprocess
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "promote.sh"

DEPLOYED_ON_TEST = "cr3cinvoice.azurecr.io/3cix-bps:v20260812a"


@pytest.fixture
def stub_az(tmp_path):
    """A fake `az` that echoes the image supposedly deployed on the test app."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    az = bin_dir / "az"
    az.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Only the image query is used by the guards.
        for arg in "$@"; do
          if [[ "$arg" == *"containers[0].image"* ]]; then
            echo "{DEPLOYED_ON_TEST}"
            exit 0
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
        "PATH": f"{stub_az}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(git_repo.parent),
    }
    return subprocess.run(
        [str(SCRIPT), *args], cwd=git_repo,
        capture_output=True, text=True, env=env,
    )


def test_dry_run_is_the_default(git_repo, stub_az):
    proc = promote(git_repo, stub_az, "bps", "v20260812a")
    assert proc.returncode == 0
    assert "DRY RUN" in proc.stdout
    assert "ca-api-bps" in proc.stdout


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
