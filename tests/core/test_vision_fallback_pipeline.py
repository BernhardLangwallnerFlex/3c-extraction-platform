"""The vision fallback, wired into the two call sites.

Both tests drive the real functions with a stubbed Azure client, so a refactor
that bypassed `call_with_vision_fallback` on either path would fail here.
"""
from types import SimpleNamespace

import fitz

from core.pipeline import Pipeline
from core.processors.azure_processor import AzureInvoiceProcessor


class FakeAPIError(Exception):
    def __init__(self, status_code, body=None):
        super().__init__(f"Error code: {status_code}")
        self.status_code = status_code
        self.body = body


CONTENT_POLICY_BODY = {"error": {
    "message": "Your input image may contain content that is not allowed by our content safety system.",
    "code": "content_policy_violation",
}}


class _RejectImagesClient:
    """Rejects any request carrying an image block; accepts text-only ones."""

    def __init__(self, payload='{"type": "invoice"}'):
        self.blocks_seen = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                blocks = kwargs["messages"][0]["content"]
                outer.blocks_seen.append(blocks)
                if any(b.get("type") == "image_url" for b in blocks):
                    raise FakeAPIError(400, body=CONTENT_POLICY_BODY)

                class _Msg:
                    content = payload

                class _Choice:
                    message = _Msg()

                class _Usage:
                    prompt_tokens = 10
                    completion_tokens = 5

                class _Resp:
                    choices = [_Choice()]
                    usage = _Usage()

                return _Resp()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _processor_with(client):
    proc = object.__new__(AzureInvoiceProcessor)
    proc.client = client
    proc.model = "gpt-5.4"
    proc.vision_model = "gpt-5.4"
    proc.name = "test"
    return proc


def test_extract_falls_back_to_text_and_marks_the_result(monkeypatch, tmp_path):
    img = tmp_path / "page.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    monkeypatch.setattr("core.processors.azure_processor.convert_file_to_images",
                        lambda p: [str(img)])

    client = _RejectImagesClient()
    result = _processor_with(client).extract(str(img), markdown_text="OCR text", prompt="extract this")

    assert result["_vision_dropped"] is True
    assert len(client.blocks_seen) == 2
    assert any(b.get("type") == "image_url" for b in client.blocks_seen[0])
    assert not any(b.get("type") == "image_url" for b in client.blocks_seen[1])


def test_extract_leaves_a_clean_result_unmarked(monkeypatch, tmp_path):
    img = tmp_path / "page.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    monkeypatch.setattr("core.processors.azure_processor.convert_file_to_images",
                        lambda p: [str(img)])

    class _AcceptAll(_RejectImagesClient):
        def __init__(self):
            super().__init__()
            outer = self

            class _Completions:
                def create(self, **kwargs):
                    outer.blocks_seen.append(kwargs["messages"][0]["content"])

                    class _Msg:
                        content = '{"type": "invoice"}'

                    class _Choice:
                        message = _Msg()

                    class _Usage:
                        prompt_tokens = 10
                        completion_tokens = 5

                    class _Resp:
                        choices = [_Choice()]
                        usage = _Usage()

                    return _Resp()

            class _Chat:
                completions = _Completions()

            self.chat = _Chat()

    client = _AcceptAll()
    result = _processor_with(client).extract(str(img), markdown_text="OCR text", prompt="extract this")

    assert "_vision_dropped" not in result
    assert len(client.blocks_seen) == 1


def test_pipeline_defaults_analyze_vision_dropped_to_false():
    # Several existing tests build Pipeline via object.__new__ and never call
    # analyze_document(); the attribute must still read False.
    assert Pipeline.analyze_vision_dropped is False
    assert object.__new__(Pipeline).analyze_vision_dropped is False


def test_analyze_document_falls_back_to_text_and_marks_dropped(monkeypatch, tmp_path):
    # Drives the real analyze_document() against the same image-rejecting
    # stub client used above, so a refactor that bypassed
    # call_with_vision_fallback on this path would fail here too.
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Seite 1")
    pdf_path = tmp_path / "input.pdf"
    doc.save(pdf_path)
    doc.close()

    client = _RejectImagesClient(payload='{"invoice_pages": {}}')
    monkeypatch.setattr("core.pipeline.AzureOpenAI", lambda **kwargs: client)

    pipe = object.__new__(Pipeline)
    pipe.product_config = SimpleNamespace(analyze_prompt_builder=None)
    pipe.markdown_with_pages_numbers = "--- PAGE 1 ---\n: seite eins"
    pipe.file_type = "pdf"
    pipe.local_input_path = str(pdf_path)
    pipe.markdown_by_page = {1: "seite eins"}

    pipe.analyze_document()  # must complete, not raise

    assert pipe.analyze_vision_dropped is True
    assert len(client.blocks_seen) == 2
    assert any(b.get("type") == "image_url" for b in client.blocks_seen[0])
    assert not any(b.get("type") == "image_url" for b in client.blocks_seen[1])
    assert pipe.analysis_dict == {"invoice_pages": {}}


# --- qualityFlags and warnings -------------------------------------------

from core.pipeline import Pipeline, SubdocumentArtifact  # noqa: E402
from core.product import ProductConfig  # noqa: E402

ANALYZE_WARNING_FRAGMENT = "Aufteilung des Dokuments erfolgte ohne Bildanalyse"
EXTRACT_WARNING_FRAGMENT = "nur anhand des OCR-Textes"


