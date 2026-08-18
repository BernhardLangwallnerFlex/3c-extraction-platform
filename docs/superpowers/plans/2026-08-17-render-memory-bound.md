# Bounding page-render memory — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the worker being OOM-killed by large-format scans, by choosing render dpi from a pixel budget instead of fixing it, and never holding more than one rendered page in memory at a time.

**Architecture:** A new `core/rendering.py` owns all bounded rasterisation: a pure `render_dpi_for()` that picks a dpi from a pixel budget, plus `render_pdf_pages_to_files()` / `concat_page_files()` which stream pages through disk so peak memory is *canvas + one page* rather than *canvas + every page*. Every `get_pixmap` call site in production code then routes through it. No API contract changes.

**Tech Stack:** Python 3.11, PyMuPDF (`fitz`), Pillow, pytest, structlog.

**Spec:** `docs/superpowers/specs/2026-08-17-render-memory-bound-design.md`

## Global Constraints

- **`CANVAS_BUDGET_PX = 200_000_000`** (200 Mpx). Calibrated against **real subdocument canvases**, measured, not estimated — see **Plan amendment 3**, which supersedes the value set in Amendment 1. Do not change this value.
- **The budget bounds the canvas's bounding box**, `max(width) × sum(heights)`, not the sum of the individual page areas. A narrow page still costs the full canvas width in white space, so the sum-of-areas figure understates what is actually allocated. See **Plan amendment 1**.
- **PIL's decompression-bomb guard must be lifted only around images we rendered ourselves.** `Image.MAX_IMAGE_PIXELS` defaults to ~89.5 Mpx and `Image.open` raises above 179 Mpx, which is below the budget. The override belongs inside `concat_page_files`, restored in a `finally`, so the guard stays in force for `_fix_image_orientation`, which opens raw customer uploads. See **Plan amendment 2**.
- **`MIN_DPI = 40`.** The floor below which the result stops being legible to the vision model. A pathological input gets a small image rather than an unreadable one, *even if that means exceeding the budget*.
- **The no-op guarantee:** a document whose pages already fit the budget must render at exactly the dpi it renders at today, producing byte-identical images. `render_dpi_for` returns `base_dpi` unchanged in that case. This is what makes the regression sweep meaningful, and it is the single most important property in this plan.
- **No API contract change.** No new field, no change to `returncode`, `returncodeReasons`, `qualityFlags` or `warnings`. Downscaling is explicitly **not** a `qualityFlags` entry — the API downsamples large images anyway, so flagging it would train the consumer to treat a normal result as suspect.
- Existing render dpi values are preserved as the *base*: 200 for subdocument images and Mistral OCR, 150 for analyze, orientation detection and `convert_file_to_images`.
- German user-facing strings: none are added or changed by this work.
- Tests live under `tests/core/`. Run with `.venv/bin/python -m pytest tests/`.

## Plan amendments

Both were found during Task 3 and settled with the maintainer before Task 4. The task text below still shows the original code; where it disagrees with an amendment, **the amendment governs**.

### Amendment 1 — the budget was calibrated on the wrong quantity

The spec justified 200 Mpx with "the largest whole file in the three regression corpora is 141.8 Mpx at 200 dpi". That figure is the **sum of the individual page areas**. The canvas `concat_page_files` actually allocates is the **bounding box**: as wide as the widest page, as tall as every page stacked. A single landscape page in an otherwise-A4 document widens all 33 pages' worth of canvas.

Measured across **441 corpus PDFs**:

| Quantity | Max observed | File |
|---|---|---|
| Sum of page areas | 227.2 Mpx | `230074677P_Splitt.pdf` |
| **Bounding-box canvas** | **331.4 Mpx** | `BPS_3.pdf` (141.8 Mpx by the old measure — 2.34× understated) |

So 200 Mpx broke the no-op guarantee on real corpus documents under *either* formula. Two changes follow:

1. `render_dpi_for` budgets `max(w) × sum(h)` over the positive-area pages, not `sum(w × h)`. For a single page the two are identical, so the per-page call sites in Tasks 4 and 5 are unaffected; for uniform-width documents they are also identical.
2. The budget was raised to 400 Mpx on the strength of that whole-file measurement. **Amendment 3 supersedes this**: the measurement was of the wrong population, and the memory estimate that accompanied it was wrong too.

### Amendment 3 — the budget was calibrated on the wrong population (supersedes Amendment 1's value)

Amendment 1 corrected the *formula* and that correction stands. Its *value* was still wrong, because it was calibrated on whole-file bounding boxes. **Production renders subdocuments, not whole files.** A 26-page file whose whole-file canvas is 331.4 Mpx splits into subdocuments whose canvases are a fraction of that; the whole-file figure is only reachable on the split-fallback path, where no Beleg was found and image fidelity does not matter.

513 real subdocument canvases from earlier pipeline runs were measured directly:

| | |
|---|---|
| Median | **3.9 Mpx** — a single A4 page |
| p99 | **73.5 Mpx** |
| Above 200 Mpx | **2 of 513**, and both are the pathological large-format documents this work exists to handle |

Nothing at all sits between 73.5 Mpx and 223.3 Mpx. A 200 Mpx budget therefore clears the p99 real subdocument by 2.7× while catching exactly the documents it is meant to catch.

Peak memory was then **measured** rather than estimated, on the crash document, and the earlier estimate was badly out:

| Budget | Canvas | Peak RSS (render + concat) | of 4 GiB |
|---|---|---|---|
| 200 Mpx | 0.60 GB | **2.07 GB** | 48% |
| 300 Mpx | 0.89 GB | 2.65 GB | 62% |
| 400 Mpx | 1.19 GB | 3.38 GB | 79% |

The full pipeline at 400 Mpx peaked at **3.69 GB** — 86% of the worker's limit, against the ~2.4 GB the plan had predicted. Peak RSS runs at roughly 2.8× the canvas bytes, so the budget is the dominant term and choosing it generously undoes the bound being added.

**`CANVAS_BUDGET_PX = 200_000_000`**, which happens to restore the original constant — but for a reason the original never had. Streaming concatenation (Task 2) is what makes this safe at a lower budget: peak is canvas plus one page, not canvas plus every page.

### Amendment 4 — analyze needs its own, much smaller budget (adds Task 7)

