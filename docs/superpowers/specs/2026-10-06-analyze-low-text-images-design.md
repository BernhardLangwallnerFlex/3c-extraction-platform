# Analyze step: skip images of low-text pages, cap images at 50

**Date:** 2026-10-06 · **Status:** design approved in conversation, experiment first · **Products:** BPS, Sanierer (VCC exempt)

## Problem

BPS documents with more than 50 pages fail in production. The analyze call sends one `detail: low` image per page in a single Azure OpenAI request, and gpt-5.4 accepts at most 50 images:

```
400 'Too many images in request: 51, maximum allowed: 50.'
```

Measured Sep 7 – Oct 5 2026: 10 of 385 BPS runs (51–94 pages) failed this way, **after** DualOCR had already processed every page (~10% of BPS OCR spend wasted). This is almost certainly what 3C reported as ">50-page Sanierer documents failing" — prod Sanierer had zero jobs in that window.

A large share of BPS pages are photos (damage photos, ~⅓ of pages). They carry little or no OCR text, get no extraction call, and in analyze cost image tokens while adding little to the page grouping.

## Goals and success criteria

1. BPS/Sanierer documents with more than 50 pages complete instead of failing.
2. On normal-size documents, **text pages are grouped into the same Belege as today**. Changes in how photo pages are assigned are allowed but reviewed manually.
3. VCC's analyze request is byte-identical to today for every document it can process today.
4. Fewer analyze tokens — a side effect, not a goal.

Out of scope: skipping OCR for low-text pages (running the engines sequentially), any change to extraction, the API, or the queue.

## Design

All changes live in `Invoice.analyze_document` (`core/pipeline.py`) and `ProductConfig` (`core/product.py`).

### 1. Per-product switch

New `ProductConfig` field:

```python
analyze_low_text_threshold: int | None = None
```

- BPS and Sanierer: set to the threshold the experiment confirms (starting value **150**).
- VCC: `None` → current behaviour, current prompt, unchanged.

### 2. Low-text page classification

After OCR, per page in `markdown_by_page`:

- strip Mistral image placeholders (`![...](...)`) and whitespace;
- `chars = len(remaining text)`;
- the page is **low-text** if `chars < analyze_low_text_threshold`.

Pure function, unit-tested on its own (`count_ocr_chars(page_markdown) -> int`).

### 3. Prompt construction (switch on)

- **Page text section:** every page keeps its `--- PAGE N ---` section and whatever OCR text it has. Low-text pages additionally get the marker:
  `[Kaum Text erkannt (N Zeichen) – vermutlich Foto oder Leerseite. Bild nicht mitgesendet.]`
- **Images:** sent only for pages that are not low-text, each preceded by a text block `Seite N:`. The label is required: once images are skipped, position no longer identifies the page.
- Low-text pages are **marked, not removed**. The model still sees every page and can assign it (e.g. a last page with only a total).

### 4. Hard cap of 50 images (all products)

If the number of images to send exceeds 50, **send no images** (text-only analyze, the same request shape the content-policy fallback already produces via `call_with_vision_fallback`) and log a warning `analyze_images_capped` with `pages`, `images_planned`.

- Applies to **all products**, including VCC: it only fires where the request would otherwise be rejected with a 400, so it changes no request that succeeds today.
- All-or-nothing rather than "best 50": simpler, and text-only analyze is already a proven path.

### 5. Telemetry

`analyze_llm_call` gains `images_sent` and `pages_low_text`. `analyze_images_capped` as above.

## Validation before building into the API

Script `scripts/analyze_image_experiment.py`, local only (`STORAGE_BACKEND=local`, `PRODUCT_NAME=bps|sanierer`).

**Corpus**

- `bps_sanierer_input/BPS_Input` (8) and `bps_sanierer_input/Sanierer_Input` (8) — normal-size documents.
- `test_uploads/BPS_Documents_Large` (6 documents, 52 / 53 / 54 / 58 / 114 / 121 pages) — the failure case.

Both directories are gitignored; nothing customer-shaped is committed.

**Steps**