class _FakeStorage:
    def materialize_to_local(self, key):
        return "/tmp/does-not-need-to-exist.png"


class _FixedProcessor:
    def __init__(self, result):
        self._result = result

    def extract(self, *args, **kwargs):
        return dict(self._result)


class _FakeOCR:
    def __init__(self, single_engine_fallback=False):
        self.single_engine_fallback = single_engine_fallback


_SUBDOC = SubdocumentArtifact(
    document_number=1, page_numbers=[1], markdown="text",
    md_key="k.md", pdf_key="k.pdf", image_key="k.png",
)


def _pipeline(*, analyze_dropped=False, ocr_degraded=False):
    pipe = object.__new__(Pipeline)
    pipe.storage = _FakeStorage()
    pipe.analysis_dict = {}
    pipe.product_config = ProductConfig(
        name="t", extract_prompt_builder=lambda **kw: "p", extract_output_schema={},
    )
    pipe.analyze_vision_dropped = analyze_dropped
    pipe.ocr_engine = _FakeOCR(ocr_degraded)
    return pipe


def test_clean_job_has_an_empty_quality_flags_list():
    pipe = _pipeline()
    result = pipe._extract_single_subdocument(_SUBDOC, _FixedProcessor({"number": "R-1"}))

    assert result["qualityFlags"] == []
    assert list(result)[:3] == ["returncode", "returncodeReasons", "qualityFlags"]


def test_analyze_degradation_flags_and_warns():
    pipe = _pipeline(analyze_dropped=True)
    result = pipe._extract_single_subdocument(_SUBDOC, _FixedProcessor({"number": "R-1"}))

    assert result["qualityFlags"] == ["VISION_DROPPED"]
    assert any(ANALYZE_WARNING_FRAGMENT in w for w in result["warnings"])


def test_extraction_degradation_flags_and_warns():
    pipe = _pipeline()
    processor = _FixedProcessor({"number": "R-1", "_vision_dropped": True})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert result["qualityFlags"] == ["VISION_DROPPED"]
    assert any(EXTRACT_WARNING_FRAGMENT in w for w in result["warnings"])


def test_the_internal_marker_never_reaches_the_consumer():
    pipe = _pipeline()
    processor = _FixedProcessor({"number": "R-1", "_vision_dropped": True})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert "_vision_dropped" not in result


def test_vision_dropped_is_not_duplicated_when_both_stages_degrade():
    pipe = _pipeline(analyze_dropped=True)
    processor = _FixedProcessor({"number": "R-1", "_vision_dropped": True})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert result["qualityFlags"] == ["VISION_DROPPED"]


def test_computed_quality_flags_win_over_a_stray_key_in_result():
    pipe = _pipeline()
    processor = _FixedProcessor({
        "number": "R-1",
        "_vision_dropped": True,
        "qualityFlags": ["BOGUS"],
    })
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert result["qualityFlags"] == ["VISION_DROPPED"]


def test_single_engine_ocr_is_flagged():
    pipe = _pipeline(ocr_degraded=True)
    result = pipe._extract_single_subdocument(_SUBDOC, _FixedProcessor({"number": "R-1"}))

    assert result["qualityFlags"] == ["SINGLE_ENGINE_OCR"]


def test_both_flags_can_appear_together():
    pipe = _pipeline(analyze_dropped=True, ocr_degraded=True)
    result = pipe._extract_single_subdocument(_SUBDOC, _FixedProcessor({"number": "R-1"}))

    assert result["qualityFlags"] == ["VISION_DROPPED", "SINGLE_ENGINE_OCR"]


def test_existing_warnings_are_preserved():
    pipe = _pipeline(analyze_dropped=True)
    processor = _FixedProcessor({"number": "R-1", "warnings": ["Steuersumme weicht ab"]})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert "Steuersumme weicht ab" in result["warnings"]
    assert len(result["warnings"]) == 2


def test_a_non_list_warnings_value_is_replaced():
    pipe = _pipeline(analyze_dropped=True)
    processor = _FixedProcessor({"number": "R-1", "warnings": "nope"})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert isinstance(result["warnings"], list)
    assert any(ANALYZE_WARNING_FRAGMENT in w for w in result["warnings"])


def test_dual_ocr_sets_the_flag_when_one_engine_fails(monkeypatch):
    from core.ocr.ocr_dual import DualOCRProcessor

    dual = object.__new__(DualOCRProcessor)

    class _Good:
        def extract_text(self, invoice):
            return "md", {1: "page one"}

    class _Bad:
        def extract_text(self, invoice):
            raise RuntimeError("engine down")

    dual.mistral, dual.azure, dual.name = _Good(), _Bad(), "dual"
    monkeypatch.setattr("core.ocr.ocr_dual.sentry_sdk.capture_message", lambda *a, **k: None)

    dual.extract_text(invoice=None)

    assert dual.single_engine_fallback is True


def test_dual_ocr_leaves_the_flag_false_when_both_engines_work(monkeypatch):
    from core.ocr.ocr_dual import DualOCRProcessor

    dual = object.__new__(DualOCRProcessor)

    class _Good:
        def extract_text(self, invoice):
            return "md", {1: "page one"}

    dual.mistral, dual.azure, dual.name = _Good(), _Good(), "dual"

    dual.extract_text(invoice=None)

    assert dual.single_engine_fallback is False
