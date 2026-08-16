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
