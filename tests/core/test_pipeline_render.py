"""The subdocument image render, bounded.

This is the call site that OOM-killed a BPS worker three times on 2026-08-17.
"""
import struct
from pathlib import Path

import fitz
import pytest
from PIL import Image

from core.pipeline import Pipeline
from core.rendering import ANALYZE_BUDGET_PX, CANVAS_BUDGET_PX, MIN_DPI, render_dpi_for


def _png_dimensions(data):
    # Production never reopens its own rendered output through PIL after
    # writing it — only a test's verification step would — so read the PNG
    # header directly instead of via Image.open. That keeps this assertion
    # from depending on PIL's own decompression-bomb guard, which is a
    # property of PIL, not of what this pipeline is supposed to guarantee.
    # Accepts either a path (disk-written subdocument images) or raw PNG
    # bytes (analyze_document's images, which only ever exist as base64
    # inside a content block, never written to disk).
    if isinstance(data, (str, Path)):
        with open(data, "rb") as f:
            data = f.read(24)
    header = data[:24]
    width, height = struct.unpack(">II", header[16:24])
    return width, height


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
    width, height = _png_dimensions(out)
    assert width * height <= CANVAS_BUDGET_PX


def test_single_page_filling_the_budget_concatenates_without_raising(tmp_path):
    # Finding 1: concat_page_files reopens the per-page PNGs it just wrote,
    # and a single large-format page can land within CANVAS_BUDGET_PX while
    # still exceeding PIL's own decompression-bomb threshold (~179 Mpx) — this
    # HUGE_A-sized page renders to ~229 Mpx at 200 dpi, no downscale needed at
    # all. Without the guard lifted inside concat_page_files this raised
    # DecompressionBombError: an exception crash traded for the OOM crash
    # this task fixes. Exactly a one-page subdocument of the failing document.
    pdf = _make_pdf(tmp_path / "in.pdf", [(4554.0, 6516.0)])
    pipe = _make_pipeline(pdf, tmp_path, {"R-1": [1]}, 1)

    pipe.split_document_into_invoices()

    img_key = pipe.subdocuments[0].image_key
    out = tmp_path / "written.png"
    out.write_bytes(pipe.storage.blobs[img_key])
    width, height = _png_dimensions(out)
    # Both bounds matter: within our budget, but still above PIL's own ~179
    # Mpx decompression-bomb threshold — otherwise this could pass vacuously
    # without ever exercising the guard it exists to regression-test.
    assert 178_956_970 < width * height <= CANVAS_BUDGET_PX


def test_per_page_temp_files_do_not_survive(tmp_path):
    # They are scratch, and on the failing document they are large scratch.
    pdf = _make_pdf(tmp_path / "in.pdf", [(595.0, 841.0)] * 3)
    pipe = _make_pipeline(pdf, tmp_path, {"R-1": [1, 2], "R-2": [3]}, 3)

    pipe.split_document_into_invoices()

    assert list(tmp_path.glob("subdoc*_page_*.png")) == []


