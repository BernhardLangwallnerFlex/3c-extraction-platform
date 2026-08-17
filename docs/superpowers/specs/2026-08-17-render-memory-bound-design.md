# Bounding page-render memory — design

**Date:** 2026-08-17
**Products:** vetcostcheck, bps, sanierer (shared core — all three)
**Origin:** BPS production OOM 2026-08-17, reported by the PO as
*"Work-horse terminated unexpectedly; waitpid returned 9 (signal 9)"*.

## Problem

A five-page BPS document killed its worker three times in a row — 09:08:33, 09:15:37 and
09:20:24 — each attempt ending in SIGKILL. SIGKILL cannot be caught, so nothing in the
application could react; RQ simply reaped the corpse and burned two retries reaching the same
outcome.

The document is 16.9 MB, and four of its five pages are enormous:

| Page | Size (pt) | At 200 dpi | Mpx |
|---|---|---|---|
| 1 | 595 × 841 | 1653 × 2338 | 3.9 |
| 2 | 4554 × 6516 | 12650 × 18101 | 229.0 |
| 3 | 4177 × 6095 | 11604 × 16930 | 196.5 |
| 4 | 4554 × 6516 | 12650 × 18101 | 229.0 |
| 5 | 4177 × 6095 | 11604 × 16930 | 196.5 |

Pages 2–5 are roughly **1.6 × 2.3 metres** — about four times A0. Large-format scans, the kind
a Sachverständiger produces for plans or wide damage documentation.

`split_document_into_invoices` (`core/pipeline.py:305-320`) renders every page of a
subdocument at a **fixed 200 dpi**, accumulates all of them in a list, and only then allocates
the concatenated canvas:

- concatenated canvas: 854.7 Mpx = **2.56 GB** as RGB
- every page pixmap still held in `page_images` at that moment: another **2.56 GB**
- peak ≈ **5.1 GB** against the worker's **4 GiB** limit

The timing confirms it: `analysis_completed` at 09:15:28, killed at 09:15:37 — nine seconds
later, inside the render loop.

**Independent corroboration:** Mistral OCR rejected the same document with
`"Image pixels are above the allowed limits"` (status 400, code 3310), so DualOCR degraded to
Azure-only. With the code currently on test, this document would also come back flagged
`SINGLE_ENGINE_OCR` — the first real-world firing of that flag. It does not prevent the crash.

**This is not the same bug as the signal-15 reports.** Those are KEDA scaling the worker in
while a job runs (see *Out of scope*). The PO sent both on the same day; they share a symptom
string and nothing else.

## Decision

Bound the rendered pixel count instead of fixing the dpi, and never hold more than one page
pixmap at a time. Both changes are invisible to documents that fit today.

## Architecture

### Layer 1 — `core/rendering.py` (new)

One function, no I/O of its own, no product knowledge:

**`render_dpi_for(pages, base_dpi, budget_px) -> int`** — returns the dpi at which the total
rendered area of `pages` stays within `budget_px`. Returns `base_dpi` unchanged when the pages
already fit, so the common case is bit-for-bit what it is today. Otherwise scales by
`sqrt(budget_px / total_px_at_base_dpi)` — area scales with the square of dpi — and floors the
result at `MIN_DPI` so a pathological input still produces something legible rather than a
1-pixel smear.

Constants, with the reasoning that fixes their values:

- **`CANVAS_BUDGET_PX = 200_000_000`** (200 Mpx). Chosen from measurement, not taste: the
  largest *whole file* in the three regression corpora is BPS_3 at **141.8 Mpx** at 200 dpi,
  and real subdocuments are subsets of a file. 200 Mpx therefore leaves every document in the
  corpora untouched while cutting the failing document by roughly 4×. This margin is the
  reason the regression sweep can be expected to be unchanged — verify, do not assume.
- **`MIN_DPI = 40`.** Below this, text on a scanned page stops being readable to the vision
  model, and returning something unusable is worse than returning something small.

### Layer 2 — bounded, streaming concatenation

`core/pipeline.py:split_document_into_invoices` changes in two ways:

1. Compute the subdocument's dpi once via `render_dpi_for(...)` before the loop, and render
   every page of that subdocument at it. One scale for the whole subdocument keeps pages
   visually consistent within the concatenated image.
2. **Paste and free.** Compute the canvas dimensions in a first pass over page sizes (cheap —
   `page.rect` needs no rasterisation), allocate the canvas once, then render each page, paste
   it, and drop the pixmap before rendering the next. Peak memory becomes *canvas + one page*
   instead of *canvas + every page*.

