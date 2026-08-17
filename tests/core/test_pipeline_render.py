"""The subdocument image render, bounded.

This is the call site that OOM-killed a BPS worker three times on 2026-08-17.
"""
import struct
from pathlib import Path

import fitz
import pytest
from PIL import Image

from core.pipeline import Pipeline
from core.rendering import CANVAS_BUDGET_PX


def _png_dimensions(path):
    # Production never reopens its own concatenated output through PIL after
    # writing it — only a test's verification step would — so read the PNG
    # header directly instead of via Image.open. That keeps this assertion
    # from depending on PIL's own decompression-bomb guard, which is a
    # property of PIL, not of what this pipeline is supposed to guarantee.
    with open(path, "rb") as f:
        header = f.read(24)
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
    # what has to fit, not their sum.
    #
    # Note: at ANALYZE_RENDER_DPI (150), the large page below renders to
    # ~128.8 Mpx — comfortably under CANVAS_BUDGET_PX (400 Mpx, raised from
    # 200 Mpx after this test was designed), so this no longer exercises an
    # actual downscale. It still pins the no-op guarantee (real document
    # geometry renders unchanged) and the per-page wiring (one image per
    # page, each individually checked against the budget rather than their
    # sum). A single page large enough to force a downscale under the 400 Mpx
    # budget renders to well above PIL's own decompression-bomb hard limit
    # (~179 Mpx) even after being scaled down to fit, so it cannot be opened
    # here via plain Image.open without lifting that guard — same reason
    # concat_page_files in core/rendering.py has to lift it.
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
