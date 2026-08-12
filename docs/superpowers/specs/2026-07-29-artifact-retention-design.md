# Artifact Retention & Cleanup — Design

**Date:** 2026-07-29
**Status:** approved, ready for implementation planning

## Problem

Blob storage was meant to hold processed documents temporarily. It never deleted anything. As of
2026-07-29 the `invoices` container in storage account `3cixstorage` holds **2,521 blobs / 2.38 GB**,
the oldest from 2026-01-29:

| Prefix | Blobs | Size | First | Last |
|---|---|---|---|---|
| `processed/` (legacy) | 1,589 | 1,613.7 MB | 2026-01-29 | 2026-07-21 |
| `uploads/` (legacy) | 197 | 416.9 MB | 2026-03-23 | 2026-07-21 |
| `processed-vetcostcheck/` | 291 | 154.8 MB | 2026-05-29 | 2026-07-29 |
| `uploads-vetcostcheck/` | 36 | 35.8 MB | 2026-05-29 | 2026-07-29 |
| `processed-bps/` | 275 | 89.4 MB | 2026-05-29 | 2026-07-28 |
| `uploads-bps/` | 62 | 38.2 MB | 2026-05-29 | 2026-07-28 |
| `processed-sanierer/` | 56 | 26.3 MB | 2026-05-29 | 2026-06-02 |
| `uploads-sanierer/` | 14 | 1.6 MB | 2026-05-29 | 2026-06-02 |
| `230075012T_Splitt.pdf` (root stray) | 1 | 0.3 MB | 2026-01-29 | 2026-01-29 |

Content mix: 945 PDFs, 637 PNGs (page renders, ~1.3 MB each — the bulk of the bytes), 637 OCR
markdown files, 299 result JSONs. The container is live, so counts drift: three more vetcostcheck
uploads landed within minutes of this listing.

This is customer invoice data with personal information, retained indefinitely for no operational
reason. Production is now stable, so artifacts should be deleted once processing succeeds.

## Current behavior

Per job the pipeline writes:

- one upload blob under `uploads-<product>/` (`core/storage/file_storage.py:save_upload`)
- per subdocument: `.md`, `.pdf`, `.png` under `processed-<product>/` (`core/pipeline.py:222`)
- one `extracted_data_<stem>.json` (`core/pipeline.py:358`)

Nothing in the codebase ever reads a processed artifact back — blob writes are write-only. Results
reach consumers solely through `GET /job/{job_id}`, backed by the RQ result in Redis with
`result_ttl=3600` (one hour, `core/api/routes/process.py:31`).

`Pipeline.cleanup_temporary_files()` (`core/pipeline.py:361`) already deletes subdoc `.md`/`.pdf`/`.png`
from storage, but **nothing calls it**. `core/jobs/tasks.py:136` calls only `cleanup_local()`, which
removes the local working directory. The dead method also leaves the upload blob and the result JSON,
and its deletes are unguarded.

## Decisions

| Question | Decision |
|---|---|
| What gets deleted | Everything, but result JSONs get a short window |
| On job failure | Keep artifacts; cleanup runs only after success |
| Retention window | 14 days, one window for both JSONs and failed-job artifacts |
| External consumers | None automated; Bernhard pulls artifacts manually for debugging |
| Mechanism | App-level cleanup on success **plus** an Azure lifecycle rule as safety net |
| Existing backlog | Apply the 14-day cutoff retroactively, uniformly across every prefix (revised — see §3) |

Rejected alternatives: a lifecycle rule alone (never delivers "delete after processing", and lifecycle
filters match prefixes only, so it cannot distinguish an intermediate PNG from a result JSON); a
reaper job in code instead of the lifecycle rule (reimplements a free Azure feature, and needs a
scheduler because the worker scales to zero).

## Design

### 1. App-level cleanup on success

Replace the dead `cleanup_temporary_files()` in `core/pipeline.py` with
`cleanup_storage_artifacts()`, which deletes per job:

