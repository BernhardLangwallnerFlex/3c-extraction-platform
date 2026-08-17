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
