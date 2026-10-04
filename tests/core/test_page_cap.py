"""The per-page render cap: short side in pixels, never above native resolution.

A PDF page's physical size is whatever the producing app wrote. Phone scanning
apps write an A4 photo as a 72 or 144 dpi image, so the page claims to be
~1.6 x 2.3 m and a dpi-based render inflates it to ~200 Mpx. These tests pin
the cap that replaces trust in physical size.
"""
import io
from pathlib import Path

import fitz
import pytest
from PIL import Image

from core.pipeline import Pipeline
from core.rendering import (
    CANVAS_BUDGET_PX,
    PAGE_SHORT_SIDE_PX,
    cap_page_dpi,
    native_image_dpi,
    page_short_side_px,
    render_dpi_for,
    render_pdf_pages_to_files,
)

A4 = (595.0, 841.0)


@pytest.fixture(autouse=True)
def _default_cap(monkeypatch):
    monkeypatch.delenv("RENDER_PAGE_SHORT_SIDE_PX", raising=False)


def _png_bytes(width, height, mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def _pdf_with_image(path, page_size, img_px):
    """A page of `page_size` points filled by an image of `img_px` pixels —
    exactly what a phone scanning app produces."""
    doc = fitz.open()
    page = doc.new_page(width=page_size[0], height=page_size[1])
    page.insert_image(page.rect, stream=_png_bytes(*img_px))
    doc.save(path)
    doc.close()
    return path


def _blank_pdf(path, page_sizes):
    doc = fitz.open()
    for w, h in page_sizes:
        doc.new_page(width=w, height=h).insert_text((72, 72), "Rechnung")
    doc.save(path)
    doc.close()
    return path


def _page_dpi(page, base_dpi=200):
    return cap_page_dpi(page, render_dpi_for([(page.rect.width, page.rect.height)], base_dpi))


def test_a4_page_is_untouched(tmp_path):
    with fitz.open(_blank_pdf(tmp_path / "a4.pdf", [A4])) as doc:
        assert _page_dpi(doc[0]) == 200


def test_a4_scan_at_300_dpi_still_renders_at_the_base_dpi(tmp_path):
    # Native resolution only ever lowers the dpi, never raises it.
    pdf = _pdf_with_image(tmp_path / "scan.pdf", A4, (2480, 3508))
    with fitz.open(pdf) as doc:
        assert round(native_image_dpi(doc[0])) == 300
        assert _page_dpi(doc[0]) == 200


def test_phone_scan_on_a_fake_large_page_renders_at_its_native_resolution(tmp_path):
    # The d40e geometry: a 4452 x 6500 px photo written at 72 dpi, so the page
    # claims 1571 x 2293 mm. Uncapped this rendered at ~189 dpi to ~199 Mpx.
    pdf = _pdf_with_image(tmp_path / "phone.pdf", (4452.0, 6500.0), (4452, 6500))
    with fitz.open(pdf) as doc:
        page = doc[0]
        dpi = _page_dpi(page)
        pix = page.get_pixmap(dpi=dpi)
    assert min(pix.width, pix.height) <= PAGE_SHORT_SIDE_PX
    assert pix.width <= 4452 and pix.height <= 6500


def test_low_resolution_scan_is_not_upsampled(tmp_path):
    # A 1240 x 1754 px image on a large page: the cap alone would allow 2500 px,
    # but there is nothing to gain above the image's own resolution.
    pdf = _pdf_with_image(tmp_path / "low.pdf", (2480.0, 3508.0), (1240, 1754))
    with fitz.open(pdf) as doc:
        page = doc[0]
        pix = page.get_pixmap(dpi=_page_dpi(page))
    assert pix.width <= 1240


def test_tall_strip_keeps_its_width(tmp_path):
    # A long receipt or a stacked multi-page scan: the short side is what
    # carries legibility, so a long page must not be squeezed narrow.
    with fitz.open(_blank_pdf(tmp_path / "strip.pdf", [(595.0, 4000.0)])) as doc:
        page = doc[0]
        pix = page.get_pixmap(dpi=_page_dpi(page))
        uncapped_width = page.get_pixmap(dpi=200).width
    assert pix.width == uncapped_width


def test_cap_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_PAGE_SHORT_SIDE_PX", "0")
    pdf = _pdf_with_image(tmp_path / "phone.pdf", (4452.0, 6500.0), (4452, 6500))
    with fitz.open(pdf) as doc:
        page = doc[0]
        assert cap_page_dpi(page, 150) == 150


@pytest.mark.parametrize("raw, expected", [("", PAGE_SHORT_SIDE_PX), ("1800", 1800), ("nonsense", PAGE_SHORT_SIDE_PX), ("-5", 0)])
def test_env_override_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("RENDER_PAGE_SHORT_SIDE_PX", raw)
    assert page_short_side_px() == expected


def test_capped_canvas_still_respects_the_budget(tmp_path):
    # The cap bounds each page; the budget still bounds their sum. 60 A4 pages
    # at 200 dpi is ~232 Mpx — above the extraction model's ~179 Mpx limit.
    pdf = _blank_pdf(tmp_path / "long.pdf", [A4] * 60)
    rendered = render_pdf_pages_to_files(pdf, tmp_path, base_dpi=200)
    width = max(w for _p, w, _h in rendered)
    height = sum(h for _p, _w, h in rendered)
    assert width * height <= CANVAS_BUDGET_PX


def test_image_upload_becomes_a_one_page_pdf_with_its_stem(tmp_path):
    # split_document_into_invoices only accepts PDFs; an image upload is
    # wrapped once at intake and then takes the scanned-PDF path, cap included.
    img_path = tmp_path / "foto_rechnung.jpg"
    Image.new("RGB", (3024, 4032), "white").save(img_path, "JPEG", dpi=(72, 72))

    pipe = object.__new__(Pipeline)
    pipe.work_dir = tmp_path
    out = pipe._image_to_pdf(img_path)

    assert out.suffix == ".pdf" and out.stem == "foto_rechnung"
    with fitz.open(out) as doc:
        assert len(doc) == 1
        page = doc[0]
        info = page.get_image_info()[0]
        assert (info["width"], info["height"]) == (3024, 4032)
        pix = page.get_pixmap(dpi=_page_dpi(page))
    assert min(pix.width, pix.height) <= PAGE_SHORT_SIDE_PX
    assert pix.width <= 3024