def test_analyze_renders_each_page_within_the_budget(tmp_path, monkeypatch):
    # Per-image budget: analyze sends pages as separate images, so each one is
    # what has to fit, not their sum. Unlike the subdocument render, analyze
    # pages are checked against ANALYZE_BUDGET_PX (8 Mpx), not CANVAS_BUDGET_PX
    # (200 Mpx) — analyze's images go to the API with "detail": "low" and get
    # downsampled to ~512px tiles regardless, so there is nothing to buy by
    # rendering them any larger.
    #
    # At ANALYZE_RENDER_DPI (150), the large page below renders to ~128.8 Mpx
    # before downscaling, well above the 8 Mpx budget, so this exercises a
    # real downscale. Note this exact page (the real BPS crash geometry, 1.6 x
    # 2.3 m) can't be brought under 8 Mpx without also dropping below
    # MIN_DPI's legibility floor (40) — fitting it into ANALYZE_BUDGET_PX
    # alone would need ~37 dpi. render_dpi_for's documented contract is that
    # MIN_DPI wins in that case "even where that means exceeding the budget",
    # so the true ceiling here is the MIN_DPI-floored render (~9.16 Mpx,
    # still a ~14x reduction from the unbounded ~128.8 Mpx), not
    # ANALYZE_BUDGET_PX itself. Dimensions are read via _png_dimensions
    # rather than Image.open for consistency with the other tests in this
    # file, and because production never reopens its own rendered output
    # through PIL either.
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

    images = [b for b in captured["blocks"] if b["type"] == "image_url"]
    assert len(images) == 2
    with fitz.open(pdf) as doc:
        for block, page in zip(images, doc):
            raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
            width, height = _png_dimensions(raw)
            expected_dpi = render_dpi_for(
                [(page.rect.width, page.rect.height)],
                pipeline.ANALYZE_RENDER_DPI,
                budget_px=ANALYZE_BUDGET_PX,
            )
            expected_pix = page.get_pixmap(dpi=expected_dpi)
            assert (width, height) == (expected_pix.width, expected_pix.height)
            # Massively reduced from the unbounded ~128.8 Mpx render either way.
            assert width * height <= max(ANALYZE_BUDGET_PX, 10_000_000)


def test_analyze_consults_render_dpi_for_once_per_page_and_uses_its_answer(
    tmp_path, monkeypatch
):
    # The downscale path, end to end, without ever rendering a genuinely
    # oversized page (that would mean a real ~1.2 GB allocation inside a
    # test whose subject is bounding memory). A spy on render_dpi_for proves
    # the call contract instead: called once per page, each time with a
    # single-page list (that's what makes the budget per-image rather than
    # per-document), the page's own (width, height) in points, and
    # ANALYZE_RENDER_DPI as the base dpi. Returning a distinctive dpi from
    # the spy and reading the resulting PNGs back with _png_dimensions (not
    # Image.open — nothing here is large enough to need the decompression
    # bomb guard lifted, but there's no reason to depend on PIL's guard
    # either) proves the returned dpi is actually the one used to render,
    # not just computed and discarded.
    import base64

    import core.pipeline as pipeline

    pdf = _make_pdf(tmp_path / "in.pdf", [(4554.0, 6516.0), (595.0, 841.0)])
    pipe = _make_pipeline(pdf, tmp_path, {}, 2)
    pipe.markdown_with_pages_numbers = "text"
    pipe.product_config = type("C", (), {"analyze_prompt_builder": None})()

    with fitz.open(pdf) as doc:
        page_sizes = [(page.rect.width, page.rect.height) for page in doc]

    spy_calls = []
    forced_dpi = 40

    def _spy_render_dpi_for(page_sizes_arg, base_dpi, budget_px=CANVAS_BUDGET_PX):
        spy_calls.append((page_sizes_arg, base_dpi, budget_px))
        return forced_dpi

    monkeypatch.setattr(pipeline, "render_dpi_for", _spy_render_dpi_for)

    captured = {}

    def _fake_call(call_fn, client, model, blocks):
        captured["blocks"] = blocks
        raise RuntimeError("stop after building the blocks")

    monkeypatch.setattr(pipeline, "call_with_vision_fallback", _fake_call)
    monkeypatch.setattr(pipeline, "AzureOpenAI", lambda **kw: object())

    with pytest.raises(RuntimeError):
        pipe.analyze_document()

    # Called once per page, each time with that page's own size alone, the
    # analyze base dpi, and analyze's own (much smaller) budget — never the
    # whole document's sizes together and never the subdocument canvas budget.
    assert spy_calls == [
        ([size], pipeline.ANALYZE_RENDER_DPI, ANALYZE_BUDGET_PX) for size in page_sizes
    ]

    images = [b for b in captured["blocks"] if b["type"] == "image_url"]
    assert len(images) == 2
    with fitz.open(pdf) as doc:
        for block, page in zip(images, doc):
            raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
            width, height = _png_dimensions(raw)
            expected_pix = page.get_pixmap(dpi=forced_dpi)
            assert (width, height) == (expected_pix.width, expected_pix.height)


