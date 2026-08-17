"""No unbounded get_pixmap left in production code.

Each of these sites renders one page at a time, so memory never reached the
subdocument site's several gigabytes — but a single large-format page at a
fixed dpi is still hundreds of megabytes against a 4 GiB limit.

CANVAS_BUDGET_PX is generous enough (400 Mpx) that a single HUGE page at any
of these sites' base dpis already fits inside it unscaled — asserting "the
output stays under budget" would pass identically before and after the fix
and prove nothing. So these tests assert the call contract instead: each
site must consult render_dpi_for per page with the right base dpi, and must
render at the dpi it returns rather than the hardcoded base dpi.
"""
import struct
from pathlib import Path

import fitz

HUGE = (4554.0, 6516.0)

# Distinctive stand-in dpi, well below any real base dpi (150/200), so a
# rendered page only matches it if the call site actually used the value
# render_dpi_for returned rather than its own hardcoded dpi.
_FAKE_DPI = 55


def _make_pdf(path, page_sizes):
    doc = fitz.open()
    for i, (w, h) in enumerate(page_sizes):
        doc.new_page(width=w, height=h).insert_text((72, 72), f"Seite {i + 1}")
    doc.save(path)
    doc.close()
    return path


def _png_dimensions(data):
    # Production never reopens its own rendered output through PIL after
    # writing it — only a test's verification step would — so read the PNG
    # header directly instead of via Image.open. That keeps this assertion
    # from depending on PIL's own decompression-bomb guard (which fires well
    # below the 400 Mpx budget), a property of PIL, not of what these call
    # sites are supposed to guarantee.
    if isinstance(data, (str, Path)):
        with open(data, "rb") as f:
            data = f.read(24)
    header = data[:24]
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def test_convert_file_to_images_honours_render_dpi_for(tmp_path, monkeypatch):
    from core import utils

    pdf = _make_pdf(tmp_path / "in.pdf", [HUGE, (595.0, 841.0)])

    calls = []

    def _fake_render_dpi_for(page_sizes, base_dpi):
        calls.append((tuple(page_sizes), base_dpi))
        return _FAKE_DPI

    monkeypatch.setattr(utils, "render_dpi_for", _fake_render_dpi_for, raising=False)

    paths = utils.convert_file_to_images(str(pdf))

    assert len(paths) == 2
    assert calls == [((HUGE,), 150), (((595.0, 841.0),), 150)]

    with fitz.open(pdf) as doc:
        for page, path in zip(doc, paths):
            expected = page.get_pixmap(dpi=_FAKE_DPI)
            assert _png_dimensions(path) == (expected.width, expected.height)


def test_mistral_ocr_honours_render_dpi_for(tmp_path, monkeypatch):
    from core.ocr import ocr_mistral_v2
    from core.ocr.ocr_mistral_v2 import MistralOCRProcessor

    pdf = _make_pdf(tmp_path / "in.pdf", [HUGE])

    engine = object.__new__(MistralOCRProcessor)

    calls = []

    def _fake_render_dpi_for(page_sizes, base_dpi):
        calls.append((tuple(page_sizes), base_dpi))
        return _FAKE_DPI

    monkeypatch.setattr(
        ocr_mistral_v2, "render_dpi_for", _fake_render_dpi_for, raising=False
    )

    seen = []

    def _fake_process_image(image_path):
        seen.append(_png_dimensions(image_path))
        return "markdown"

    monkeypatch.setattr(engine, "_process_image", _fake_process_image)

    result = engine._process_pdf(str(pdf))

    assert result == {1: "markdown"}
    assert calls == [((HUGE,), 200)]

    with fitz.open(pdf) as doc:
        expected = doc[0].get_pixmap(dpi=_FAKE_DPI)
    assert seen == [(expected.width, expected.height)]


def test_orientation_detection_honours_render_dpi_for(tmp_path, monkeypatch):
    import core.pipeline as pipeline
    from core.pipeline import Pipeline

    pdf = _make_pdf(tmp_path / "in.pdf", [HUGE])

    pipe = object.__new__(Pipeline)
    pipe.local_input_path = str(pdf)
    pipe.work_dir = tmp_path

    calls = []

    def _fake_render_dpi_for(page_sizes, base_dpi):
        calls.append((tuple(page_sizes), base_dpi))
        return _FAKE_DPI

    monkeypatch.setattr(
        pipeline, "render_dpi_for", _fake_render_dpi_for, raising=False
    )

    seen = []

    def _fake_detect(img):
        seen.append((img.width, img.height))
        return 0

    monkeypatch.setattr(Pipeline, "_detect_rotation", staticmethod(_fake_detect))

    pipe._fix_pdf_orientation()

    assert calls == [((HUGE,), pipeline.ORIENTATION_RENDER_DPI)]

    with fitz.open(pdf) as doc:
        expected = doc[0].get_pixmap(dpi=_FAKE_DPI)
    assert seen == [(expected.width, expected.height)]