Found by sampling RSS across a full acceptance run. With the split site bounded, **the pipeline's memory peak moved to `analyze_document`** — 4.62 GB high-water across the run, and 2.41 GB for its render loop measured on its own. The shared 200 Mpx per-image budget never binds there, because the pathological pages are 128.8 Mpx at 150 dpi.

The waste is plain once measured: a single page produced a **67 MB PNG**, and all five were held as base64 at once (318 MB accumulated). These blocks are sent with **`"detail": "low"`**, so the API downsamples them to roughly 512px tiles regardless — every one of those megabytes is bought and thrown away.

**`ANALYZE_BUDGET_PX = 32_000_000`.** This is Task 7.

The value was first set at 8 Mpx on the reasoning that A4 (2.2 Mpx) and A3 (4.4 Mpx) at 150 dpi sit comfortably below it. That reasoning was right about A4 and A3 and wrong about the corpus: measuring the largest page of each of 441 corpus PDFs puts the **p90 at 8.70 Mpx**, so 8 Mpx sits at the ninetieth percentile and would have changed the analyze images for **64 of 441 files** — a far wider blast radius than "pathological documents".

| Budget | Files touched | Pixmap per page |
|---|---|---|
| 8 Mpx | 64/441 (14.5%) | 24 MB |
| 16 Mpx | 21/441 (4.8%) | 48 MB |
| **32 Mpx** | **14/441 (3.2%)** | **96 MB** |
| 50 Mpx | 2/441 (0.5%) | 150 MB |

32 Mpx keeps nearly all of the memory win — the worst page still falls from 128.8 Mpx to 32, and its pixmap from 386 MB to 96 MB — while cutting the number of documents whose analyze input changes by 4.5×. That matters more than usual here because the regression sweep that would vouch for those documents is blocked (see *Verification status*).

Corpus distribution, largest page per file at 150 dpi: median **2.18 Mpx**, p90 **8.70**, p95 **13.63**, p99 **48.46**, max **125.60**.

### Amendment 2 — PIL's decompression-bomb guard fires in production

`Image.MAX_IMAGE_PIXELS` defaults to 89,478,485 and `Image.open` raises `DecompressionBombError` above twice that (178,956,970) — **below the budget**. `concat_page_files` reopens the per-page PNGs it just wrote, so a single large-format page inside the budget raises. Verified empirically: a 4554 × 6516 pt page renders to 198.0 Mpx and `concat_page_files` raises. Left unfixed this trades an OOM crash for an exception crash.

The override goes **in production code**, inside `concat_page_files`, restored in a `finally`:

```python
prev_limit = Image.MAX_IMAGE_PIXELS
Image.MAX_IMAGE_PIXELS = None
try:
    ...open, paste and close each page...
finally:
    Image.MAX_IMAGE_PIXELS = prev_limit
```

Scoped deliberately: these are files this module rendered moments earlier, not untrusted input. `_fix_image_orientation` opens raw customer uploads directly and must keep the guard.

---

### Task 1: `core/rendering.py` — the budget helper

The pure function everything else depends on. No I/O, no product knowledge, no `fitz` needed to test it.

**Files:**
- Create: `core/rendering.py`
- Test: `tests/core/test_rendering.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `CANVAS_BUDGET_PX: int = 200_000_000`
  - `MIN_DPI: int = 40`
  - `render_dpi_for(page_sizes: Sequence[tuple[float, float]], base_dpi: int, budget_px: int = CANVAS_BUDGET_PX) -> int` — `page_sizes` are `(width, height)` pairs in **PDF points** (1/72 inch), as read from `page.rect`.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_rendering.py`:

```python
"""Bounding rendered pixels.

A page's pixel count grows with the square of the dpi, and every pixel costs
three bytes as RGB. A fixed dpi therefore means an unbounded memory footprint,
which is how a five-page BPS document with 1.6 x 2.3 m pages killed a 4 GiB
worker three times in a row on 2026-08-17.
"""
import pytest

from core.rendering import CANVAS_BUDGET_PX, MIN_DPI, render_dpi_for

A4 = (595.0, 841.0)
# The two page geometries from the document that caused the OOM, in points.
HUGE_A = (4554.0, 6516.0)
HUGE_B = (4177.0, 6095.0)


def _total_px(page_sizes, dpi):
    return sum(w * h for w, h in page_sizes) * (dpi / 72.0) ** 2


def test_pages_within_budget_return_base_dpi_unchanged():
    # The no-op guarantee: every document that works today must render at
    # exactly the dpi it renders at today.
    assert render_dpi_for([A4] * 5, base_dpi=200) == 200


def test_single_oversized_page_scales_down_within_budget():
    dpi = render_dpi_for([HUGE_A], base_dpi=200)
    assert dpi < 200
    assert _total_px([HUGE_A], dpi) <= CANVAS_BUDGET_PX


def test_many_normal_pages_that_collectively_exceed_budget_scale_down():
    pages = [A4] * 500
    assert _total_px(pages, 200) > CANVAS_BUDGET_PX
    dpi = render_dpi_for(pages, base_dpi=200)
    assert dpi < 200
    assert _total_px(pages, dpi) <= CANVAS_BUDGET_PX


def test_result_never_goes_below_min_dpi():
    absurd = [(100_000.0, 100_000.0)] * 50
    assert render_dpi_for(absurd, base_dpi=200) == MIN_DPI


def test_empty_page_list_returns_base_dpi():
    assert render_dpi_for([], base_dpi=200) == 200


def test_zero_area_pages_return_base_dpi():
    # Degenerate geometry must not divide by zero.
    assert render_dpi_for([(0.0, 0.0)], base_dpi=150) == 150


def test_real_failing_geometry_fits_the_budget():
    # The actual document: one A4 cover page plus four large-format scans.
    pages = [A4, HUGE_A, HUGE_B, HUGE_A, HUGE_B]
    assert _total_px(pages, 200) > 800_000_000  # ~854.7 Mpx, the crash
    dpi = render_dpi_for(pages, base_dpi=200)
    assert MIN_DPI <= dpi < 200
    assert _total_px(pages, dpi) <= CANVAS_BUDGET_PX


@pytest.mark.parametrize("base_dpi", [150, 200])
def test_budget_is_honoured_for_each_base_dpi(base_dpi):
    pages = [HUGE_A, HUGE_B]
    assert _total_px(pages, render_dpi_for(pages, base_dpi)) <= CANVAS_BUDGET_PX
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_rendering.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.rendering'`

