# Artifact Retention & Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete every stored artifact for a job as soon as extraction succeeds, keep the result JSON for 14 days, and purge the 2.4 GB backlog that accumulated since January.

**Architecture:** Two independent mechanisms. (1) `Pipeline.cleanup_storage_artifacts()` deletes the upload blob and per-subdocument markdown / sub-PDF / page image immediately after a successful extraction, called from `process_file`. (2) An Azure lifecycle rule on the `invoices` container deletes anything older than 14 days, covering what the app cannot reach: failed jobs, orphaned uploads, result JSONs, and blobs left by a crashed worker. A one-off script purges the existing backlog.

**Tech Stack:** Python 3.11+, pytest 9.0.3, structlog, sentry-sdk, azure-storage-blob, Azure CLI.

**Spec:** `docs/superpowers/specs/2026-07-29-artifact-retention-design.md`

## Global Constraints

- Retention window is **14 days**, for both result JSONs and all artifacts of failed jobs.
- Cleanup runs **only after a successful extraction**. Every failure path must leave artifacts intact so a job can be reproduced.
- Cleanup **must never fail a job**. Extraction has already succeeded when it runs; every delete is individually guarded.
- `extracted_data_<stem>.json` is **never** deleted by application code. Only the lifecycle rule expires it.
- Storage account: `3cixstorage`, resource group `3c_information_extraction`, container `invoices`.
- Container Apps resource group: `rg-3c-invoice`. Products: `vetcostcheck`, `bps`, `sanierer`.
- Blob soft delete is enabled (7 days, `allowPermanentDelete: false`) — verified 2026-07-29. Deletions are recoverable within that window.
- Run tests with `.venv/bin/python -m pytest`. The repo has no linter configured.
- `deploy.sh` requires a unique tag; `latest` silently skips creating a new revision.

---

### Task 1: `Pipeline.cleanup_storage_artifacts()`

Replaces the dead `cleanup_temporary_files()` (never called from anywhere) with a method that also deletes the upload blob and guards each delete individually. `AzureBlobStorage.delete()` raises `ResourceNotFoundError` for a missing blob, so an unguarded loop would abort partway and leave the remaining artifacts behind.

The `CLEANUP_ARTIFACTS` flag is checked *inside* the method rather than at the call site, so the flag's behavior is unit-testable without constructing a real job.

**Files:**
- Modify: `core/pipeline.py` — add `import sentry_sdk` near the existing imports; replace `cleanup_temporary_files` at lines 361-375
- Test: `tests/core/test_pipeline_cleanup.py` (create)

**Interfaces:**
- Consumes: `SubdocumentArtifact` (`core/pipeline.py:47`) with fields `md_key`, `pdf_key`, `image_key`; `StorageBackend.delete(key)` (`core/storage/storage.py:38`); `Pipeline.file_key`, `Pipeline.subdocuments`
- Produces: `Pipeline.cleanup_storage_artifacts() -> int` — returns the count of successfully deleted keys, never raises. Task 2 calls it.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_pipeline_cleanup.py`:

```python
"""Cleanup of stored artifacts after a successful extraction.

Follows the construction trick in test_pipeline_postprocess.py: build a Pipeline
via object.__new__ so __init__ (OCR, PDF I/O, storage materialization) never runs.
"""
import pytest

from core.pipeline import Pipeline, SubdocumentArtifact


class _RecordingStorage:
    """Records deletes; raises for any key listed in fail_keys."""

    def __init__(self, fail_keys=()):
        self.deleted = []
        self.fail_keys = set(fail_keys)

    def delete(self, key):
        if key in self.fail_keys:
            raise RuntimeError(f"delete refused: {key}")
        self.deleted.append(key)


def _subdoc(n):
    return SubdocumentArtifact(
        document_number=n,
        page_numbers=[n],
        markdown="dummy",
        md_key=f"az://invoices/processed-bps/abc_subdocument_{n}.md",
        pdf_key=f"az://invoices/processed-bps/abc_subdocument_{n}.pdf",
        image_key=f"az://invoices/processed-bps/abc_subdocument_{n}.png",
    )


