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

import fitz
import structlog
from PIL import Image

_log = structlog.get_logger()

# Total rendered pixels one concatenated image — or one standalone page image
# — may occupy. 400 Mpx as RGB is 1.2 GB; worst case, that canvas plus one
# page alive at once, is roughly 2.4 GB against the worker's 4 GiB limit.
#
# Calibrated, not chosen for roundness: measured across 441 corpus PDFs, the
# largest bounding-box canvas — max(width) x sum(heights), what
# concat_page_files actually allocates — is 331.4 Mpx at 200 dpi
# (BPS_3.pdf). 400 Mpx is a 21% margin over that observed maximum. Every
# document that works today therefore renders at exactly the dpi it renders
# at today, byte for byte.
CANVAS_BUDGET_PX = 400_000_000

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

    The budget is checked against the canvas these pages are actually
    rendered onto: as wide as the widest page, as tall as every page stacked
    (`concat_page_files` pastes each page at its own width, so narrower pages
    still cost the full canvas width in white space). For same-width pages —
    the overwhelming majority of real documents — that canvas area equals the
    sum of each page's own area, so this changes nothing for them. It matters
    only when one subdocument mixes page widths, where the sum would
    understate what actually gets allocated.

    Rendered area grows with the square of the dpi, so the scale factor is the
    square root of the ratio of budget to actual.
    """
    valid = [(w, h) for w, h in page_sizes if w > 0 and h > 0]
    if not valid:
        return base_dpi

    canvas_pt2 = max(w for w, _h in valid) * sum(h for _w, h in valid)
    total_px_at_base = canvas_pt2 * (base_dpi / 72.0) ** 2
    if total_px_at_base <= budget_px:
        return base_dpi

    # int() truncates, which keeps the result inside the budget rather than
    # rounding back over it.
    return max(MIN_DPI, int(base_dpi * sqrt(budget_px / total_px_at_base)))


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
    # PIL's own decompression-bomb guard defaults to ~89.5 Mpx and raises
    # above ~179 Mpx — below CANVAS_BUDGET_PX. These per-page files are ones
    # this module rendered moments earlier, not untrusted input, so the guard
    # is lifted only for the duration of this loop and restored in the
    # finally. Left in force elsewhere: `_fix_image_orientation` opens raw
    # customer uploads directly with Image.open and must keep it.
    prev_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        for page_path, _width, height in page_files:
            with Image.open(page_path) as page_img:
                # No mask: an RGBA page is converted to RGB by paste, exactly
                # as the previous implementation did.
                canvas.paste(page_img, (0, y))
            y += height
    finally:
        Image.MAX_IMAGE_PIXELS = prev_limit

    out_path = Path(out_path)
    canvas.save(out_path)
    canvas.close()
    return out_path