- [ ] **Step 3: Write the implementation**

Create `core/rendering.py`:

```python
"""Bounded page rendering.

Rasterising a PDF page is the one place in this pipeline where a document's
physical size turns directly into resident memory: a page's pixel count grows
with the square of the dpi, and every pixel costs three bytes as RGB. A fixed
dpi therefore means an unbounded memory footprint — which is how a five-page
BPS document with roughly 1.6 x 2.3 m pages killed a 4 GiB worker three times
in a row on 2026-08-17.

Two bounds live here: choose the dpi from a pixel budget rather than fixing
it, and never hold more than one rendered page in memory at a time.
"""
from __future__ import annotations

from math import sqrt
from pathlib import Path
from typing import Sequence

# Total rendered pixels one concatenated image — or one standalone page image
# — may occupy. 200 Mpx as RGB is 600 MB, which leaves headroom under the
# worker's 4 GiB limit even alongside a page pixmap.
#
# Calibrated, not chosen for roundness: the largest whole file in the three
# regression corpora renders to 141.8 Mpx at 200 dpi, and a subdocument is a
# subset of a file. Every document that works today therefore renders at
# exactly the dpi it renders at today, byte for byte.
CANVAS_BUDGET_PX = 200_000_000

# Below roughly this resolution, body text on a scanned page stops being
# legible to the vision model. A pathological input gets a small image rather
# than an unreadable one, even where that means exceeding the budget.
MIN_DPI = 40


def render_dpi_for(
    page_sizes: Sequence[tuple[float, float]],
    base_dpi: int,
    budget_px: int = CANVAS_BUDGET_PX,
) -> int:
    """Pick the dpi at which `page_sizes` render within `budget_px` pixels.

    `page_sizes` are (width, height) pairs in PDF points (1/72 inch), as read
    from `page.rect`. Returns `base_dpi` unchanged whenever the pages already
    fit — the no-op guarantee that keeps existing documents byte-identical.

    Rendered area grows with the square of the dpi, so the scale factor is the
    square root of the ratio of budget to actual.
    """
    total_pt2 = sum(w * h for w, h in page_sizes if w > 0 and h > 0)
    if total_pt2 <= 0:
        return base_dpi

    total_px_at_base = total_pt2 * (base_dpi / 72.0) ** 2
    if total_px_at_base <= budget_px:
        return base_dpi

    # int() truncates, which keeps the result inside the budget rather than
    # rounding back over it.
    return max(MIN_DPI, int(base_dpi * sqrt(budget_px / total_px_at_base)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_rendering.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add core/rendering.py tests/core/test_rendering.py
git commit -m "feat: add render_dpi_for — pick render dpi from a pixel budget"
```

---

### Task 2: Streaming render-and-concatenate

The two functions that make peak memory *canvas + one page*. Still no pipeline changes — this task delivers and tests the mechanism in isolation.

**Files:**
- Modify: `core/rendering.py`
- Test: `tests/core/test_rendering.py` (append)

**Interfaces:**
- Consumes: `render_dpi_for`, `CANVAS_BUDGET_PX` from Task 1.
- Produces:
  - `render_pdf_pages_to_files(pdf_path, out_dir, base_dpi, budget_px=CANVAS_BUDGET_PX, prefix="page") -> list[tuple[Path, int, int]]` — one PNG per page under `out_dir`, returning `(path, width_px, height_px)` in page order.
  - `concat_page_files(page_files, out_path) -> Path` — vertical concatenation of those PNGs onto a white RGB canvas.

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_rendering.py`:

```python
import fitz
from PIL import Image

from core.rendering import concat_page_files, render_pdf_pages_to_files


def _make_pdf(path, page_sizes):
    doc = fitz.open()
    for i, (w, h) in enumerate(page_sizes):
        page = doc.new_page(width=w, height=h)
        page.insert_text((72, 72), f"Seite {i + 1}")
    doc.save(path)
    doc.close()
    return path


def test_render_writes_one_file_per_page_with_recorded_sizes(tmp_path):
    pdf = _make_pdf(tmp_path / "in.pdf", [A4, A4, A4])

    rendered = render_pdf_pages_to_files(pdf, tmp_path, base_dpi=200)

    assert len(rendered) == 3
    for page_path, width, height in rendered:
        assert page_path.exists()
        with Image.open(page_path) as img:
            assert (img.width, img.height) == (width, height)


def test_render_uses_base_dpi_when_pages_fit(tmp_path):
    # The no-op guarantee, end to end: a normal page renders at exactly the
    # size a fixed 200 dpi would have produced.
    pdf = _make_pdf(tmp_path / "in.pdf", [A4])

    (_path, width, height), = render_pdf_pages_to_files(pdf, tmp_path, base_dpi=200)

    with fitz.open(pdf) as doc:
        expected = doc[0].get_pixmap(dpi=200)
    assert (width, height) == (expected.width, expected.height)


def test_render_downscales_an_oversized_page(tmp_path):
    pdf = _make_pdf(tmp_path / "in.pdf", [HUGE_A])

    (_path, width, height), = render_pdf_pages_to_files(pdf, tmp_path, base_dpi=200)

    assert width * height <= CANVAS_BUDGET_PX


def test_concat_produces_expected_canvas_dimensions(tmp_path):
    pdf = _make_pdf(tmp_path / "in.pdf", [A4, (300.0, 400.0), A4])
    rendered = render_pdf_pages_to_files(pdf, tmp_path, base_dpi=200)

    out = concat_page_files(rendered, tmp_path / "out.png")

    with Image.open(out) as img:
        assert img.width == max(w for _, w, _ in rendered)
        assert img.height == sum(h for _, _, h in rendered)
        assert img.mode == "RGB"


def test_concat_matches_the_previous_implementation(tmp_path):
    # The old code rendered every page into a list, then pasted them onto a
    # canvas sized from that list. Same pixels, different memory profile.
    pdf = _make_pdf(tmp_path / "in.pdf", [A4, A4])

    with fitz.open(pdf) as doc:
        old_pages = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            mode = "RGB" if pix.alpha == 0 else "RGBA"
            old_pages.append(Image.frombytes(mode, [pix.width, pix.height], pix.samples))
    old = Image.new(
        "RGB",
        (max(i.width for i in old_pages), sum(i.height for i in old_pages)),
        color=(255, 255, 255),
    )
    y = 0
    for img in old_pages:
        old.paste(img, (0, y))
        y += img.height

    new_path = concat_page_files(
        render_pdf_pages_to_files(pdf, tmp_path, base_dpi=200), tmp_path / "new.png"
    )
    with Image.open(new_path) as new:
        assert new.convert("RGB").tobytes() == old.tobytes()