def _make_pipeline(storage, subdoc_count=2):
    pipe = object.__new__(Pipeline)
    pipe.storage = storage
    pipe.file_key = "az://invoices/uploads-bps/abc.pdf"
    pipe.subdocuments = [_subdoc(n) for n in range(1, subdoc_count + 1)]
    pipe.output_prefix = "az://invoices/processed-bps/"
    pipe.stem = "abc"
    return pipe


@pytest.fixture(autouse=True)
def _cleanup_enabled(monkeypatch):
    # load_dotenv() at import time may have set this from a local .env; pin it.
    monkeypatch.setenv("CLEANUP_ARTIFACTS", "true")


def test_deletes_upload_and_every_subdoc_artifact():
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage, subdoc_count=2)

    deleted = pipe.cleanup_storage_artifacts()

    assert deleted == 7  # 1 upload + 2 subdocs x 3 artifacts
    assert set(storage.deleted) == {
        "az://invoices/uploads-bps/abc.pdf",
        "az://invoices/processed-bps/abc_subdocument_1.md",
        "az://invoices/processed-bps/abc_subdocument_1.pdf",
        "az://invoices/processed-bps/abc_subdocument_1.png",
        "az://invoices/processed-bps/abc_subdocument_2.md",
        "az://invoices/processed-bps/abc_subdocument_2.pdf",
        "az://invoices/processed-bps/abc_subdocument_2.png",
    }


def test_never_deletes_the_result_json():
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage)

    pipe.cleanup_storage_artifacts()

    # The key exactly as extract_data_from_subdocuments writes it (core/pipeline.py:358).
    result_key = f"{pipe.output_prefix}/extracted_data_{pipe.stem}.json"
    assert result_key not in storage.deleted
    assert not [k for k in storage.deleted if "extracted_data" in k]


def test_one_failing_delete_does_not_stop_the_others():
    doomed = "az://invoices/processed-bps/abc_subdocument_1.pdf"
    storage = _RecordingStorage(fail_keys=[doomed])
    pipe = _make_pipeline(storage, subdoc_count=2)

    deleted = pipe.cleanup_storage_artifacts()

    assert deleted == 6
    assert doomed not in storage.deleted
    assert "az://invoices/uploads-bps/abc.pdf" in storage.deleted
    assert "az://invoices/processed-bps/abc_subdocument_2.png" in storage.deleted


def test_disabled_by_env_flag(monkeypatch):
    monkeypatch.setenv("CLEANUP_ARTIFACTS", "false")
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage)

    assert pipe.cleanup_storage_artifacts() == 0
    assert storage.deleted == []


def test_total_failure_reports_to_sentry(monkeypatch):
    all_keys = [
        "az://invoices/uploads-bps/abc.pdf",
        "az://invoices/processed-bps/abc_subdocument_1.md",
        "az://invoices/processed-bps/abc_subdocument_1.pdf",
        "az://invoices/processed-bps/abc_subdocument_1.png",
    ]
    storage = _RecordingStorage(fail_keys=all_keys)
    pipe = _make_pipeline(storage, subdoc_count=1)

    captured = []
    monkeypatch.setattr(
        "core.pipeline.sentry_sdk.capture_message",
        lambda msg, level=None: captured.append((msg, level)),
    )

    assert pipe.cleanup_storage_artifacts() == 0  # does not raise
    assert len(captured) == 1
    assert captured[0][1] == "warning"


