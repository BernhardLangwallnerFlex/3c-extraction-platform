"""Bounding rendered pixels.

A page's pixel count grows with the square of the dpi, and every pixel costs
three bytes as RGB. A fixed dpi therefore means an unbounded memory footprint,
which is how a five-page BPS document with 1.6 x 2.3 m pages killed a 4 GiB
worker three times in a row on 2026-08-17.
"""
import struct

import pytest
import fitz
from PIL import Image

from core.rendering import CANVAS_BUDGET_PX, MIN_DPI, render_dpi_for, concat_page_files, render_pdf_pages_to_files

A4 = (595.0, 841.0)
# The two page geometries from the document that caused the OOM, in points.
HUGE_A = (4554.0, 6516.0)
HUGE_B = (4177.0, 6095.0)
# Neither HUGE_A nor HUGE_B alone exceeds the 400 Mpx budget (each renders to
# ~229 Mpx at 200 dpi) — a real corpus page doesn't need to for the no-op
# guarantee to matter. This one exists purely to exercise the
# single-oversized-page path: no real corpus page is this large.
OVERSIZED_SINGLE = (7000.0, 10000.0)


def _total_px(page_sizes, dpi):
    return sum(w * h for w, h in page_sizes) * (dpi / 72.0) ** 2


def test_pages_within_budget_return_base_dpi_unchanged():
    # The no-op guarantee: every document that works today must render at
    # exactly the dpi it renders at today.
    assert render_dpi_for([A4] * 5, base_dpi=200) == 200


def test_single_oversized_page_scales_down_within_budget():
    dpi = render_dpi_for([OVERSIZED_SINGLE], base_dpi=200)
    assert dpi < 200
    assert _total_px([OVERSIZED_SINGLE], dpi) <= CANVAS_BUDGET_PX


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
    pdf = _make_pdf(tmp_path / "in.pdf", [OVERSIZED_SINGLE])

    (_path, width, height), = render_pdf_pages_to_files(pdf, tmp_path, base_dpi=200)

    # Not just within budget — actually smaller than the undownscaled render,
    # so this test still proves a downscale happened rather than passing
    # vacuously because the page already fit. Computed arithmetically rather
    # than by rendering the undownscaled page: at 200 dpi OVERSIZED_SINGLE is
    # a 540 Mpx, ~1.6 GB pixmap, and the whole point of this test is not to
    # pay that memory cost to prove a memory-bounding fix.
    undownscaled_w, undownscaled_h = OVERSIZED_SINGLE
    undownscaled_px = (undownscaled_w / 72 * 200) * (undownscaled_h / 72 * 200)
    assert width * height < undownscaled_px
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


def test_concat_does_not_raise_reopening_a_page_that_fills_the_budget(tmp_path):
    # concat_page_files reopens the per-page PNGs it just wrote. HUGE_A alone
    # renders to ~229 Mpx at 200 dpi — no downscale needed, since that is
    # under CANVAS_BUDGET_PX — but still well above PIL's own decompression-
    # bomb threshold (~179 Mpx). Without the guard lifted for the duration of
    # that reopen, this raised DecompressionBombError: an exception crash
    # traded for the OOM crash this task fixes. Exactly a one-page
    # subdocument of the document that caused it.
    pdf = _make_pdf(tmp_path / "in.pdf", [HUGE_A])
    rendered = render_pdf_pages_to_files(pdf, tmp_path, base_dpi=200)

    out = concat_page_files(rendered, tmp_path / "out.png")

    with open(out, "rb") as f:
        width, height = struct.unpack(">II", f.read(24)[16:24])
    # Both bounds matter: within our budget, but still above PIL's own ~179
    # Mpx decompression-bomb threshold — otherwise this could pass vacuously
    # without ever exercising the guard it exists to regression-test.
    assert 178_956_970 < width * height <= CANVAS_BUDGET_PX