def test_concat_holds_at_most_one_page_open_at_a_time(tmp_path, monkeypatch):
    # The whole point of the change: the canvas plus one page, not the canvas
    # plus every page. Asserted through a counting fake, not by measuring RSS.
    pdf = _make_pdf(tmp_path / "in.pdf", [A4] * 6)
    rendered = render_pdf_pages_to_files(pdf, tmp_path, base_dpi=200)

    import core.rendering as rendering

    real_open = rendering.Image.open
    live = {"now": 0, "max": 0}

    class _Tracked:
        def __init__(self, path):
            self._img = real_open(path)

        def __enter__(self):
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            return self._img.__enter__()

        def __exit__(self, *exc):
            live["now"] -= 1
            return self._img.__exit__(*exc)

    monkeypatch.setattr(rendering.Image, "open", _Tracked)

    concat_page_files(rendered, tmp_path / "out.png")

    assert live["max"] == 1


def test_concat_rejects_an_empty_page_list(tmp_path):
    with pytest.raises(ValueError):
        concat_page_files([], tmp_path / "out.png")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_rendering.py -v`
Expected: FAIL — `ImportError: cannot import name 'concat_page_files'`

- [ ] **Step 3: Write the implementation**

Add to `core/rendering.py` — imports first (`fitz`, `Image`, `structlog` alongside the existing ones):

```python
import fitz
import structlog
from PIL import Image

_log = structlog.get_logger()
```

then:

```python
def render_pdf_pages_to_files(
    pdf_path,
    out_dir,
    base_dpi: int,
    budget_px: int = CANVAS_BUDGET_PX,
    prefix: str = "page",
) -> list[tuple[Path, int, int]]:
    """Render every page of `pdf_path` to its own PNG under `out_dir`.

    Returns (path, width_px, height_px) per page, in page order.

    Exactly one page pixmap is alive at any moment: each is written to disk and
    dropped before the next is rendered. The dpi is chosen once for the whole
    file so that pages stay visually consistent with one another within the
    concatenated image.
    """
    out_dir = Path(out_dir)
    rendered: list[tuple[Path, int, int]] = []

    with fitz.open(pdf_path) as doc:
        dpi = render_dpi_for(
            [(page.rect.width, page.rect.height) for page in doc], base_dpi, budget_px
        )
        if dpi != base_dpi:
            _log.warning(
                "render_downscaled",
                reason="pages exceed the pixel budget at the base dpi",
                base_dpi=base_dpi,
                dpi=dpi,
                pages=len(doc),
                budget_px=budget_px,
            )
        for index, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi)
            page_path = out_dir / f"{prefix}_{index:04d}.png"
            pix.save(str(page_path))
            rendered.append((page_path, pix.width, pix.height))
            del pix

    return rendered


def concat_page_files(page_files: Sequence[tuple[Path, int, int]], out_path) -> Path:
    """Paste per-page PNGs onto one vertically concatenated canvas.

    Canvas dimensions come from the sizes recorded at render time, so the
    canvas is allocated once and each page is opened, pasted and closed in
    turn. Peak memory is the canvas plus a single page — not the canvas plus
    every page, which is what made a large-format document fatal.
    """
    if not page_files:
        raise ValueError("concat_page_files requires at least one page")

    max_width = max(width for _path, width, _height in page_files)
    total_height = sum(height for _path, _width, height in page_files)

    canvas = Image.new("RGB", (max_width, total_height), color=(255, 255, 255))
    y = 0
    for page_path, _width, height in page_files:
        with Image.open(page_path) as page_img:
            # No mask: an RGBA page is converted to RGB by paste, exactly as
            # the previous implementation did.
            canvas.paste(page_img, (0, y))
        y += height

    out_path = Path(out_path)
    canvas.save(out_path)
    canvas.close()
    return out_path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_rendering.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add core/rendering.py tests/core/test_rendering.py
git commit -m "feat: stream page renders through disk so peak memory is canvas + one page"
```

---

### Task 3: Wire the subdocument image render — the crash site

`core/pipeline.py:307-327` is where the OOM happened. It renders every page of a subdocument at a fixed 200 dpi, accumulates all of them in `page_images`, and only then allocates the canvas.

**Files:**
- Modify: `core/pipeline.py` (imports; the constant; `split_document_into_invoices`, currently lines 307-327)
- Test: `tests/core/test_pipeline_render.py` (create)

**Interfaces:**
- Consumes: `render_pdf_pages_to_files`, `concat_page_files` from Task 2.
- Produces: `SUBDOC_RENDER_DPI = 200` as a module constant in `core/pipeline.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_pipeline_render.py`. The `object.__new__(Pipeline)` construction mirrors `tests/core/test_pipeline_split_fallback.py` — it skips the I/O-heavy `__init__`.

```python
"""The subdocument image render, bounded.

This is the call site that OOM-killed a BPS worker three times on 2026-08-17.
"""
from pathlib import Path

import fitz
import pytest
from PIL import Image

from core.pipeline import Pipeline
from core.rendering import CANVAS_BUDGET_PX


class _CapturingStorage:
    def __init__(self):
        self.blobs = {}

    def write_text(self, key, text):
        self.blobs[key] = text.encode()

    def write_bytes(self, key, data, content_type=None):
        self.blobs[key] = data


def _make_pipeline(pdf_path, work_dir, invoice_pages, page_count):
    pipe = object.__new__(Pipeline)
    pipe.storage = _CapturingStorage()
    pipe.analysis_dict = {"invoice_pages": invoice_pages}
    pipe.file_type = "pdf"
    pipe.local_input_path = str(pdf_path)
    pipe.work_dir = Path(work_dir)
    pipe.output_prefix = "az://invoices/processed-bps"
    pipe.stem = "abc"
    pipe.subdocuments = []
    pipe.markdown_by_page = {n: f"seite {n}" for n in range(1, page_count + 1)}
    return pipe


