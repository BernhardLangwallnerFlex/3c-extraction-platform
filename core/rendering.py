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