def test_partial_failure_does_not_report_to_sentry(monkeypatch):
    storage = _RecordingStorage(fail_keys=["az://invoices/uploads-bps/abc.pdf"])
    pipe = _make_pipeline(storage, subdoc_count=1)

    captured = []
    monkeypatch.setattr(
        "core.pipeline.sentry_sdk.capture_message",
        lambda msg, level=None: captured.append((msg, level)),
    )

    assert pipe.cleanup_storage_artifacts() == 3
    assert captured == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_cleanup.py -v`
Expected: all 6 FAIL with `AttributeError: 'Pipeline' object has no attribute 'cleanup_storage_artifacts'`.

- [ ] **Step 3: Add the sentry import**

In `core/pipeline.py`, add after `import shutil` (line 17):

```python
import sentry_sdk
```

- [ ] **Step 4: Replace `cleanup_temporary_files` with `cleanup_storage_artifacts`**

Delete the whole `cleanup_temporary_files` method (`core/pipeline.py:361-375`) and put this in its place, immediately above `cleanup_local`:

```python
    def cleanup_storage_artifacts(self) -> int:
        """Delete every stored artifact for this job except the result JSON.

        Removes the upload blob and each subdocument's markdown, sub-PDF and page
        image. `extracted_data_<stem>.json` is deliberately kept — the 14-day
        lifecycle rule on the container expires it.

        Never raises. Extraction has already succeeded by the time this runs, so a
        storage hiccup must not turn a good job into a failed one: every key is
        deleted independently and failures are logged. Returns the number of keys
        actually deleted.
        """
        if os.getenv("CLEANUP_ARTIFACTS", "true").strip().lower() not in {"1", "true", "yes"}:
            _telemetry.info("artifact_cleanup_skipped", reason="CLEANUP_ARTIFACTS disabled")
            return 0

        keys = [self.file_key]
        for subdoc in self.subdocuments:
            keys.extend([subdoc.md_key, subdoc.pdf_key, subdoc.image_key])

        deleted = 0
        failed = 0
        for key in keys:
            try:
                self.storage.delete(key)
                deleted += 1
            except Exception as exc:
                failed += 1
                _telemetry.warning("artifact_delete_failed", key=key, error=str(exc))

        _telemetry.info(
            "artifact_cleanup_completed", deleted=deleted, failed=failed, total=len(keys)
        )

        # Every delete failing points at credentials or permissions rather than a
        # stray missing blob — surface it the way DualOCR degradation is surfaced.
        if failed and not deleted:
            sentry_sdk.capture_message(
                f"Artifact cleanup deleted nothing: all {failed} deletes failed",
                level="warning",
            )

        return deleted
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_cleanup.py -v`
Expected: 6 passed.

- [ ] **Step 6: Run the whole suite to check nothing regressed**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all pass. Nothing referenced `cleanup_temporary_files`, so removing it breaks nothing — confirm with `grep -rn "cleanup_temporary_files" --include="*.py" .` returning no hits outside `.venv`.

- [ ] **Step 7: Commit**

```bash
git add core/pipeline.py tests/core/test_pipeline_cleanup.py
git commit -m "$(cat <<'EOF'
feat: add Pipeline.cleanup_storage_artifacts (replaces dead cleanup_temporary_files)

Deletes the upload blob plus each subdocument's md/pdf/png, keeping the result
JSON for the lifecycle rule to expire. Each delete is guarded so cleanup can
never fail a job whose extraction already succeeded. Gated on CLEANUP_ARTIFACTS
(default true) so local runs can keep their artifacts.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Call cleanup from the job, document the flag

**Files:**
- Modify: `core/jobs/tasks.py:124-129` (between the `extraction_completed` and `job_completed` log lines)
- Modify: `.env.example:3-11` (Storage section)
- Modify: `CLAUDE.md` — add `CLEANUP_ARTIFACTS` to Key Environment Variables; correct the stale "no test suite" claim

**Interfaces:**
- Consumes: `Pipeline.cleanup_storage_artifacts() -> int` from Task 1

- [ ] **Step 1: Wire the call into `process_file`**

In `core/jobs/tasks.py`, after the `extraction_completed` log line (currently line 126) and before the `total_duration` line, insert:

```python
        # Only reached when extraction succeeded — every exception path above skips
        # cleanup so a failed job keeps its artifacts for reproduction.
        t = time.monotonic()
        deleted = invoice.cleanup_storage_artifacts()
        log.info("artifact_cleanup", file_id=file_id,
                 duration_s=round(time.monotonic() - t, 2), deleted=deleted)
```

- [ ] **Step 2: Verify placement by reading it back**

Run: `.venv/bin/python -c "import ast,sys; ast.parse(open('core/jobs/tasks.py').read()); print('parses')"`
Expected: `parses`

Then `grep -n "artifact_cleanup\|job_completed\|cleanup_local" core/jobs/tasks.py` — expected order: `artifact_cleanup` before `job_completed`, and `cleanup_local` still last, inside `finally`.

- [ ] **Step 3: Document the flag in `.env.example`**