def _make_pdf(path, page_sizes):
    doc = fitz.open()
    for i, (w, h) in enumerate(page_sizes):
        doc.new_page(width=w, height=h).insert_text((72, 72), f"Seite {i + 1}")
    doc.save(path)
    doc.close()
    return path


def test_normal_document_image_is_unchanged(tmp_path):
    # The no-op guarantee at pipeline level: an A4 document still renders at
    # 200 dpi, so the regression corpora produce the images they always have.
    pdf = _make_pdf(tmp_path / "in.pdf", [(595.0, 841.0)] * 2)
    pipe = _make_pipeline(pdf, tmp_path, {"R-1": [1, 2]}, 2)

    pipe.split_document_into_invoices()

    with fitz.open(pdf) as doc:
        expected_w = doc[0].get_pixmap(dpi=200).width
        expected_h = sum(p.get_pixmap(dpi=200).height for p in doc)

    img_key = pipe.subdocuments[0].image_key
    out = tmp_path / "written.png"
    out.write_bytes(pipe.storage.blobs[img_key])
    with Image.open(out) as img:
        assert (img.width, img.height) == (expected_w, expected_h)


def test_oversized_document_renders_within_the_budget(tmp_path):
    # The failing geometry: one A4 cover page plus four large-format scans.
    pdf = _make_pdf(
        tmp_path / "in.pdf",
        [(595.0, 841.0), (4554.0, 6516.0), (4177.0, 6095.0), (4554.0, 6516.0), (4177.0, 6095.0)],
    )
    pipe = _make_pipeline(pdf, tmp_path, {"R-1": [1, 2, 3, 4, 5]}, 5)

    pipe.split_document_into_invoices()

    img_key = pipe.subdocuments[0].image_key
    out = tmp_path / "written.png"
    out.write_bytes(pipe.storage.blobs[img_key])
    with Image.open(out) as img:
        assert img.width * img.height <= CANVAS_BUDGET_PX


def test_per_page_temp_files_do_not_survive(tmp_path):
    # They are scratch, and on the failing document they are large scratch.
    pdf = _make_pdf(tmp_path / "in.pdf", [(595.0, 841.0)] * 3)
    pipe = _make_pipeline(pdf, tmp_path, {"R-1": [1, 2], "R-2": [3]}, 3)

    pipe.split_document_into_invoices()

    assert list(tmp_path.glob("subdoc*_page_*.png")) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_render.py -v`
Expected: `test_oversized_document_renders_within_the_budget` FAILS on the budget assertion. The other two pass against the current code.

**Caution:** against the *unfixed* code this test reproduces the production OOM — it tries to allocate roughly 5 GB. On a developer machine that means heavy swapping for a minute or two, not a crash, but do not run it on a memory-constrained box. If the process is killed rather than failing the assertion, that is itself the red-fail this step is looking for: record it and move to Step 3.

- [ ] **Step 3: Write the implementation**

In `core/pipeline.py`, add to the imports near `from core.utils import log_retry, sampling_params`:

```python
from core.rendering import concat_page_files, render_dpi_for, render_pdf_pages_to_files
```

Add a module constant beside the existing German warning constants:

```python
# Base render resolutions. These are what the pipeline asks for; the actual dpi
# is whatever core.rendering can deliver inside the pixel budget.
SUBDOC_RENDER_DPI = 200
ANALYZE_RENDER_DPI = 150
```

Replace the body of step 3 in `split_document_into_invoices` — currently lines 307-327, from the `# 3) render pages...` comment through the `self.storage.write_bytes(img_key, ...)` call:

```python
                # 3) render pages into one concatenated image locally, then upload/store
                #
                # Rendered page by page through disk rather than accumulated in
                # a list: a large-format scan makes "every page pixmap plus the
                # canvas" several gigabytes, and the worker has four.
                page_files = render_pdf_pages_to_files(
                    subdoc_pdf_local,
                    self.work_dir,
                    base_dpi=SUBDOC_RENDER_DPI,
                    prefix=f"subdoc{document_number}_page",
                )
                subdoc_img_local = self.work_dir / Path(img_key).name
                concat_page_files(page_files, subdoc_img_local)
                for page_path, _width, _height in page_files:
                    page_path.unlink(missing_ok=True)

                self.storage.write_bytes(img_key, subdoc_img_local.read_bytes(), content_type="image/png")
```

Delete the now-unused `page_images` list, the `total_height`/`max_width` computation and the old paste loop. If `from PIL import Image` becomes unused in `core/pipeline.py`, leave it — `_fix_image_orientation` and `_fix_pdf_orientation` still use it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_render.py tests/core/test_pipeline_split_fallback.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite for regressions**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS, no fewer tests than before this task.

- [ ] **Step 6: Commit**

```bash
git add core/pipeline.py tests/core/test_pipeline_render.py
git commit -m "fix: bound subdocument image render — the OOM site"
```

---

### Task 4: Wire the analyze-document render

`core/pipeline.py:208-220` renders one image per page for `analyze_document`. It rendered the failing document's five pages at 150 dpi — about 480 Mpx across five separate PNGs — and survived only because each is encoded and released before the next. The flaw is the same; the budget applies **per image** here, because these go to the API as separate images and each one, not their sum, is what has to fit.

**Files:**
- Modify: `core/pipeline.py` (`analyze_document`, currently lines 208-220)
- Test: `tests/core/test_pipeline_render.py` (append)

