"""The >=1-subdocument guarantee.

When the analyzer reports no Beleg, the pipeline used to emit an empty
`subdocuments` array — nothing for the consumer to branch on. It now emits one
subdocument spanning every page, which extraction then classifies (almost
always 200). This is what makes "read subdocuments[].returncode" a contract
rather than a usually-works.
"""
from pathlib import Path

import fitz
import pytest

from core.pipeline import Pipeline


class _CollectingStorage:
    def __init__(self):
        self.written = []

    def write_text(self, key, text):
        self.written.append(key)

    def write_bytes(self, key, data, content_type=None):
        self.written.append(key)


@pytest.fixture
def three_page_pdf(tmp_path):
    doc = fitz.open()
    for n in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Seite {n + 1}")
    path = tmp_path / "input.pdf"
    doc.save(path)
    doc.close()
    return path


def _make_pipeline(analysis_dict, pdf_path, work_dir):
    pipe = object.__new__(Pipeline)
    pipe.storage = _CollectingStorage()
    pipe.analysis_dict = analysis_dict
    pipe.file_type = "pdf"
    pipe.local_input_path = str(pdf_path)
    pipe.work_dir = Path(work_dir)
    pipe.output_prefix = "az://invoices/processed-bps"
    pipe.stem = "abc"
    pipe.subdocuments = []
    pipe.markdown_by_page = {1: "seite eins", 2: "seite zwei", 3: "seite drei"}
    return pipe


@pytest.mark.parametrize("analysis", [
    {"invoice_pages": {}},
    {"invoice_pages": None},
    {},  # key absent entirely
])
def test_empty_invoice_pages_yields_one_subdocument_over_all_pages(analysis, three_page_pdf, tmp_path):
    pipe = _make_pipeline(analysis, three_page_pdf, tmp_path)

    pipe.split_document_into_invoices()

    assert len(pipe.subdocuments) == 1
    assert pipe.subdocuments[0].page_numbers == [1, 2, 3]
    assert pipe.subdocuments[0].document_number == 1


def test_non_empty_invoice_pages_is_unaffected(three_page_pdf, tmp_path):
    pipe = _make_pipeline({"invoice_pages": {"R-1": [1], "R-2": [2, 3]}}, three_page_pdf, tmp_path)

    pipe.split_document_into_invoices()

    assert [sd.page_numbers for sd in pipe.subdocuments] == [[1], [2, 3]]
    assert pipe._invoice_key_to_doc_number == {"R-1": 1, "R-2": 2}


def test_no_pages_at_all_produces_no_subdocument(three_page_pdf, tmp_path):
    # Nothing to render; the fallback must not crash trying to build an image
    # out of zero pages.
    pipe = _make_pipeline({"invoice_pages": {}}, three_page_pdf, tmp_path)
    pipe.markdown_by_page = {}

    pipe.split_document_into_invoices()

    assert pipe.subdocuments == []