def test_analyze_ordinary_pages_are_a_no_op_under_the_analyze_budget(tmp_path, monkeypatch):
    # The property that keeps ANALYZE_BUDGET_PX safe to ship: A4 (595 x 841 pt)
    # is 2.2 Mpx and A3 (842 x 1191 pt) is 4.4 Mpx at ANALYZE_RENDER_DPI (150),
    # both comfortably under the 8 Mpx analyze budget, so ordinary pages must
    # render at exactly 150 dpi and come back byte-identical to today's output
    # — not merely "small enough by some other measure".
    import base64

    import core.pipeline as pipeline

    a4 = (595.0, 841.0)
    a3 = (842.0, 1191.0)
    pdf = _make_pdf(tmp_path / "in.pdf", [a4, a3])
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

    images = [b for b in captured["blocks"] if b["type"] == "image_url"]
    assert len(images) == 2
    with fitz.open(pdf) as doc:
        for block, page in zip(images, doc):
            raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
            width, height = _png_dimensions(raw)
            expected_pix = page.get_pixmap(dpi=pipeline.ANALYZE_RENDER_DPI)
            assert (width, height) == (expected_pix.width, expected_pix.height)
            assert width * height <= ANALYZE_BUDGET_PX
            # Byte-identical, not just same dimensions.
            assert raw == expected_pix.tobytes("png")


def test_analyze_pathological_page_is_capped_to_the_analyze_budget(tmp_path, monkeypatch):
    # The pathological geometry from the brief: a 4554 x 6516 pt page (the
    # real BPS crash page, 1.6 x 2.3 m) is 128.8 Mpx at ANALYZE_RENDER_DPI
    # (150) — comfortably under CANVAS_BUDGET_PX (200 Mpx) but nearly 16x the
    # 8 Mpx analyze budget — so this is the case ANALYZE_BUDGET_PX exists to
    # catch, distinct from the subdocument render's much larger budget.
    #
    # Verified finding: this exact page can't be brought under 8 Mpx without
    # also dropping below MIN_DPI's legibility floor (40) — fitting it into
    # ANALYZE_BUDGET_PX alone would compute ~37 dpi. render_dpi_for's
    # documented contract has MIN_DPI win that tie "even where that means
    # exceeding the budget" (core/rendering.py), the same trade-off
    # CANVAS_BUDGET_PX already accepts elsewhere. So the real ceiling for
    # this page is the MIN_DPI-floored render (~9.16 Mpx), not
    # ANALYZE_BUDGET_PX itself — still a ~14x reduction from the unbounded
    # ~128.8 Mpx this test would have produced before this change.
    import base64

    import core.pipeline as pipeline

    page_size = (4554.0, 6516.0)
    pdf = _make_pdf(tmp_path / "in.pdf", [page_size])
    pipe = _make_pipeline(pdf, tmp_path, {}, 1)
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

    images = [b for b in captured["blocks"] if b["type"] == "image_url"]
    assert len(images) == 1
    raw = base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1])
    width, height = _png_dimensions(raw)

    expected_dpi = render_dpi_for(
        [page_size], pipeline.ANALYZE_RENDER_DPI, budget_px=ANALYZE_BUDGET_PX
    )
    assert expected_dpi == MIN_DPI  # confirms this page does hit the floor
    with fitz.open(pdf) as doc:
        expected_pix = doc[0].get_pixmap(dpi=expected_dpi)
    assert (width, height) == (expected_pix.width, expected_pix.height)
    # Capped far below the unbounded ~128.8 Mpx render either way.
    assert width * height <= max(ANALYZE_BUDGET_PX, 10_000_000)