**Interfaces:**
- Consumes: `render_dpi_for`, `ANALYZE_RENDER_DPI` from Task 3.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_pipeline_render.py`:

```python
def test_analyze_renders_each_page_within_the_budget(tmp_path, monkeypatch):
    # Per-image budget: analyze sends pages as separate images, so each one is
    # what has to fit, not their sum.
    import core.pipeline as pipeline

    pdf = _make_pdf(tmp_path / "in.pdf", [(4554.0, 6516.0), (595.0, 841.0)])
    pipe = _make_pipeline(pdf, tmp_path, {}, 2)
    pipe.markdown_with_pages_numbers = "text"
    pipe.product_config = type("C", (), {"analyze_prompt_builder": None})()

    captured = {}

    def _fake_call(call_fn, client, model, blocks):
        captured["blocks"] = blocks
        raise RuntimeError("stop after building the blocks")

    monkeypatch.setattr(pipeline, "call_with_vision_fallback", _fake_call)
    monkeypatch.setattr(pipeline, "AzureOpenAI", lambda **kw: object())

    with pytest.raises(RuntimeError):
        pipe.analyze_document()

    import base64
    import io

    images = [b for b in captured["blocks"] if b["type"] == "image_url"]
    assert len(images) == 2
    for block in images:
        raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as img:
            assert img.width * img.height <= CANVAS_BUDGET_PX
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_render.py::test_analyze_renders_each_page_within_the_budget -v`
Expected: FAIL on the budget assertion for the first (large) page.

- [ ] **Step 3: Write the implementation**

Replace the render loop inside `analyze_document`:

```python
        if self.file_type == "pdf":
            with fitz.open(self.local_input_path) as doc:
                for page in doc:
                    # Budget applied per page: these reach the API as separate
                    # images, so each one — not their sum — has to fit.
                    dpi = render_dpi_for(
                        [(page.rect.width, page.rect.height)], ANALYZE_RENDER_DPI
                    )
                    pix = page.get_pixmap(dpi=dpi)
                    img_bytes = pix.tobytes("png")
                    del pix
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    content_blocks.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "low",
                        },
                    })
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_render.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/pipeline.py tests/core/test_pipeline_render.py
git commit -m "fix: bound the per-page render in analyze_document"
```

---

### Task 5: The remaining render sites

The spec requires that no unbounded `get_pixmap` be left behind — otherwise the next large document simply finds a different way to kill the worker. There are three more in production code. Two are named in the spec; the third (`ocr_mistral_v2.py:53`) was found while reading the code and renders at **200 dpi**, the highest of any per-page site.

| Site | Base dpi | Purpose |
|---|---|---|
| `core/pipeline.py:140` (`_fix_pdf_orientation`) | 150 | Tesseract OSD rotation detection |
| `core/utils.py:190` (`convert_file_to_images`) | 150 | direct-PDF vision input |
| `core/ocr/ocr_mistral_v2.py:53` (`_process_pdf`) | 200 | production Mistral OCR |

All three already render one page at a time, so the fix is the dpi bound alone. `core/ocr/ocr_mistral.py` and `core/ocr/ocr_tesseract.py` also call `get_pixmap`, but neither is wired into production — `DualOCRProcessor` imports only `ocr_mistral_v2` and `ocr_azure_docintel`. Leave them alone.

**Scope note:** bounding `ocr_mistral_v2` is a **memory** fix, in scope under "no unbounded `get_pixmap`". It does **not** claim to fix Mistral's `"Image pixels are above the allowed limits"` rejection — Mistral's own limit is far below 200 Mpx, and the spec puts that rejection out of scope. Do not widen this task to chase it.

**Files:**
- Modify: `core/pipeline.py` (`_fix_pdf_orientation`, line 140)
- Modify: `core/utils.py` (`convert_file_to_images`, line 190)
- Modify: `core/ocr/ocr_mistral_v2.py` (`_process_pdf`, line 53)
- Test: `tests/core/test_rendering_call_sites.py` (create)

**Interfaces:**
- Consumes: `render_dpi_for`, `CANVAS_BUDGET_PX` from Task 1.
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_rendering_call_sites.py`:

```python
"""No unbounded get_pixmap left in production code.

Each of these sites renders one page at a time, so memory never reached the
subdocument site's several gigabytes — but a single large-format page at a
fixed dpi is still hundreds of megabytes against a 4 GiB limit.
"""
import fitz
import pytest
from PIL import Image

from core.rendering import CANVAS_BUDGET_PX

HUGE = (4554.0, 6516.0)


def _make_pdf(path, page_sizes):
    doc = fitz.open()
    for i, (w, h) in enumerate(page_sizes):
        doc.new_page(width=w, height=h).insert_text((72, 72), f"Seite {i + 1}")
    doc.save(path)
    doc.close()
    return path


def test_convert_file_to_images_bounds_each_page(tmp_path):
    from core.utils import convert_file_to_images

    pdf = _make_pdf(tmp_path / "in.pdf", [HUGE, (595.0, 841.0)])

    paths = convert_file_to_images(str(pdf))

    assert len(paths) == 2
    for path in paths:
        with Image.open(path) as img:
            assert img.width * img.height <= CANVAS_BUDGET_PX


def test_mistral_ocr_bounds_each_rendered_page(tmp_path, monkeypatch):
    from core.ocr.ocr_mistral_v2 import MistralOCRProcessor

    pdf = _make_pdf(tmp_path / "in.pdf", [HUGE])

    engine = object.__new__(MistralOCRProcessor)
    sizes = []

    def _fake_process_image(image_path):
        with Image.open(image_path) as img:
            sizes.append(img.width * img.height)
        return "markdown"

    monkeypatch.setattr(engine, "_process_image", _fake_process_image)

    result = engine._process_pdf(str(pdf))

    assert result == {1: "markdown"}
    assert sizes and all(px <= CANVAS_BUDGET_PX for px in sizes)


def test_orientation_detection_bounds_each_page(tmp_path, monkeypatch):
    import core.pipeline as pipeline
    from core.pipeline import Pipeline

    pdf = _make_pdf(tmp_path / "in.pdf", [HUGE])

    pipe = object.__new__(Pipeline)
    pipe.local_input_path = str(pdf)
    pipe.work_dir = tmp_path

    seen = []

    def _fake_detect(img):
        seen.append(img.width * img.height)
        return 0

    monkeypatch.setattr(Pipeline, "_detect_rotation", staticmethod(_fake_detect))

    pipe._fix_pdf_orientation()

    assert seen and all(px <= CANVAS_BUDGET_PX for px in seen)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_rendering_call_sites.py -v`
Expected: all three FAIL on the budget assertion.

- [ ] **Step 3: Write the implementation**

In `core/pipeline.py`, `_fix_pdf_orientation` — replace `pix = page.get_pixmap(dpi=150)`:

```python
                dpi = render_dpi_for(
                    [(page.rect.width, page.rect.height)], ORIENTATION_RENDER_DPI
                )
                pix = page.get_pixmap(dpi=dpi)
```

and add `ORIENTATION_RENDER_DPI = 150` to the render-dpi constants added in Task 3.