Replace the Storage section (lines 3-11) with:

```
# --- Storage ---
STORAGE_BACKEND=local                    # local | azure | s3
LOCAL_STORAGE_BASE_DIR=./temp            # Only used when STORAGE_BACKEND=local

# Delete the upload + intermediates after a successful job (result JSON is kept
# and expired by the container's 14-day lifecycle rule). Defaults to true in
# code; keep it false locally so ./temp artifacts survive for inspection.
CLEANUP_ARTIFACTS=false

# Azure Blob Storage (only needed when STORAGE_BACKEND=azure)
# Deployed apps use per-product prefixes (uploads-<product>/, processed-<product>/).
# Do not point local runs at these — see CLAUDE.md.
AZURE_STORAGE_ACCOUNT_NAME=
AZURE_STORAGE_ACCOUNT_KEY=
AZURE_INPUT_PREFIX=az://invoices/uploads-vetcostcheck/
AZURE_OUTPUT_PREFIX=az://invoices/processed-vetcostcheck/
```

- [ ] **Step 4: Update `CLAUDE.md`**

In the "Key Environment Variables" list, after the `STORAGE_BACKEND` bullet, add:

```markdown
- `CLEANUP_ARTIFACTS` — `true` (default) deletes the upload and per-subdocument artifacts from storage after a successful job; the result JSON survives and is expired by the container's 14-day lifecycle rule. Set to `false` for local work so artifacts stay inspectable.
```

In the "Commands" section, replace:

```markdown
There is no test suite or linter configured. Python 3.11+ (Dockerfile uses 3.11-slim).
```

with:

```markdown
Tests: `.venv/bin/python -m pytest tests/` (pytest 9.x). No linter configured. Python 3.11+ (Dockerfile uses 3.11-slim).
```

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all pass (unchanged from Task 1 — this task adds no tests, the wiring is verified end-to-end in Task 5).

- [ ] **Step 6: Commit**

```bash
git add core/jobs/tasks.py .env.example CLAUDE.md
git commit -m "$(cat <<'EOF'
feat: run artifact cleanup after successful extraction

process_file now calls cleanup_storage_artifacts between the extraction and
job_completed log lines, so failure paths keep their artifacts. Documents
CLEANUP_ARTIFACTS, points .env.example at per-product prefixes, and corrects
the stale "no test suite" note in CLAUDE.md.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Backlog selection logic and purge script

The selection rules live in `core/retention.py` as a pure function so they can be tested without touching Azure; the script is a thin CLI around it.

**Files:**
- Create: `core/retention.py`
- Create: `scripts/purge_blob_backlog.py`
- Test: `tests/core/test_retention.py` (create)

**Interfaces:**
- Produces: `core.retention.select_for_deletion(blobs, now, cutoff_days=14) -> list[str]` where `blobs` is an iterable of `(name: str, last_modified: datetime)` pairs with timezone-aware datetimes; and `core.retention.LEGACY_PREFIXES: tuple[str, ...]`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_retention.py`:

```python
"""Selection rules for the one-off blob backlog purge."""
from datetime import datetime, timedelta, timezone

from core.retention import select_for_deletion

NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def test_legacy_prefixes_go_regardless_of_age():
    blobs = [
        ("uploads/abc.pdf", NOW),                                    # uploaded seconds ago
        ("processed/abc_subdocument_1.png", NOW - timedelta(days=1)),
        ("processed//extracted_data_abc.json", NOW),
    ]
    assert set(select_for_deletion(blobs, now=NOW)) == {b[0] for b in blobs}


def test_recent_product_blobs_are_kept():
    blobs = [
        ("uploads-bps/abc.pdf", NOW - timedelta(days=2)),
        ("processed-vetcostcheck//extracted_data_abc.json", NOW - timedelta(days=13)),
        ("processed-sanierer/abc_subdocument_1.png", NOW),
    ]
    assert select_for_deletion(blobs, now=NOW) == []


def test_old_product_blobs_go():
    blobs = [
        ("uploads-bps/old.pdf", NOW - timedelta(days=15)),
        ("processed-vetcostcheck/old_subdocument_1.md", NOW - timedelta(days=90)),
    ]
    assert set(select_for_deletion(blobs, now=NOW)) == {b[0] for b in blobs}


def test_root_level_strays_follow_the_cutoff():
    old = ("230075012T_Splitt.pdf", NOW - timedelta(days=180))
    new = ("something_just_uploaded.pdf", NOW - timedelta(hours=1))
    assert select_for_deletion([old, new], now=NOW) == [old[0]]


def test_unknown_prefix_follows_the_cutoff():
    blobs = [
        ("uploads-garagenhub/new.pdf", NOW - timedelta(days=3)),
        ("uploads-garagenhub/old.pdf", NOW - timedelta(days=20)),
    ]
    assert select_for_deletion(blobs, now=NOW) == ["uploads-garagenhub/old.pdf"]


def test_cutoff_boundary_is_strict():
    exactly = ("uploads-bps/a.pdf", NOW - timedelta(days=14))
    just_over = ("uploads-bps/b.pdf", NOW - timedelta(days=14, seconds=1))
    assert select_for_deletion([exactly, just_over], now=NOW) == [just_over[0]]


def test_cutoff_days_is_configurable():
    blobs = [("uploads-bps/a.pdf", NOW - timedelta(days=5))]
    assert select_for_deletion(blobs, now=NOW, cutoff_days=3) == ["uploads-bps/a.pdf"]
    assert select_for_deletion(blobs, now=NOW, cutoff_days=30) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_retention.py -v`
Expected: all 7 FAIL with `ModuleNotFoundError: No module named 'core.retention'`.

- [ ] **Step 3: Write `core/retention.py`**

```python
"""Selection rules for the one-off blob backlog purge.

Kept separate from scripts/purge_blob_backlog.py so the rules can be unit-tested
without an Azure connection.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

# Prefixes belonging to the decommissioned pre-multi-product deployment. Nothing
# should write here any more, so everything under them goes regardless of age.
# The trailing slash matters: without it "uploads/" would also match
# "uploads-bps/" and friends.
LEGACY_PREFIXES = ("processed/", "uploads/")


def select_for_deletion(
    blobs: Iterable[tuple[str, datetime]],
    now: datetime,
    cutoff_days: int = 14,
) -> list[str]:
    """Return the names of the blobs that should be deleted.

    `blobs` yields (name, last_modified) pairs; last_modified must be
    timezone-aware, as the Azure SDK returns it.

    Rules:
      - under a legacy prefix: delete, whatever its age
      - anything else (per-product prefixes, root-level strays, prefixes added
        later): delete only if strictly older than `cutoff_days`
    """
    cutoff = now - timedelta(days=cutoff_days)
    doomed = []
    for name, last_modified in blobs:
        if name.startswith(LEGACY_PREFIXES) or last_modified < cutoff:
            doomed.append(name)
    return doomed
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_retention.py -v`
Expected: 7 passed.

- [ ] **Step 5: Write the purge script**

Create `scripts/purge_blob_backlog.py`:

```python
"""One-off purge of the blob backlog in the `invoices` container.

Dry-run by default; --apply actually deletes. Blob soft delete is enabled on the
account (7 days, allowPermanentDelete=false), so an --apply run is recoverable
within that window.

    python scripts/purge_blob_backlog.py              # report only
    python scripts/purge_blob_backlog.py --apply      # delete
"""
import argparse
import os
from collections import defaultdict
from datetime import datetime, timezone

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

from core.retention import select_for_deletion


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: dry run)")
    parser.add_argument("--cutoff-days", type=int, default=14)
    parser.add_argument("--container", default="invoices")
    args = parser.parse_args()

    load_dotenv()
    account = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
    key = os.environ["AZURE_STORAGE_ACCOUNT_KEY"]
    container = BlobServiceClient(
        f"https://{account}.blob.core.windows.net", credential=key
    ).get_container_client(args.container)

    blobs = [(b.name, b.last_modified, b.size or 0) for b in container.list_blobs()]
    doomed = set(select_for_deletion(
        [(name, modified) for name, modified, _ in blobs],
        now=datetime.now(timezone.utc),
        cutoff_days=args.cutoff_days,
    ))

    counts: dict[str, int] = defaultdict(int)
    sizes: dict[str, int] = defaultdict(int)
    for name, _, size in blobs:
        if name in doomed:
            bucket = name.split("/")[0] if "/" in name else "(root)"
            counts[bucket] += 1
            sizes[bucket] += size

    print(f"account={account} container={args.container} "
          f"cutoff_days={args.cutoff_days}")
    print(f"total={len(blobs)} selected={len(doomed)} keeping={len(blobs) - len(doomed)}")
    for bucket in sorted(counts):
        print(f"  {bucket:26} {counts[bucket]:5d} blobs  {sizes[bucket] / 1048576:8.1f} MB")

    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply to delete.")
        return

    deleted = failed = 0
    for name in sorted(doomed):
        try:
            container.delete_blob(name)
            deleted += 1
        except Exception as exc:  # keep going; report at the end
            failed += 1
            print(f"FAILED {name}: {exc}")
    print(f"\ndeleted={deleted} failed={failed}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Dry-run the script against production**

Run: `.venv/bin/python scripts/purge_blob_backlog.py`

Expected output shape — a report only, ending in `DRY RUN — nothing deleted`. Sanity-check against the inventory in the spec: `total` around 2,600+ (the container is live, so it drifts), `(root)` 1 blob, and every bucket showing only its blobs older than 14 days. **Do not pass `--apply` yet** — that happens in Task 5 after review.

**Note:** the code blocks above show the rule as originally planned, with an unconditional sweep of the legacy prefixes. That was revised during implementation — `select_for_deletion` now applies a single 14-day cutoff to every prefix, and `LEGACY_PREFIXES` no longer exists. See the spec's §3 revision note for why. Selected count is therefore ~1,958, not ~2,039 — the 81 in-window legacy blobs are spared, and the rest of the selection (1,705 legacy older than the cutoff + ~252 per-product + 1 root stray) is unchanged.

- [ ] **Step 7: Commit**

```bash
git add core/retention.py scripts/purge_blob_backlog.py tests/core/test_retention.py
git commit -m "$(cat <<'EOF'
feat: add blob backlog purge script with tested selection rules

core/retention.select_for_deletion holds the rules (legacy prefixes go whatever
their age; everything else follows the 14-day cutoff) as a pure function.
scripts/purge_blob_backlog.py is a dry-run-by-default CLI around it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Apply the lifecycle rule and quiet the legacy app pair

Infrastructure only — no code. Requires an authenticated `az` session on tenant `eb15ebac-a9e7-43c7-bd1c-b99f13afdda9`.

**Files:**
- Create: `infra/lifecycle-policy.json` (checked in, so the applied policy is reviewable in git)
- Modify: `.gitignore` — `*.json` is ignored with an allowlist, so this file needs an exception

- [ ] **Step 1: Allow the policy file past `.gitignore`**

`.gitignore` line 52 ignores `*.json` (an API-key precaution) with a `!`-allowlist below it. Add to that allowlist, after `!products/*/analyze_schema.json` (line 58):

```
!infra/lifecycle-policy.json
```

Verify: `git check-ignore -v infra/lifecycle-policy.json` should print nothing once the file exists (Step 2). Without this, `git add` in Step 7 silently refuses the file.

- [ ] **Step 2: Write the policy file**

Create `infra/lifecycle-policy.json`:

```json
{
  "rules": [
    {
      "enabled": true,
      "name": "expire-invoice-artifacts-14d",
      "type": "Lifecycle",
      "definition": {
        "actions": {
          "baseBlob": {
            "delete": { "daysAfterModificationGreaterThan": 14 }
          }
        },
        "filters": {
          "blobTypes": ["blockBlob"],
          "prefixMatch": ["invoices/"]
        }
      }
    }
  ]
}
```

Versioning is disabled on the account, so no `snapshot` or `version` actions are needed.

- [ ] **Step 3: Confirm no policy exists yet**

Run:
```bash
az storage account management-policy show \
  --account-name 3cixstorage -g 3c_information_extraction -o json
```
Expected: `ManagementPolicyNotFound`. If a policy *does* exist, stop and reconcile it with this one rather than overwriting blind.

- [ ] **Step 4: Apply the policy**

```bash
az storage account management-policy create \
  --account-name 3cixstorage -g 3c_information_extraction \
  --policy @infra/lifecycle-policy.json
```