- `self.file_key` — the upload blob
- for each subdocument: `md_key`, `pdf_key`, `image_key`

It does **not** delete `extracted_data_<stem>.json`. That is the single survivor; the lifecycle rule
expires it at 14 days.

Two differences from the dead method: the upload blob is included, and each delete is individually
guarded. `AzureBlobStorage.delete()` (`core/storage/storage.py:212`) raises `ResourceNotFoundError`
for a missing blob, so an unguarded loop would abort partway and leave the remaining artifacts behind.

Storage cleanup stays separate from `cleanup_local()`. Local tmp cleanup continues to run in the
`finally` block on every outcome; storage cleanup runs only on success.

In `core/jobs/tasks.py`, call it inside the `try`, after `extract_data_from_subdocuments()` returns
and before the `job_completed` log line — so the log reflects a fully finished job, and every
exception path skips cleanup. RQ's `Retry(max=2)` still finds the upload, because retries only happen
on failure.

**Flag:** `CLEANUP_ARTIFACTS`, default `true` in code. `.env.example` documents setting it to `false`
for local work, so `python main.py` does not delete the sub-PDFs and page images being inspected.

**Accepted behavior change:** re-processing a `file_id` after a successful run no longer works. The
upload is gone, so `materialize_to_local()` in `Pipeline.__init__` raises and the job fails. This is
left as a plain failure — no special error path. Manual re-runs now mean re-uploading.

### 2. Lifecycle rule

One rule on `3cixstorage` covering the whole container:

```json
{"rules": [{
  "enabled": true, "name": "expire-invoice-artifacts-14d", "type": "Lifecycle",
  "definition": {
    "actions": {"baseBlob": {"delete": {"daysAfterModificationGreaterThan": 14}}},
    "filters": {"blobTypes": ["blockBlob"], "prefixMatch": ["invoices/"]}
  }}]}
```

Applied with `az storage account management-policy create`. It covers what app-level cleanup cannot:
failed jobs, orphaned uploads (`/upload` called but never `/process`), result JSONs, and blobs left by
a crashed worker.

Caveat: the rule evaluates roughly once per day, so 14 days is 14–15 in practice.

Verified 2026-07-29: the account has **no** management policy today, and blob versioning is disabled,
so the rule needs no `snapshot`/`version` delete actions.

### 3. Backlog sweep

`scripts/purge_blob_backlog.py` — dry-run by default, `--apply` to execute, matching the convention of
the existing VCC cutover script. It applies **one uniform rule**: delete any blob strictly older than
14 days, whatever its prefix. That covers the legacy `processed/` and `uploads/` prefixes, the six
per-product prefixes, the root-level stray `230075012T_Splitt.pdf`, and any prefix added later.

**Revised 2026-07-29, after implementation.** The original design deleted everything under the legacy
prefixes unconditionally, on the stated premise that nothing writes there any more. That premise was
wrong: 81 of the 1,786 legacy blobs are newer than 14 days — real production results from the legacy
app pair's final days of service before the 2026-07-21 domain cutover. An unconditional sweep would
have destroyed results that the 14-day rule keeps, so the two rules contradicted each other. The
uniform cutoff resolves it, and removes the special case that carried the risk: `select_for_deletion`
now has a single branch and no prefix allowlist. The legacy prefixes still empty out — the 81 recent
blobs age past the cutoff within a week, and the lifecycle rule expires them regardless.

The dry run prints per-prefix counts, bytes and date ranges for review before `--apply`. The date
ranges are what exposed the contradiction above, so they are load-bearing rather than cosmetic.

Dry run under the uniform rule (2026-07-29): **total 2,622 · selected 1,958 · keeping 664** — 1,705
legacy blobs past the cutoff, ~252 per-product, 1 root stray. The container is live, so these drift.