In `core/utils.py` — add `from core.rendering import render_dpi_for` to the imports, then in `convert_file_to_images` replace `pix = page.get_pixmap(dpi=150)`:

```python
                dpi = render_dpi_for([(page.rect.width, page.rect.height)], 150)
                pix = page.get_pixmap(dpi=dpi)
```

In `core/ocr/ocr_mistral_v2.py` — add `from core.rendering import render_dpi_for` to the imports, then in `_process_pdf` replace `pix = page.get_pixmap(dpi=200)`:

```python
                # One page at a time, but a large-format page at a fixed 200 dpi
                # is still hundreds of megabytes.
                dpi = render_dpi_for([(page.rect.width, page.rect.height)], 200)
                pix = page.get_pixmap(dpi=dpi)
```

**Check for an import cycle** before committing: `core/rendering.py` imports only `fitz`, `PIL`, `structlog` and stdlib, so importing it from `core/utils.py` is safe. If a cycle appears anyway, fix it by keeping `core/rendering.py` free of project imports — never by duplicating the helper.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_rendering_call_sites.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/pipeline.py core/utils.py core/ocr/ocr_mistral_v2.py tests/core/test_rendering_call_sites.py
git commit -m "fix: bound the remaining production render sites"
```

---

### Task 6: Verification against the real corpora

Unit tests prove the mechanism. Only the corpora prove the no-op guarantee held, and only the failing document proves the crash is gone. **This task is run by the controller, not delegated** — it needs live API keys and costs roughly $1 in LLM calls per sweep.

**Files:** none modified. This task produces evidence, not code.

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: a go/no-go on merging.

- [ ] **Step 1: Full unit suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS, with a test count at least 219 + the new tests.

- [ ] **Step 2: Acceptance — the document that caused the OOM**

`25K10201C91.pdf` in the repository root is the culprit: five pages, of which four are 4554 × 6516 pt and 4178 × 6095 pt, totalling **854.8 Mpx** at a fixed 200 dpi. (It is the same document as `~/Downloads/bps-oom-20260817-c16e5672.pdf`; measured geometry matches page for page.)

```bash
PRODUCT_NAME=bps STORAGE_BACKEND=local CLEANUP_ARTIFACTS=false \
  .venv/bin/python scripts/extract_local.py 25K10201C91.pdf
```

Expected: completes without being killed, and returns **at least one subdocument**. Confirm in the logs that a `render_downscaled` event fired — if it did not, the budget was never exercised and the run proves nothing about this fix. Note the resulting dpi.

- [ ] **Step 3: Acceptance — the two no-op controls**

`26551118700.pdf` and `26551430800.pdf`, also in the repository root, are the 33-page BPS claim files from the content-policy incident. Their **whole-file** bounding-box canvases at 200 dpi are 254.9 Mpx and 191.5 Mpx, but production splits them into subdocuments whose canvases are far smaller — which is exactly the distinction Amendment 3 turns on. Expect no `render_downscaled` event from either; if one fires, read the page range it names before concluding anything, because a fired event here means a subdocument genuinely exceeded 200 Mpx rather than that the budget is too low.

They are therefore the sharpest available test of the no-op guarantee: real production documents, near the top of the observed size range, that must render **exactly as they do today**.

```bash
PRODUCT_NAME=bps STORAGE_BACKEND=local CLEANUP_ARTIFACTS=false \
  .venv/bin/python scripts/extract_local.py 26551118700.pdf
```

Expected: no `render_downscaled` event at all. One firing here would mean the budget is biting real documents and the calibration is wrong — stop and report rather than proceeding.

All three are customer data. They are covered by the `*.pdf` rule in `.gitignore`; never commit them or paste their contents.

- [ ] **Step 4: Regression sweep — the no-op guarantee at corpus scale**

This is the gate that matters. Every document in the corpora should render at exactly the dpi it rendered at before, so results should be unchanged:

```bash
.venv/bin/python scripts/returncode_sweep.py <corpus-dir> --expect 100
```

Run for all three corpora. Expected: green, matching the 29 PDFs / 37 subdocuments baseline. A `render_downscaled` event anywhere in the sweep means a corpus document exceeded the budget — investigate before merging, because the calibration in Amendment 1 says none should.

Measure the **actual** canvas, not the predicted one. Amendment 1 exists because a proxy figure was trusted over the thing being allocated; the check that would have caught it is confirming no written subdocument PNG exceeds `CANVAS_BUDGET_PX`.

- [ ] **Step 5: Regression check, if its references are still valid**

Run: `.venv/bin/python scripts/regression_check.py`
If its stored references predate the returncode work they may be stale — if so, say so rather than treating a mismatch as a failure of this change.

- [ ] **Step 6: Commit any evidence worth keeping**

No code commit is expected here. Report the acceptance dpi, the sweep result, and the unit test count.

---

### Task 7: A separate, much smaller budget for analyze

Added by **Amendment 4**. `analyze_document` is the pipeline's memory ceiling now that the split site is bounded, and the memory it spends is spent on nothing: its images go to the API with `"detail": "low"`.

**Files:**
- Modify: `core/rendering.py` (add the constant)
- Modify: `core/pipeline.py` (`analyze_document` — pass the new budget)
- Test: `tests/core/test_pipeline_render.py` (append)

**Interfaces:**
- Consumes: `render_dpi_for` from Task 1.
- Produces: `ANALYZE_BUDGET_PX = 32_000_000` in `core/rendering.py`.

- [ ] **Step 1: Write the failing tests**

Two properties matter, and the first is the one that keeps this safe:

1. **No-op for ordinary pages.** An A4 page (595 × 841 pt) and an A3 page (842 × 1191 pt) at 150 dpi are 2.2 and 4.4 Mpx, both far under 32 Mpx, so both must render at exactly 150 dpi and produce byte-identical images to today.
2. **The pathological page is capped.** A 4554 × 6516 pt page is 128.8 Mpx at 150 dpi and must come back at or under 32 Mpx. Unlike the 8 Mpx value this replaces, 32 Mpx is reachable without hitting the `MIN_DPI = 40` floor, so the cap holds exactly.

Measure the resulting images with the existing `_png_dimensions` helper, which reads the PNG header via `struct` — do not use `Image.open`. Follow the pattern of the spy test already in this file for reaching into `analyze_document`'s content blocks.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_render.py -v`
Expected: the cap test FAILS (the page renders at 128.8 Mpx); the no-op tests already pass and must keep passing.