- [ ] **Step 5: Read the policy back**

```bash
az storage account management-policy show \
  --account-name 3cixstorage -g 3c_information_extraction \
  --query "policy.rules[0].{name:name, enabled:enabled, days:definition.actions.baseBlob.delete.daysAfterModificationGreaterThan, prefixes:definition.filters.prefixMatch}" -o json
```
Expected: `name: expire-invoice-artifacts-14d`, `enabled: true`, `days: 14`, `prefixes: ["invoices/"]`.

- [ ] **Step 6: Scale the legacy API to zero**

`ca-invoice-api` has been idle since the 2026-07-21 domain cutover but still runs a replica around the clock on pre-cleanup code. Scaling to zero stops the billing and stops it writing legacy artifacts, while leaving the app and its config in place.

```bash
az containerapp update -n ca-invoice-api -g rg-3c-invoice --min-replicas 0
az containerapp show -n ca-invoice-api -g rg-3c-invoice \
  --query "properties.template.scale.minReplicas" -o tsv
```
Expected: `0`. (`ca-invoice-worker` is already at 0 — leave it.)

- [ ] **Step 7: Point local dev away from production storage**

Edit the local `.env` (untracked, so this is a manual machine-local change):

```
STORAGE_BACKEND=local
CLEANUP_ARTIFACTS=false
```

Leave the `AZURE_*` values in place for occasional Azure-backed runs, but if you do switch `STORAGE_BACKEND=azure`, change the prefixes to the per-product ones first. With the old legacy prefixes plus cleanup enabled, a local run would delete blobs in production storage.

Verify: `grep -E "STORAGE_BACKEND|CLEANUP_ARTIFACTS" .env`

- [ ] **Step 8: Commit the policy file**

```bash
git add .gitignore infra/lifecycle-policy.json
git commit -m "$(cat <<'EOF'
chore: check in the invoices container lifecycle policy (14-day expiry)

Applied to 3cixstorage as expire-invoice-artifacts-14d. Checked in so the
applied policy is reviewable and re-appliable.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Deploy, verify end-to-end, purge the backlog

The verification the spec calls for. Nothing here is committed; it is a runbook whose output is the evidence that the feature works.

**Files:** none modified.

- [ ] **Step 1: Deploy all three product pairs with a unique tag**

```bash
./deploy.sh all v20260729
```
`latest` would silently skip creating a new revision, so the explicit tag matters. Expected: three ACR builds, six `containerapp update` calls, no errors.

- [ ] **Step 2: Confirm `CLEANUP_ARTIFACTS` is unset on the deployed apps**

```bash
for app in ca-api-vetcostcheck ca-worker-vetcostcheck ca-api-bps ca-worker-bps ca-api-sanierer ca-worker-sanierer; do
  echo -n "$app: "
  az containerapp show -n $app -g rg-3c-invoice \
    --query "properties.template.containers[0].env[?name=='CLEANUP_ARTIFACTS'].value|[0]" -o tsv
done
```
Expected: empty for all six — the code default is `true`, so cleanup is on. If any prints `false`, remove that env var.

- [ ] **Step 3: Run a real job and confirm only the JSON survives**

Pick a test PDF from `test_uploads/vetcostcheck/`. List the prefix, run the job, list again:

```bash
set -a && source .env && set +a
az storage blob list --account-name 3cixstorage --account-key "$AZURE_STORAGE_ACCOUNT_KEY" \
  --container-name invoices --prefix processed-vetcostcheck/ --query "length(@)" -o tsv
```

Submit through the deployed API (`https://3cvetcostcheck.flex-capital-scale.com`) with `X-Api-Key`: `POST /upload`, `POST /process`, poll `GET /job/{job_id}` until `finished`. Then:

```bash
az storage blob list --account-name 3cixstorage --account-key "$AZURE_STORAGE_ACCOUNT_KEY" \
  --container-name invoices --prefix uploads-vetcostcheck/ \
  --query "[?contains(name, '<file_id>')].name" -o tsv
az storage blob list --account-name 3cixstorage --account-key "$AZURE_STORAGE_ACCOUNT_KEY" \
  --container-name invoices --prefix processed-vetcostcheck/ \
  --query "[?contains(name, '<file_id>')].name" -o tsv
```
Expected: the uploads query returns nothing; the processed query returns exactly one blob, `processed-vetcostcheck//extracted_data_<file_id>.json`. No `_subdocument_*.md/.pdf/.png`. Confirm the JSON's contents match the `result` the API returned.