Local copies of the 194 real legacy uploads were downloaded to `test_uploads/` on 2026-07-29 before
this design was written, so the sweep does not lose usable test data. (Three 16-byte
`das ist ein Test` placeholder blobs were found in that prefix and discarded.)

Blob soft delete is already enabled (verified 2026-07-29): 7-day blob retention, 7-day container
retention, `allowPermanentDelete: false`. The purge is therefore recoverable for 7 days with no
further setup.

### 4. Stop the local-dev leak

Local `.env` has `STORAGE_BACKEND=azure` with `AZURE_INPUT_PREFIX="az://invoices/uploads/"` and
`AZURE_OUTPUT_PREFIX="az://invoices/processed/"`. Local runs therefore write into production storage
under the legacy prefixes.

Point local work at local storage (`STORAGE_BACKEND=local`, as CLAUDE.md's native-mode instructions
already prescribe) and reserve the Azure prefixes for deployed apps. Without this the legacy prefix
refills after every purge, and once cleanup ships, local runs would be deleting blobs in production
storage.

### 5. The legacy Container App pair

Verified 2026-07-29 in `rg-3c-invoice`:

- `ca-invoice-api` (minReplicas **1**, running) and `ca-invoice-worker` (minReplicas 0) still carry
  `AZURE_INPUT_PREFIX=az://invoices/uploads/` and `AZURE_OUTPUT_PREFIX=az://invoices/processed/`
- neither has a custom domain; `3cvetcostcheck` / `3cbps` / `3csanierer.flex-capital-scale.com` are
  all bound to the per-product APIs
- the six per-product prefixes are correctly configured on their respective app pairs
- legacy blob writes stop on 2026-07-21, the date of the domain cutover — so the pair has been idle
  since, but is still reachable on its `azurecontainerapps.io` FQDN

The legacy pair is the reason the legacy prefixes filled up, not local dev runs. It runs pre-cleanup
code, so while it is up it can still write artifacts that nothing deletes.

**Action:** scale `ca-invoice-api` to `minReplicas=0`. That stops the 24/7 billing and stops it
writing legacy artifacts, while keeping both apps and their configuration in place — if an unknown
caller is still hitting the `azurecontainerapps.io` FQDN it will fail visibly rather than silently
losing its target. Deleting the pair outright is deferred to a later decision.

## Error handling

Cleanup never fails a job. Every delete is wrapped; failures log `artifact_delete_failed` with the
blob key and continue. If *all* deletes for a job fail — indicating a credential or permission
problem rather than a missing blob — one `sentry_sdk.capture_message` at warning level fires, matching
how DualOCR degradation is already reported. The job returns its result either way.

## Verification

There is no test suite in this repo, so verification is manual and evidence-based:

1. One real job against Azure with `CLEANUP_ARTIFACTS=true` — list the product prefix before and
   after; confirm only `extracted_data_*.json` remains and its content matches the API response.
2. One deliberately failing job (corrupt PDF) — confirm upload and intermediates survive and the
   failure reaches Sentry.
3. One job with `CLEANUP_ARTIFACTS=false` — confirm nothing is deleted, proving the flag works before
   it goes near production.
4. Backlog script dry run — compare its counts against the 2,521 blobs / 2.38 GB inventory above,
   then `--apply` and re-list to confirm what remains.
5. Read the lifecycle policy back with `az storage account management-policy show`.

## Blockers

None. The `az` session was re-authenticated on 2026-07-29 and all control-plane reads are done; soft
delete turned out to be already enabled, so nothing is outstanding before implementation.

## Out of scope

- The double-slash key quirk: result JSONs land at `processed-<product>//extracted_data_*.json`
  because the env prefix ends in `/` and `core/pipeline.py:358` adds another. Harmless in a flat blob
  namespace, and lifecycle prefix matching still works. Not fixed here.
- Any change to `result_ttl` or how results are delivered to consumers.
- Retention policy for anything outside the `invoices` container.