Together these turn the failing document's ~5.1 GB into roughly 600 MB, and they cap every
future document at approximately `CANVAS_BUDGET_PX × 3` bytes plus one page — bounded by
construction rather than by hoping documents stay small.

### Layer 3 — the other render sites

Four call sites rasterise pages today:

| Site | dpi | Purpose |
|---|---|---|
| `core/pipeline.py:311` | 200 | subdocument image for extraction — **the crash** |
| `core/pipeline.py:211` | 150 | one image per page for `analyze_document` |
| `core/pipeline.py:140` | 150 | audit before implementation |
| `core/utils.py:190` | 150 | audit before implementation |

The analyze site (`:211`) has the same unbounded flaw — it rendered the failing document's
five pages at 150 dpi (≈480 Mpx across five separate PNGs) and survived only because the
images are encoded and released one at a time rather than concatenated. It must use the same
helper, with the budget applied **per image** rather than to a canvas, since these are sent as
separate images.

The remaining two sites are to be read and either converted or explicitly documented as
bounded during implementation. Leaving an unbounded `get_pixmap` in the codebase after this
work would defeat its purpose.

## What this does not change

No API contract change, no new field, no change to `returncode`, `qualityFlags` or `warnings`.
A document that renders within budget produces byte-identical images to today. Nothing to
communicate to the consumer.

**No `qualityFlags` entry for downscaling.** A page rendered at 2733 px instead of 12650 px
loses nothing the vision model would have used — the API downsamples large images regardless —
so this is not a degradation worth reporting. Adding a flag would train the consumer to treat a
normal result as suspect.

## Release

Ships **in the same release** as the returncode and content-policy work, which is on the test
tier as `v20260817a` and not yet promoted. This adds no contract change, so it does not
complicate 3C's integration, and it fixes a live production crash — there is no argument for
holding it back for a separate cycle.

## Testing

**Unit — `render_dpi_for`:**
- Pages already within budget return `base_dpi` unchanged (the no-op guarantee that keeps
  existing documents identical).
- A single oversized page scales down; the resulting total area is ≤ budget.
- Many normal pages that collectively exceed the budget scale down.
- The result never goes below `MIN_DPI`, even for absurd input.
- An empty page list returns `base_dpi` rather than dividing by zero.
- The real failing geometry — 1 A4 page plus 4 pages of 4554 × 6516 pt — yields a dpi whose
  total area is within budget.

**Unit — streaming concatenation:** a multi-page subdocument produces a canvas of the expected
dimensions, and the same visual result as the previous implementation for a small document.
Assert that no more than one page pixmap is alive at a time — via a counting fake around the
render call, not by inspecting memory.

**Regression (the gate that matters):** `scripts/returncode_sweep.py --expect 100` over all
three corpora must stay green, and — because this touches the image every extraction sees —
`scripts/regression_check.py` should be run as well if its references are still valid.

**Acceptance (manual, maintainer's machine):** the document that caused this,
`~/Downloads/bps-oom-20260817-c16e5672.pdf` (5 pages, 854.7 Mpx at 200 dpi), must complete
through `PRODUCT_NAME=bps STORAGE_BACKEND=local scripts/extract_local.py` without being killed,
and must return at least one subdocument. It is customer data and is not committed; the upload
survives in `uploads-bps/` because cleanup runs only on success, and expires under the 14-day
lifecycle rule.

## Out of scope

- **The KEDA scale-in that produces the signal-15 reports.** A separate root cause: RQ removes
  a job from the queue list the moment it is dequeued, so a running job looks like an empty
  queue to a `listLength` trigger, and 30-second polling misses the brief re-enqueue windows
  on retry, so the 300 s cooldown never resets. Measured at ~15 kills per 244 job attempts
  over 30 days. Needs its own spec and a scaling-rule decision.
- **Raising the worker memory limit.** It moves the threshold rather than removing it — a
  larger document still kills the worker — and 4 GiB across five replicas per product is real
  money. Bounding the render is the fix; the limit is the backstop.
- **Rejecting oversized documents at upload.** The point of this change is that they now
  process successfully.
- **Mistral's pixel-limit rejection.** Real, already handled by the existing DualOCR fallback,
  and now visible to the consumer as `SINGLE_ENGINE_OCR`. Sending Mistral a downscaled image
  is a separate question about OCR quality, not about this crash.