- [ ] **Step 4: Confirm a failed job keeps its artifacts**

Upload a deliberately corrupt PDF (e.g. `head -c 2000 <some>.pdf > /tmp/broken.pdf`) and process it. Expected: `GET /job/{job_id}` reports `failed`; the upload blob for that `file_id` is still in `uploads-vetcostcheck/`; Sentry shows the `job_failed` event.

- [ ] **Step 5: Confirm the flag switches cleanup off**

Cleanup runs in the **worker**, inside `process_file` — so the flag has to be set on the worker process, and the run has to go through the queue. `main.py` does not call `cleanup_storage_artifacts()` at all, so it cannot verify this (and must not be wired up to: under `LocalStorage`, `materialize_to_local` returns the source path unchanged, so cleanup would `unlink` the test PDF in `3C_testdaten_pdf/`).

Use the native-mode stack from CLAUDE.md:

```bash
docker compose up redis -d

# Terminal 1 — API
STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 \
  uvicorn core.api.main:app --host 0.0.0.0 --port 8000 --log-level debug

# Terminal 2 — worker, with cleanup disabled
STORAGE_BACKEND=local CLEANUP_ARTIFACTS=false REDIS_URL=redis://localhost:6379/0 \
  .venv/bin/python -m core.jobs.worker

# Terminal 3
.venv/bin/python test_api.py
```

Expected with the flag off: the worker logs `artifact_cleanup_skipped`, and `./temp/` still holds the sub-PDFs, page images and markdown.

Then restart the worker with the flag removed (code default is `true`), re-run `test_api.py`, and expect `artifact_cleanup_completed` with `deleted` > 0 and only `extracted_data_*.json` left in `./temp/`.

- [ ] **Step 6: Dry-run the purge, then apply it**

```bash
.venv/bin/python scripts/purge_blob_backlog.py
```
Review the per-bucket counts against the spec's inventory. Then:

```bash
.venv/bin/python scripts/purge_blob_backlog.py --apply
```
Expected: `failed=0`. Soft delete keeps everything recoverable for 7 days if the numbers turn out wrong.

- [ ] **Step 7: Confirm the end state**

```bash
set -a && source .env && set +a
az storage blob list --account-name 3cixstorage --account-key "$AZURE_STORAGE_ACCOUNT_KEY" \
  --container-name invoices --num-results 5000 \
  --query "[].{n:name,m:properties.lastModified}" -o tsv | awk '{split($1,a,"/"); print a[1]}' | sort | uniq -c | sort -rn
```
Expected: no root-level stray, and every bucket — legacy and per-product alike — holding only blobs from the last 14 days. The legacy `processed` and `uploads` buckets do **not** vanish: about 81 blobs there are still inside the window and survive this sweep, ageing out over the following week (the lifecycle rule expires them without further action). Total well under the 2,521 blobs / 2.38 GB starting point.

**Note:** this expectation reflects the revised uniform-cutoff rule. The unconditional legacy sweep the plan originally described was dropped after the dry run's date ranges showed 81 legacy blobs inside the retention window — see the spec's §3 revision note.

---

## Notes on what is deliberately not here

- **No change to `result_ttl`.** Results still leave Redis after one hour; the design did not touch delivery.
- **The double-slash key quirk stays.** Result JSONs land at `processed-<product>//extracted_data_*.json` because the env prefix ends in `/` and `core/pipeline.py:358` adds another. Harmless in a flat namespace and the lifecycle prefix still matches.
- **Deleting the legacy app pair is deferred.** Task 4 scales it to zero; removing `ca-invoice-api` / `ca-invoice-worker` is a separate decision once you're satisfied nothing calls their `azurecontainerapps.io` FQDN.
- **`test_uploads/` is gitignored** and holds 309 real customer PDFs downloaded on 2026-07-29. It is test input, not part of this plan.
