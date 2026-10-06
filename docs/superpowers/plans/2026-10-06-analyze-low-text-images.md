# Analyze Low-Text Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop BPS/Sanierer documents with more than 50 pages failing in analyze, by not sending images of low-text (photo) pages and by capping analyze images at 50 — validated by an offline A/B experiment before any product switches it on.

**Architecture:** A new pure module `core/analyze_content.py` holds the page classification and the analyze request construction. `Pipeline.analyze_document` uses it, driven by a new per-product `ProductConfig.analyze_low_text_threshold` (default `None` = today's behaviour). The 50-image cap applies to every product. No product turns the threshold on until the experiment (Tasks 3–4) says go, so Tasks 1–2 change nothing for any request that succeeds today.

**Tech Stack:** Python 3.11, PyMuPDF (`fitz`), Azure OpenAI (gpt-5.4), structlog, pytest 9.

**Spec:** `docs/superpowers/specs/2026-10-06-analyze-low-text-images-design.md`

## Global Constraints

- VCC's analyze request must stay byte-identical to today for every document it can process today (`analyze_low_text_threshold = None`).
- Image cap: at most **50** images per analyze request; above that, send **no** images (all-or-nothing). Applies to all products.
- Low-text marker text, verbatim: `[Kaum Text erkannt ({chars} Zeichen) – vermutlich Foto oder Leerseite. Bild nicht mitgesendet.]`
- Image label text, verbatim: `Seite {n}:` — only when the threshold is set.
- Starting threshold **150** characters; the experiment confirms or replaces it.
- Images stay `"detail": "low"`; rendering budget/dpi logic unchanged.
- No customer documents committed: corpora live in gitignored `bps_sanierer_input/` and `test_uploads/`; experiment output in `temp/`.
- Deploy only after the page-rescaling change (commit `cc8a46a`, bps-test `v20261004a`) has been promoted to production. Unique deploy tags, never `latest`.
- Tests: `.venv/bin/python -m pytest tests/` — baseline 292 passed.

## Review Focus

1. **OCR degraded to one engine** (only one `--- OCR Source … ---` header on a page) — the character count must be on the same scale as with two engines, not halved. → test in Task 1 (`count_ocr_chars` takes the max per engine).
2. **Page whose only OCR output is Mistral image placeholders** (`![img-0.jpeg](img-0.jpeg)`) — must count as 0 characters, i.e. low-text. → test in Task 1.
3. **Document where every page is low-text** (pure photo set) — request must still be valid: prompt only, no images, no stray `Seite N:` labels, no crash. → test in Task 2.
4. **Exactly 50 vs 51 images** — 50 sends all, 51 sends none and logs `analyze_images_capped`. → tests in Task 1 (selection) and Task 2 (end to end).
5. **Page missing from `markdown_by_page`** (OCR returned nothing for it) — no evidence it is a photo, so it keeps its image. → test in Task 1.

## File Structure

| File | Responsibility |
|---|---|
| `core/analyze_content.py` (new) | Pure functions: count OCR chars, classify low-text pages, build the page-numbered markdown, select pages to image (with cap), build the analyze content blocks. |
| `core/product.py` | New field `analyze_low_text_threshold`. |
| `core/pipeline.py` | `analyze_document` uses `core/analyze_content.py`; telemetry fields. |
| `scripts/analyze_image_experiment.py` (new) | Offline A/B + calibration + large-document runs. |
| `products/bps/product.py`, `products/sanierer/product.py` | Switch on (Task 5, only after go). |
| `tests/core/test_analyze_content.py` (new), `tests/core/test_analyze_document_images.py` (new) | Tests. |

---

### Task 1: Pure analyze-content module

**Files:**
- Create: `core/analyze_content.py`
- Test: `tests/core/test_analyze_content.py`

**Interfaces:**
- Consumes: nothing (pure; no imports from `core.pipeline`).
- Produces:
  - `MAX_ANALYZE_IMAGES: int = 50`
  - `LOW_TEXT_MARKER: str` (format string with `{chars}`)
  - `count_ocr_chars(page_markdown: str) -> int`
  - `low_text_page_chars(markdown_by_page: dict[int, str], threshold: int | None) -> dict[int, int]` — page → char count, only for low-text pages; `{}` when threshold is `None`.
  - `build_pages_markdown(markdown_by_page: dict[int, str], low_text: dict[int, int]) -> str`
  - `select_image_pages(page_numbers: Iterable[int], low_text: dict[int, int], cap: int = MAX_ANALYZE_IMAGES) -> tuple[list[int], bool]` — `(pages to image, capped)`.
  - `build_analyze_blocks(prompt: str, page_images: list[tuple[int, str]], label_images: bool) -> list[dict]` — `page_images` is `(page_number, base64_png)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_analyze_content.py
from core.analyze_content import (
    LOW_TEXT_MARKER,
    MAX_ANALYZE_IMAGES,
    build_analyze_blocks,
    build_pages_markdown,
    count_ocr_chars,
    low_text_page_chars,
    select_image_pages,
)

A = "--- OCR Source A (Mistral) ---\n"
B = "--- OCR Source B (Azure Document Intelligence) ---\n"


def test_count_takes_the_larger_engine_not_the_sum():
    page = A + "Rechnung 123" + "\n\n" + B + "Rechnung 123 Summe"
    assert count_ocr_chars(page) == len("Rechnung123Summe")


def test_count_single_engine_page_is_on_the_same_scale():
    assert count_ocr_chars(B + "Rechnung 123 Summe") == len("Rechnung123Summe")


def test_count_ignores_mistral_image_placeholders_and_whitespace():
    page = A + "![img-0.jpeg](img-0.jpeg)\n\n  \n" + "\n\n" + B + ""
    assert count_ocr_chars(page) == 0


def test_count_text_without_source_headers():
    assert count_ocr_chars("  ab c \n") == 3


def test_count_empty_page():
    assert count_ocr_chars("") == 0


def test_low_text_disabled_when_threshold_is_none():
    assert low_text_page_chars({1: "", 2: "x"}, None) == {}


def test_low_text_marks_pages_below_threshold_only():
    pages = {1: A + "x" * 149, 2: A + "x" * 150, 3: A + "![img-0.jpeg](img-0.jpeg)"}
    assert low_text_page_chars(pages, 150) == {1: 149, 3: 0}


def test_pages_markdown_without_low_text_matches_pipeline_format():
    pages = {1: "eins", 2: "zwei"}
    expected = "--- PAGE 1 ---\n: eins\n\n---\n\n--- PAGE 2 ---\n: zwei"
    assert build_pages_markdown(pages, {}) == expected


def test_pages_markdown_appends_marker_to_low_text_pages():
    pages = {1: "eins", 2: "ab"}
    out = build_pages_markdown(pages, {2: 2})
    assert out == (
        "--- PAGE 1 ---\n: eins\n\n---\n\n--- PAGE 2 ---\n: ab\n"
        + LOW_TEXT_MARKER.format(chars=2)
    )


def test_marker_wording_is_verbatim():
    assert LOW_TEXT_MARKER.format(chars=23) == (
        "[Kaum Text erkannt (23 Zeichen) – vermutlich Foto oder Leerseite. "
        "Bild nicht mitgesendet.]"
    )


def test_select_skips_low_text_pages():
    assert select_image_pages(range(1, 5), {2: 0, 4: 10}) == ([1, 3], False)


def test_select_keeps_pages_missing_from_low_text():
    # A page OCR returned nothing for is not in markdown_by_page, hence not in
    # low_text: no evidence it is a photo, so it keeps its image.
    assert select_image_pages([1, 2, 3], {}) == ([1, 2, 3], False)


def test_select_exactly_at_cap_sends_all():
    pages, capped = select_image_pages(range(1, MAX_ANALYZE_IMAGES + 1), {})
    assert len(pages) == MAX_ANALYZE_IMAGES and capped is False


def test_select_over_cap_sends_none():
    assert select_image_pages(range(1, MAX_ANALYZE_IMAGES + 2), {}) == ([], True)


def test_select_counts_only_text_pages_against_the_cap():
    low = {p: 0 for p in range(1, 61)}  # 60 photo pages
    pages, capped = select_image_pages(range(1, 101), low)  # 40 text pages
    assert pages == list(range(61, 101)) and capped is False


def test_blocks_unlabelled_match_todays_shape():
    blocks = build_analyze_blocks("P", [(1, "AAA"), (2, "BBB")], label_images=False)
    assert blocks == [
        {"type": "text", "text": "P"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA", "detail": "low"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB", "detail": "low"}},
    ]


def test_blocks_labelled_put_page_label_before_each_image():
    blocks = build_analyze_blocks("P", [(3, "AAA")], label_images=True)
    assert blocks == [
        {"type": "text", "text": "P"},
        {"type": "text", "text": "Seite 3:"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA", "detail": "low"}},
    ]


def test_blocks_without_images_are_prompt_only():
    assert build_analyze_blocks("P", [], label_images=True) == [{"type": "text", "text": "P"}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_analyze_content.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.analyze_content'`

- [ ] **Step 3: Write the implementation**

```python
# core/analyze_content.py
"""Building the analyze request: which pages get an image, and how pages are labelled.

Pure functions, no I/O, so the pipeline and the offline experiment
(scripts/analyze_image_experiment.py) build byte-identical requests.

Background: gpt-5.4 accepts at most 50 images per request, and analyze sends
one per page — so every document over 50 pages failed (spec
docs/superpowers/specs/2026-10-06-analyze-low-text-images-design.md). Many of
those pages are damage photos with no text, whose images add little to the
page grouping.
"""
from __future__ import annotations

import re
from typing import Iterable

MAX_ANALYZE_IMAGES = 50

# Reviewed German, shown to the model in the page's text section.
LOW_TEXT_MARKER = (
    "[Kaum Text erkannt ({chars} Zeichen) – vermutlich Foto oder Leerseite. "
    "Bild nicht mitgesendet.]"
)

# DualOCRProcessor labels each engine's output with one of these headers.
_SOURCE_HEADER = re.compile(r"^--- OCR Source [^\n]*---$", re.MULTILINE)
# Mistral writes a placeholder like ![img-0.jpeg](img-0.jpeg) for each photo.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_WHITESPACE = re.compile(r"\s+")


def count_ocr_chars(page_markdown: str) -> int:
    """Characters of real text on a page, taking the larger of the two engines.

    The max rather than the sum keeps a page that fell back to one OCR engine
    on the same scale as a page both engines read.
    """
    parts = _SOURCE_HEADER.split(page_markdown or "")
    counts = [len(_WHITESPACE.sub("", _MD_IMAGE.sub("", part))) for part in parts]
    return max(counts, default=0)


def low_text_page_chars(markdown_by_page: dict[int, str], threshold: int | None) -> dict[int, int]:
    """Page -> char count for every page below `threshold`. Empty when disabled."""
    if threshold is None:
        return {}
    counts = {page: count_ocr_chars(md) for page, md in markdown_by_page.items()}
    return {page: n for page, n in counts.items() if n < threshold}


def build_pages_markdown(markdown_by_page: dict[int, str], low_text: dict[int, int]) -> str:
    """The page-numbered OCR text the analyze prompt embeds.

    With `low_text` empty this is exactly the string Pipeline.extract_markdown
    builds, which is what keeps VCC's request unchanged.
    """
    sections = []
    for page, txt in markdown_by_page.items():
        section = f"--- PAGE {page} ---\n: {txt}"
        if page in low_text:
            section += "\n" + LOW_TEXT_MARKER.format(chars=low_text[page])
        sections.append(section)
    return "\n\n---\n\n".join(sections)


def select_image_pages(
    page_numbers: Iterable[int],
    low_text: dict[int, int],
    cap: int = MAX_ANALYZE_IMAGES,
) -> tuple[list[int], bool]:
    """Pages that get an image, and whether the cap removed all of them.

    All-or-nothing on purpose: text-only analyze is a proven path (the
    content-policy fallback), "the best 50" would be a new judgement call.
    """
    pages = [p for p in page_numbers if p not in low_text]
    if len(pages) > cap:
        return [], True
    return pages, False


def build_analyze_blocks(prompt: str, page_images: list[tuple[int, str]], label_images: bool) -> list[dict]:
    """Prompt text block, then one `detail: low` image per entry.

    `label_images` puts a `Seite N:` text block before each image. Needed once
    any page is skipped: position alone no longer says which page an image is.
    """
    blocks: list[dict] = [{"type": "text", "text": prompt}]
    for page, b64 in page_images:
        if label_images:
            blocks.append({"type": "text", "text": f"Seite {page}:"})
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
        })
    return blocks
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_analyze_content.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add core/analyze_content.py tests/core/test_analyze_content.py
git commit -m "feat: pure helpers for low-text page classification and analyze blocks"
```

---

### Task 2: Wire into `analyze_document`, add the product switch and the cap

**Files:**
- Modify: `core/product.py` (dataclass `ProductConfig`)
- Modify: `core/pipeline.py:234-292` (`analyze_document`)
- Test: `tests/core/test_analyze_document_images.py`

**Interfaces:**
- Consumes: everything Task 1 produces.
- Produces: `ProductConfig.analyze_low_text_threshold: int | None = None`; `analyze_llm_call` telemetry gains `images_sent: int`, `pages_low_text: int`; new warning event `analyze_images_capped` with `pages: int`, `images_planned: int`. No product sets the threshold in this task.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_analyze_document_images.py
"""analyze_document drives core.analyze_content: unchanged when the switch is
off, skips low-text images when on, and never sends more than 50 images."""
import fitz
from structlog.testing import capture_logs

from core.pipeline import Pipeline
from core.product import ProductConfig

A = "--- OCR Source A (Mistral) ---\n"
TEXT = A + "Rechnung " + "x" * 200
PHOTO = A + "![img-0.jpeg](img-0.jpeg)"


class _CaptureClient:
    def __init__(self):
        self.blocks_seen = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.blocks_seen.append(kwargs["messages"][0]["content"])

                class _Msg:
                    content = '{"invoice_pages": {}}'

                class _Choice:
                    message = _Msg()

                class _Usage:
                    prompt_tokens = 10
                    completion_tokens = 5

                class _Resp:
                    choices = [_Choice()]
                    usage = _Usage()

                return _Resp()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _pdf(tmp_path, n_pages):
    doc = fitz.open()
    for i in range(n_pages):
        doc.new_page().insert_text((72, 72), f"Seite {i + 1}")
    path = tmp_path / "input.pdf"
    doc.save(path)
    doc.close()
    return str(path)


def _pipe(tmp_path, monkeypatch, markdown_by_page, threshold):
    client = _CaptureClient()
    monkeypatch.setattr("core.pipeline.AzureOpenAI", lambda **kwargs: client)
    pipe = object.__new__(Pipeline)
    pipe.product_config = ProductConfig(
        name="t",
        extract_prompt_builder=lambda **kw: "x",
        extract_output_schema={},
        analyze_prompt_builder=lambda *, markdown_text: f"PROMPT<{markdown_text}>",
        analyze_low_text_threshold=threshold,
    )
    pipe.file_type = "pdf"
    pipe.local_input_path = _pdf(tmp_path, len(markdown_by_page))
    pipe.markdown_by_page = markdown_by_page
    pipe.markdown_with_pages_numbers = "\n\n---\n\n".join(
        f"--- PAGE {p} ---\n: {t}" for p, t in markdown_by_page.items()
    )
    return pipe, client


def _images(blocks):
    return [b for b in blocks if b["type"] == "image_url"]


def _texts(blocks):
    return [b["text"] for b in blocks if b["type"] == "text"]


def test_threshold_defaults_to_none():
    cfg = ProductConfig(name="t", extract_prompt_builder=lambda **kw: "x", extract_output_schema={})
    assert cfg.analyze_low_text_threshold is None


def test_switch_off_request_is_unchanged(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {1: TEXT, 2: PHOTO}, threshold=None)
    pipe.analyze_document()
    (blocks,) = client.blocks_seen
    assert _texts(blocks) == [f"PROMPT<{pipe.markdown_with_pages_numbers}>"]
    assert len(_images(blocks)) == 2
    assert blocks[0]["type"] == "text" and all(b["type"] == "image_url" for b in blocks[1:])


def test_switch_on_skips_photo_image_and_marks_the_page(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {1: TEXT, 2: PHOTO}, threshold=150)
    pipe.analyze_document()
    (blocks,) = client.blocks_seen
    assert len(_images(blocks)) == 1
    assert _texts(blocks)[1:] == ["Seite 1:"]
    prompt = _texts(blocks)[0]
    assert "--- PAGE 2 ---" in prompt
    assert "[Kaum Text erkannt (0 Zeichen)" in prompt
    assert prompt.count("[Kaum Text erkannt") == 1


def test_all_pages_low_text_sends_prompt_only(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {1: PHOTO, 2: PHOTO, 3: PHOTO}, threshold=150)
    pipe.analyze_document()
    (blocks,) = client.blocks_seen
    assert len(blocks) == 1 and blocks[0]["type"] == "text"


def test_fifty_text_pages_send_fifty_images(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {p: TEXT for p in range(1, 51)}, threshold=None)
    pipe.analyze_document()
    assert len(_images(client.blocks_seen[0])) == 50


def test_fifty_one_pages_go_text_only_and_log_the_cap(tmp_path, monkeypatch):
    # Applies with the switch OFF too: this request would be rejected today.
    pipe, client = _pipe(tmp_path, monkeypatch, {p: TEXT for p in range(1, 52)}, threshold=None)
    with capture_logs() as logs:
        pipe.analyze_document()
    (blocks,) = client.blocks_seen
    assert _images(blocks) == []
    capped = [e for e in logs if e["event"] == "analyze_images_capped"]
    assert capped and capped[0]["pages"] == 51 and capped[0]["images_planned"] == 51


def test_telemetry_reports_images_and_low_text_pages(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {1: TEXT, 2: PHOTO, 3: PHOTO}, threshold=150)
    with capture_logs() as logs:
        pipe.analyze_document()
    (call,) = [e for e in logs if e["event"] == "analyze_llm_call"]
    assert call["images_sent"] == 1 and call["pages_low_text"] == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_analyze_document_images.py -v`
Expected: FAIL — `TypeError: ProductConfig.__init__() got an unexpected keyword argument 'analyze_low_text_threshold'`

- [ ] **Step 3: Add the field to `ProductConfig`**

In `core/product.py`, after `analyze_output_schema`:

```python
    analyze_output_schema: dict | None = None

    # Pages whose OCR text is shorter than this (chars, larger engine) are sent
    # to analyze without their image and marked as "probably a photo".
    # None = every page keeps its image (VCC). See core/analyze_content.py.
    analyze_low_text_threshold: int | None = None
```

- [ ] **Step 4: Rewrite `analyze_document`**

Add to the imports at the top of `core/pipeline.py`:

```python
from core.analyze_content import (
    build_analyze_blocks,
    build_pages_markdown,
    low_text_page_chars,
    select_image_pages,
)
```

Replace the body of `analyze_document` from its first line down to (not including) `client = AzureOpenAI(` with:

```python
    def analyze_document(self):
        # getattr: several tests build product_config as a bare SimpleNamespace.
        threshold = getattr(self.product_config, "analyze_low_text_threshold", None)
        low_text = low_text_page_chars(self.markdown_by_page, threshold)
        # Only rebuilt when a page is marked, so the switch-off prompt is the
        # exact string extract_markdown produced.
        pages_markdown = (
            build_pages_markdown(self.markdown_by_page, low_text)
            if low_text else self.markdown_with_pages_numbers
        )

        if self.product_config.analyze_prompt_builder is not None:
            prompt = self.product_config.analyze_prompt_builder(markdown_text=pages_markdown)
        else:
            prompt = build_prompt_for_analyze_document(markdown_text=pages_markdown)

        # One low-res image per page that is not low-text, at most 50 in all.
        page_images: list[tuple[int, str]] = []
        if self.file_type == "pdf":
            with fitz.open(self.local_input_path) as doc:
                all_pages = range(1, len(doc) + 1)
                image_pages, capped = select_image_pages(all_pages, low_text)
                if capped:
                    _telemetry.warning(
                        "analyze_images_capped",
                        reason="more than 50 page images — analyzing from OCR text only",
                        pages=len(doc),
                        images_planned=len(doc) - len(low_text),
                    )
                for page_number in image_pages:
                    page = doc[page_number - 1]
                    # Budget applied per page: these reach the API as separate
                    # images, so each one — not their sum — has to fit.
                    dpi = cap_page_dpi(page, render_dpi_for(
                        [(page.rect.width, page.rect.height)],
                        ANALYZE_RENDER_DPI,
                        budget_px=ANALYZE_BUDGET_PX,
                    ))
                    pix = page.get_pixmap(dpi=dpi)
                    img_bytes = pix.tobytes("png")
                    del pix
                    page_images.append((page_number, base64.b64encode(img_bytes).decode("utf-8")))

        content_blocks = build_analyze_blocks(prompt, page_images, label_images=threshold is not None)

```

Then extend the existing `analyze_llm_call` telemetry in the same method:

```python
            _telemetry.info(
                "analyze_llm_call",
                model=analyze_model,
                prompt_tokens=_usage.prompt_tokens,
                completion_tokens=_usage.completion_tokens,
                images_sent=len(page_images),
                pages_low_text=len(low_text),
            )
```

The rest of the method (`client = AzureOpenAI(...)`, `call_with_vision_fallback`, `analysis_dict`) stays as it is.

- [ ] **Step 5: Run the new tests and the whole suite**

Run: `.venv/bin/python -m pytest tests/core/test_analyze_document_images.py -v`
Expected: all PASS

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (baseline 292 + Task 1 + Task 2 tests), including `tests/core/test_vision_fallback_pipeline.py::test_analyze_document_falls_back_to_text_and_marks_dropped` unchanged.

- [ ] **Step 6: Commit**

```bash
git add core/product.py core/pipeline.py tests/core/test_analyze_document_images.py
git commit -m "feat: analyze skips low-text page images per product and caps images at 50"
```

---

### Task 3: Offline experiment script

**Files:**
- Create: `scripts/analyze_image_experiment.py`

**Interfaces:**
- Consumes: `count_ocr_chars`, `build_pages_markdown` (Task 1); `ProductConfig.analyze_low_text_threshold`, `Pipeline.analyze_document` (Task 2); `Pipeline`, `DualOCRProcessor`, `AzureInvoiceProcessor`, `LocalStorage`, `load_product_config` (existing).
- Produces: CLI `scripts/analyze_image_experiment.py {calibrate|ab|large} --product bps|sanierer [--threshold 150] [--runs 3] PDF...`; outputs under `temp/analyze_image_experiment/`.

No unit tests: this is a throwaway-grade research harness like `scripts/layout_experiment.py`; correctness is checked by the smoke run in Step 2.

- [ ] **Step 1: Write the script**

```python
# scripts/analyze_image_experiment.py
"""A/B harness for skipping low-text page images in analyze.

Spec: docs/superpowers/specs/2026-10-06-analyze-low-text-images-design.md

Modes (all local; STORAGE_BACKEND forced to local):
  calibrate  OCR every PDF (cached), print a chars-per-page histogram and list
             pages with 50–300 chars for visual review.
  ab         Analyze only, A = threshold None (today) vs B = --threshold,
             --runs each. Compares how TEXT pages are grouped (must match) and
             where LOW-TEXT pages land (reported), plus analyze tokens.
  large      B only, full pipeline (analyze, split, extract) — the >50-page
             documents A cannot process.

OCR runs once per PDF and is cached in temp/analyze_image_experiment/ocr/, so
A and B see identical input and OCR is paid once. Delete the cache to re-OCR.

Usage:
    .venv/bin/python scripts/analyze_image_experiment.py calibrate --product bps bps_sanierer_input/BPS_Input/*.pdf
    .venv/bin/python scripts/analyze_image_experiment.py ab --product bps --runs 3 bps_sanierer_input/BPS_Input/*.pdf
    .venv/bin/python scripts/analyze_image_experiment.py large --product bps test_uploads/BPS_Documents_Large/*.pdf
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "temp" / "analyze_image_experiment"
os.environ["STORAGE_BACKEND"] = "local"
os.environ.setdefault("CLEANUP_ARTIFACTS", "false")
sys.path.insert(0, str(REPO_ROOT))

import structlog  # noqa: E402

_EVENTS: list[dict] = []


def _capture(logger, method_name, event_dict):
    if event_dict.get("event") in ("analyze_llm_call", "llm_call", "analyze_images_capped"):
        _EVENTS.append(dict(event_dict))
    return event_dict


structlog.configure(
    processors=[_capture, structlog.processors.add_log_level, structlog.processors.JSONRenderer()],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

from core.analyze_content import build_pages_markdown, count_ocr_chars  # noqa: E402
from core.ocr.ocr_dual import DualOCRProcessor  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.processors.azure_processor import AzureInvoiceProcessor  # noqa: E402
from core.product import load_product_config  # noqa: E402
from core.storage.storage import LocalStorage  # noqa: E402


def _pipeline(pdf: Path, config, tag: str) -> Pipeline:
    """A Pipeline on `pdf` with OCR loaded from cache (or run once and cached)."""
    out_prefix = OUT / "runs" / f"{pdf.stem}__{tag}"
    out_prefix.mkdir(parents=True, exist_ok=True)
    pipe = Pipeline(
        file_key=str(pdf.resolve()),
        ocr_engine=DualOCRProcessor(name="dual_ocr"),
        product_config=config,
        storage=LocalStorage(),
        output_prefix=str(out_prefix),
    )
    cache = OUT / "ocr" / f"{pdf.stem}.json"
    if cache.exists():
        by_page = {int(k): v for k, v in json.loads(cache.read_text()).items()}
        pipe.markdown_by_page = by_page
        pipe.markdown = "\n\n".join(by_page.values())
        pipe.markdown_with_pages_numbers = build_pages_markdown(by_page, {})
    else:
        pipe.extract_markdown()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(pipe.markdown_by_page, ensure_ascii=False))
    return pipe


def _grouping(invoice_pages: dict, low: set[int]) -> tuple[frozenset, dict[int, frozenset]]:
    """Split an analyze result into (partition of text pages, low-text page -> its Beleg).

    Belege are identified by their text pages, not by the LLM's keys, which
    vary between runs. A low-text page maps to the text pages of its Beleg
    (empty frozenset = a Beleg of only low-text pages); unassigned pages are absent.
    """
    groups = [frozenset(int(p) for p in pages) for pages in (invoice_pages or {}).values()]
    partition = frozenset(g - low for g in groups if g - low)
    placement = {p: g - low for g in groups for p in g if p in low}
    return partition, placement


def cmd_calibrate(args, config):
    hist = Counter()
    review = []
    for pdf in args.pdfs:
        pipe = _pipeline(pdf, config, "calibrate")
        for page, md in pipe.markdown_by_page.items():
            n = count_ocr_chars(md)
            hist[min(n // 50 * 50, 1000)] += 1
            if 50 <= n <= 300:
                review.append({"pdf": pdf.name, "page": page, "chars": n, "text": md[:300]})
    print("chars/page (bucket start: pages)")
    for bucket in sorted(hist):
        label = f"{bucket}+" if bucket == 1000 else f"{bucket}-{bucket + 49}"
        print(f"  {label:>9}: {hist[bucket]}")
    (OUT / "calibration_review.json").write_text(json.dumps(review, indent=2, ensure_ascii=False))
    print(f"{len(review)} pages with 50–300 chars -> {OUT / 'calibration_review.json'}")


def cmd_ab(args, config):
    variants = {"A": dataclasses.replace(config, analyze_low_text_threshold=None),
                "B": dataclasses.replace(config, analyze_low_text_threshold=args.threshold)}
    report = []
    for pdf in args.pdfs:
        ocr = _pipeline(pdf, config, "ocr").markdown_by_page
        low = {p for p, md in ocr.items() if count_ocr_chars(md) < args.threshold}
        runs: dict[str, list] = {"A": [], "B": []}
        tokens: dict[str, list] = {"A": [], "B": []}
        for name, cfg in variants.items():
            for i in range(args.runs):
                _EVENTS.clear()
                pipe = _pipeline(pdf, cfg, f"{name}{i}")
                pipe.analyze_document()
                runs[name].append(_grouping(pipe.analysis_dict.get("invoice_pages"), low))
                tokens[name].append(sum(e.get("prompt_tokens", 0) for e in _EVENTS))
        a_parts = {part for part, _ in runs["A"]}
        b_parts = {part for part, _ in runs["B"]}
        text_ok = b_parts <= a_parts
        a_place = Counter(json.dumps({p: sorted(g) for p, g in sorted(pl.items())}) for _, pl in runs["A"])
        b_place = Counter(json.dumps({p: sorted(g) for p, g in sorted(pl.items())}) for _, pl in runs["B"])
        rec = {
            "pdf": pdf.name, "pages": len(ocr), "low_text_pages": sorted(low),
            "text_grouping_ok": text_ok,
            "a_distinct_text_groupings": len(a_parts), "b_distinct_text_groupings": len(b_parts),
            "a_low_text_placements": a_place, "b_low_text_placements": b_place,
            "a_prompt_tokens": tokens["A"], "b_prompt_tokens": tokens["B"],
            "a_groupings": [sorted(sorted(g) for g in part) for part in a_parts],
            "b_groupings": [sorted(sorted(g) for g in part) for part in b_parts],
        }
        report.append(rec)
        flag = "OK " if text_ok else "DIFF"
        print(f"[{flag}] {pdf.name}: pages={len(ocr)} low={len(low)} "
              f"A-groupings={len(a_parts)} B-groupings={len(b_parts)} "
              f"tokens A={tokens['A']} B={tokens['B']} "
              f"photo-placement same={a_place == b_place}", file=sys.stderr)
    (OUT / f"ab_{args.product}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=list))
    ok = sum(r["text_grouping_ok"] for r in report)
    print(f"text grouping matches A: {ok}/{len(report)} -> {OUT / f'ab_{args.product}.json'}")


def cmd_large(args, config):
    cfg = dataclasses.replace(config, analyze_low_text_threshold=args.threshold)
    processor = AzureInvoiceProcessor(
        name="azure_processor",
        api_key=os.getenv("AZURE_OPENAI_KEY"),
        model=os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4"),
        vision_model=os.getenv("OPENAI_VISION_MODEL", "gpt-5.4"),
        azure_endpoint=os.getenv("AZURE_ENDPOINT"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
    )
    report = []
    for pdf in args.pdfs:
        _EVENTS.clear()
        t0 = time.monotonic()
        rec = {"pdf": pdf.name}
        try:
            pipe = _pipeline(pdf, cfg, "large")
            pipe.analyze_document()
            pipe.split_document_into_invoices()
            pipe.extract_data_from_subdocuments(processor)
            result = pipe.extraction_result_json
            rec.update(
                completed=True, pages=len(pipe.markdown_by_page),
                invoice_pages=pipe.analysis_dict.get("invoice_pages"),
                subdocuments=result["number_of_subdocuments"],
                items=[len((s or {}).get("items") or []) for s in result["subdocuments"]],
                returncodes=[(s or {}).get("returncode") for s in result["subdocuments"]],
            )
        except Exception as exc:  # report and continue with the next document
            rec.update(completed=False, error=f"{type(exc).__name__}: {str(exc)[:300]}")
        analyze = [e for e in _EVENTS if e["event"] == "analyze_llm_call"]
        rec.update(
            seconds=round(time.monotonic() - t0, 1),
            capped=any(e["event"] == "analyze_images_capped" for e in _EVENTS),
            images_sent=analyze[0].get("images_sent") if analyze else None,
            pages_low_text=analyze[0].get("pages_low_text") if analyze else None,
            prompt_tokens=sum(e.get("prompt_tokens", 0) for e in _EVENTS),
            completion_tokens=sum(e.get("completion_tokens", 0) for e in _EVENTS),
        )
        report.append(rec)
        print(f"{pdf.name}: completed={rec['completed']} subdocs={rec.get('subdocuments')} "
              f"images={rec['images_sent']} low={rec['pages_low_text']} capped={rec['capped']} "
              f"{rec['seconds']}s", file=sys.stderr)
    (OUT / f"large_{args.product}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"completed {sum(r['completed'] for r in report)}/{len(report)} -> {OUT / f'large_{args.product}.json'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["calibrate", "ab", "large"])
    ap.add_argument("--product", required=True, choices=["bps", "sanierer"])
    ap.add_argument("--threshold", type=int, default=150)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("pdfs", nargs="+", type=Path)
    args = ap.parse_args()
    missing = [str(p) for p in args.pdfs if not p.exists()]
    if missing:
        print(f"Missing files: {missing}", file=sys.stderr)
        return 2
    os.environ["PRODUCT_NAME"] = args.product
    OUT.mkdir(parents=True, exist_ok=True)
    config = load_product_config(args.product)
    {"calibrate": cmd_calibrate, "ab": cmd_ab, "large": cmd_large}[args.mode](args, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke-run on one small document**

Run: `.venv/bin/python scripts/analyze_image_experiment.py ab --product bps --runs 1 "$(ls bps_sanierer_input/BPS_Input/*.pdf | head -1)"`
Expected: one `[OK ]` or `[DIFF]` line on stderr, `text grouping matches A: x/1`, and `temp/analyze_image_experiment/ab_bps.json` plus a cached OCR file under `temp/analyze_image_experiment/ocr/`. Costs one OCR pass and two analyze calls.

- [ ] **Step 3: Commit**

```bash
git add scripts/analyze_image_experiment.py
git commit -m "chore: A/B harness for skipping low-text page images in analyze"
```

---

### Task 4: Run the experiment and decide (human gate)

**Files:**
- Modify: `docs/superpowers/specs/2026-10-06-analyze-low-text-images-design.md` (append a "Results" section)

**Interfaces:**
- Consumes: Task 3's script and outputs.
- Produces: a recorded go / no-go and the chosen threshold `T` (an integer) that Task 5 uses.

- [ ] **Step 1: Calibrate the threshold**

```bash
.venv/bin/python scripts/analyze_image_experiment.py calibrate --product bps \
    bps_sanierer_input/BPS_Input/*.pdf test_uploads/BPS_Documents_Large/*.pdf
.venv/bin/python scripts/analyze_image_experiment.py calibrate --product sanierer \
    bps_sanierer_input/Sanierer_Input/*.pdf
```

This OCRs every document once (≈600 pages, ≈€7). Read `temp/analyze_image_experiment/calibration_review.json` and look at each listed page in its PDF. Pick `T` so that real invoice/letter pages (totals-only last pages, cover letters) stay at or above it and photo/blank pages fall below. Keep 150 unless the review shows a reason.

- [ ] **Step 2: Run the A/B on normal-size documents**

```bash
.venv/bin/python scripts/analyze_image_experiment.py ab --product bps --threshold T --runs 3 bps_sanierer_input/BPS_Input/*.pdf
.venv/bin/python scripts/analyze_image_experiment.py ab --product sanierer --threshold T --runs 3 bps_sanierer_input/Sanierer_Input/*.pdf
```

For every `[DIFF]` line and every document where `photo-placement same=False`, open `ab_<product>.json` and the PDF and judge whether B's grouping is a regression.

- [ ] **Step 3: Run the large documents end to end**

```bash
.venv/bin/python scripts/analyze_image_experiment.py large --product bps --threshold T test_uploads/BPS_Documents_Large/*.pdf
```

For each document check `completed`, `capped`, and compare `invoice_pages` against the PDF by eye.

- [ ] **Step 4: Record results and decide**

Append to the spec:

```markdown
## Results (YYYY-MM-DD)

- Threshold chosen: T (calibration: N pages reviewed between 50 and 300 chars; …)
- A/B normal-size: BPS x/8, Sanierer y/8 text groupings match; photo-page placement changed in … — judged …
- Large documents: k/6 completed; capped on …; splits checked: …
- Analyze prompt tokens A vs B: …
- **Decision: GO / NO-GO** (if NO-GO: ship the cap only — Task 2 already contains it — and skip Task 5)
```

Present the results to Bernhard and wait for an explicit go / no-go before Task 5.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-10-06-analyze-low-text-images-design.md
git commit -m "docs: record analyze low-text image experiment results"
```

---

### Task 5: Switch on for BPS and Sanierer (only after GO)

**Files:**
- Modify: `products/bps/product.py`, `products/sanierer/product.py`
- Test: `tests/core/test_product.py`

**Interfaces:**
- Consumes: `ProductConfig.analyze_low_text_threshold` (Task 2), threshold `T` (Task 4).
- Produces: BPS and Sanierer configs with `analyze_low_text_threshold=T`; VCC unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_product.py` (replace `150` with `T` if Task 4 chose differently):

```python
@pytest.mark.parametrize("name, expected", [("bps", 150), ("sanierer", 150), ("vetcostcheck", None)])
def test_low_text_threshold_per_product(name, expected):
    assert load_product_config(name).analyze_low_text_threshold == expected
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/core/test_product.py -v -k low_text`
Expected: FAIL for `bps` and `sanierer` (`None != 150`), PASS for `vetcostcheck`.

- [ ] **Step 3: Set the threshold**

In both `products/bps/product.py` and `products/sanierer/product.py`, add one argument to `CONFIG = ProductConfig(...)`:

```python
    analyze_output_schema=ANALYZE_OUTPUT_SCHEMA,
    # Photo pages go to analyze without their image (spec 2026-10-06).
    analyze_low_text_threshold=150,
)
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add products/bps/product.py products/sanierer/product.py tests/core/test_product.py
git commit -m "feat: skip low-text page images in analyze for bps and sanierer"
```

---

### Task 6: Deploy to test, verify, promote (after the rescaling change is in production)

**Files:** none (operations). Requires Bernhard's go for each deploy and promote.

- [ ] **Step 1: Check the precondition**

```bash
az containerapp show -n ca-worker-bps -g rg-3c-invoice --query "properties.template.containers[0].image" -o tsv
```

Expected: a tag at or after the one carrying `cc8a46a` (bps-test currently `v20261004a`). If prod still shows `v20260824a`, stop: the rescaling change must be promoted first.

- [ ] **Step 2: Merge to `main` and deploy to test**

```bash
./deploy.sh bps v2026MMDDa test
./deploy.sh sanierer v2026MMDDa test
```

Use today's date and a fresh suffix. Test workers scale to zero now, so the first job after idle waits for a cold start.

- [ ] **Step 3: Verify on the test tier**

Upload the 114-page document (`test_uploads/BPS_Documents_Large/Sammeldokument_26551468300.pdf`) to `https://3cbps-test.flex-capital-scale.com` (API key in `.env`, `X-Api-Key`), e.g. with `scripts/smoke_test_tier.py`. Expected: job finishes with subdocuments; worker logs show `analyze_llm_call` with `images_sent ≤ 50` and `pages_low_text > 0`, no `Too many images` error.

- [ ] **Step 4: Promote**

```bash
scripts/promote.sh bps v2026MMDDa            # dry run
scripts/promote.sh bps v2026MMDDa --apply
scripts/promote.sh sanierer v2026MMDDa --apply
```

- [ ] **Step 5: Close the loop**

Update the memory note `project_sanierer_large_docs.md` (fixed, tag, date) and tell 3C: documents over 50 pages now process; ask whether they had been sending Sanierer documents to the BPS endpoint.