- [ ] **Step 3: Write the implementation**

In `core/rendering.py`, beside `CANVAS_BUDGET_PX`:

```python
# The analyze step sends one image per page with "detail": "low", which the API
# downsamples to roughly 512px tiles — so resolution beyond a legible page scan
# is bought and thrown away. Measured: a single large-format page produced a
# 67 MB PNG, and holding five of them as base64 put the analyze loop at 2.41 GB,
# the pipeline's ceiling once the split site was bounded.
#
# 32 Mpx sits above the corpus p95 (13.6 Mpx) so ordinary pages — including
# large-format scans — stay byte-identical, while capping the extremes.
ANALYZE_BUDGET_PX = 32_000_000
```

In `core/pipeline.py`, `analyze_document`, pass it explicitly:

```python
                    dpi = render_dpi_for(
                        [(page.rect.width, page.rect.height)],
                        ANALYZE_RENDER_DPI,
                        budget_px=ANALYZE_BUDGET_PX,
                    )
```

and add `ANALYZE_BUDGET_PX` to the `core.rendering` import.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_render.py -v`, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add core/rendering.py core/pipeline.py tests/core/test_pipeline_render.py
git commit -m "fix: give analyze its own budget — detail:low does not need 67 MB pages"
```

---

## Verification status

Recorded 2026-08-18, at the end of the branch.

**Passed:**

- **Full unit suite** — 247 passing, warning-free.
- **Acceptance on the crash document** (`25K10201C91.pdf`, 854.8 Mpx at a fixed 200 dpi). Exit 0, where production was SIGKILLed three times. Returns 1 Beleg, `returncode` 100, 7 line items, flagged `SINGLE_ENGINE_OCR` — the first real-world firing of that flag, since Mistral rejected the oversized pages. `render_downscaled` fired at 200 → 95 dpi.
- **Peak RSS, sampled across the whole run:** 4.62 GB before the analyze budget, **2.86 GB** after. Against a 4 GiB worker limit.
- **No-op control** `26551118700.pdf` (33 pages): completed with **zero** `render_downscaled` events, 4 subdocuments, all `returncode` 100.
- **Arithmetic no-op argument across 444 PDFs**, with a stated limit. A subdocument's canvas cannot exceed its whole file's bounding box, so a whole-file figure under budget implies every subdocument of it renders byte-identically. On that basis **439 of 444 are untouched** at the split site; only 5 could possibly downscale, and 2 of those are the pathological documents this work targets.

  **The premise holds for the geometry as uploaded, which is not always the geometry that gets rendered.** `_fix_pdf_orientation` may rewrite the input with pages rotated 90°, and the bounding box is *not* rotation-invariant: one portrait page turned landscape in an otherwise-portrait document widens the whole canvas by roughly 40% for A4. The subdocument ⊆ whole-file inequality is sound; the measurement feeding it is pre-rotation. The exposure is small — only 5 files sit anywhere near the line and the median real subdocument canvas is 3.9 Mpx — and the failure mode is benign, a document downscaling when the arithmetic said it would not. But it is an argument about uploaded geometry, not a proof about rendered geometry, and the sweep is what closes the gap.

**Blocked:**

- **The regression sweep** (`scripts/returncode_sweep.py`) and the second no-op control could not run: Azure returns **429 rate-limit errors** on `gpt-5.4` in `germanywestcentral` after a handful of documents. This is environmental, not a code failure — the same error aborted a control run twice, in `analyze_document`, after tenacity exhausted its retries.
- `26551430800.pdf` is nonetheless **provably** a no-op: its whole-file canvas is 191.5 Mpx, under the 200 Mpx budget, and its largest page is 4.7 Mpx at 150 dpi, under the 32 Mpx analyze budget. Neither site can downscale it.

**Run before promoting to production — treat this as a hard gate, not a formality:** the sweep across all three corpora, once quota recovers.

Its value is the 14 documents whose analyze images change under the 32 Mpx budget, and the reason that matters is easy to understate. `analyze_document`'s images do not merely inform fidelity — **they decide which pages belong to which sub-invoice.** A changed image can therefore change the split itself, not just the values read off it. The `detail: low` argument makes that unlikely, since the API downsamples to roughly 512px tiles regardless, but unlikely is not verified, and a wrong split is a wrong Vorgang.

Every other document is covered by the arithmetic argument above, which for those files is stronger than the sweep: byte-identical inputs cannot produce different outputs.

---

## Deployment prerequisite — worker memory

**Raise the worker containers from 4 GiB to 8 GiB before or with this deploy.**

The render bound is what makes this a headroom decision rather than a blank cheque. With the split site and analyze bounded, the crash document's peak sits at **4.16 GB against a 4 GiB (4.29 GB) limit — 97%**. Tracing the peak by phase puts it between `invoice_created` and the first retry: the **OCR phase**, where Mistral renders each page under the 200 Mpx canvas budget and its render loop alone measures 2.38 GB.

Capping OCR resolution was considered and rejected. Unlike analyze's `detail: low` images — which the API downsamples regardless, making a cap free — OCR resolution feeds text recognition directly, and a 48 Mpx cap would change OCR input for 20 of 441 corpus files with no sweep available to vouch for them. Memory is the cheap side of that trade.

Two things to check when applying it:

- **Container Apps couples CPU and memory** — memory in GiB must be twice the CPU count, so 8 GiB requires 4 CPU. That doubles compute per replica, not just memory. Confirm the current allocation before assuming the delta.
- **No CPU or memory setting exists anywhere in this repository** — not in `deploy.sh`, not in `scripts/provision_product.sh`, not in `azure_deployment_plan.md`. The workers run on whatever was set manually or defaulted. Worth pinning it in `provision_product.sh` while making this change, so the next product does not inherit a limit nobody chose.

Without this, the branch still fixes the crash — 5.1 GB down to 4.16 GB, and acceptance passes end to end — but a document somewhat larger than the one that caused this would still be at risk.

---

## Release

Ships in the same release as the returncode and content-policy work, currently on the test tier as `v20260817a` and awaiting 3C sign-off. This adds no contract change, so it does not complicate 3C's integration, and it fixes a live production crash.

After merge: `./deploy.sh bps <new-tag> test` first, since BPS is where the crash occurred and where large-format scans are most likely.