1. **OCR once per document, cached** to disk (markdown per page). A and B then see identical input; OCR is paid once (~€6 for ~600 pages).
2. **Threshold calibration:** histogram of `chars` per page across the corpus; list every page with 50–300 chars with a rendered thumbnail path for visual review. Pick the threshold from this.
3. **Analyze A/B** on normal-size documents, 3 runs each:
   - A = current prompt (all images), B = new prompt.
   - Compare per document: Beleg assignment of text pages (must match A, modulo A's own run-to-run variance), assignment of low-text pages (report differences), run-to-run stability, analyze tokens.
4. **Large documents:** A fails by construction, so run B only — analyze, then the full split + extraction — and record: completes yes/no, Belege found, images sent, whether the cap fired, tokens. Manual check of the splits against the PDFs.
5. Summary table to stdout, raw results as JSON under `temp/analyze_image_experiment/`.

**Go / no-go**

- Go if: text-page grouping matches A on all normal documents (allowing differences A also shows across its own runs), all 6 large documents complete with plausible splits, and the manual review of changed photo-page assignments finds no regression.
- Otherwise: adjust the threshold or marker wording and rerun, or fall back to the cap alone (item 4) without the low-text skipping.

Estimated cost: ~€6 OCR + ~€3 LLM.

## Rollout

1. Experiment → go/no-go (results recorded in this spec).
2. Implement items 1–5 test-first: `count_ocr_chars`, prompt construction with/without switch (VCC request unchanged — assert on the built `content_blocks`), the cap.
3. Deploy to test **after** the page-rescaling change currently on test has been promoted, so the two are not mixed.
4. Rerun one large document against the BPS test tier, then `scripts/promote.sh bps <tag>` (and sanierer).
5. Tell 3C: >50-page documents now process; ask whether they were sending Sanierer documents to the BPS endpoint.

## Risks

- **Photo-page assignment changes** — the model no longer sees photos it might have attached to a Beleg. Measured in step 3; changed assignments are reviewed.
- **Misclassified pages** (a real page with little or badly OCR'd text) — still in the prompt as text with the marker, only the image is missing. Calibration list in step 2 covers this.
- **Text-only analyze on very long documents** loses visual boundary cues; only used where the request would otherwise fail outright.

## Results (2026-10-07)

**Threshold: 200** (not 150). Calibration over 549 pages (7 BPS, 6 large BPS, 7 Sanierer): pages under 150 chars are photos, letter sign-offs and letterhead footers; 150–200 adds DEKRA "Fotoanlage" photo pages (≈196 chars of header text) and scale/ruler pages. Pages from 200 up are real documents (emails, letters, invoice line items). The one real page under 200 (a 153-char "Dummy-Dokument" note) keeps its text with the marker.

**Finding: the large documents are text-dense, not photo albums.** Text pages at T=200: 44 / 42 / 44 / 50 / 110 / 114. The 114- and 121-page documents go text-only via the 50-image cap; the other four fit under the limit with images.

**A/B on normal-size documents** (analyze only, 3 runs each, identical cached OCR):

| | Docs | Text grouping = A | Notes |
|---|---|---|---|
| BPS | 7 | 7/7 | BPS_3 (22 of 26 pages photos): same grouping, photo pages placed identically, analyze tokens −40% (10.4K → 6.2K). BPS_6: −7%. |
| Sanierer | 7 | 6/7 | No Sanierer test document has a low-text page. The one difference (5926210150, 19 pages) persists with a **byte-identical** request (same tokens as A) — model run-to-run variance, not this change. |

First run showed a second difference (BPS_7) caused by the `Seite N:` labels alone on a document with no skipped page. **Design change:** labels are now added only when at least one image is skipped, so documents without photo pages send exactly today's request (commit b5ddcf3).

**Large documents** (B, full pipeline): **6/6 complete** (today: 0/6), all subdocuments returncode 100.

| Document | Pages | Images sent | Belege | Spot check |
|---|---|---|---|---|
| …25552222600 | 54 | 44 | 2 | – |
| …25552283100 | 58 | 42 | 4 | duplicate copies of one invoice merged into one Beleg (24 items) |
| …26551211500 | 53 | 44 | 6 | – |
| …26551468300 | 114 | 0 (cap) | 2 | all invoice-like pages assigned |
| …26551820200 | 121 | 0 (cap) | 1 | cost estimate p26–27 found; no other invoice-like pages |
| …26K40159C05 | 52 | 50 | 1 | DEKRA invoice (two copies) merged; unassigned invoice-like pages are lawyer letters |

Runtime 56–79 s per large document.

**Decision:** pending (Bernhard).
