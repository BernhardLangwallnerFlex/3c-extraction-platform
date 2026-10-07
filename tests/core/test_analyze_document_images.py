"""analyze_document drives core.analyze_content: unchanged when the switch is
off, skips low-text images when on, and never sends more than 50 images."""
import fitz
from structlog.testing import capture_logs

from core.pipeline import Pipeline
from core.product import ProductConfig

A = "--- OCR Source A (Mistral) ---\n"
TEXT = A + "Rechnung " + "x" * 200
PHOTO = A + "![img-0.jpeg](img-0.jpeg)"


class _CaptureClient:
    def __init__(self):
        self.blocks_seen = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.blocks_seen.append(kwargs["messages"][0]["content"])

                class _Msg:
                    content = '{"invoice_pages": {}}'

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


def _pdf(tmp_path, n_pages):
    doc = fitz.open()
    for i in range(n_pages):
        doc.new_page().insert_text((72, 72), f"Seite {i + 1}")
    path = tmp_path / "input.pdf"
    doc.save(path)
    doc.close()
    return str(path)


def _pipe(tmp_path, monkeypatch, markdown_by_page, threshold):
    client = _CaptureClient()
    monkeypatch.setattr("core.pipeline.AzureOpenAI", lambda **kwargs: client)
    pipe = object.__new__(Pipeline)
    pipe.product_config = ProductConfig(
        name="t",
        extract_prompt_builder=lambda **kw: "x",
        extract_output_schema={},
        analyze_prompt_builder=lambda *, markdown_text: f"PROMPT<{markdown_text}>",
        analyze_low_text_threshold=threshold,
    )
    pipe.file_type = "pdf"
    pipe.local_input_path = _pdf(tmp_path, len(markdown_by_page))
    pipe.markdown_by_page = markdown_by_page
    pipe.markdown_with_pages_numbers = "\n\n---\n\n".join(
        f"--- PAGE {p} ---\n: {t}" for p, t in markdown_by_page.items()
    )
    return pipe, client


def _images(blocks):
    return [b for b in blocks if b["type"] == "image_url"]


def _texts(blocks):
    return [b["text"] for b in blocks if b["type"] == "text"]


def test_threshold_defaults_to_none():
    cfg = ProductConfig(name="t", extract_prompt_builder=lambda **kw: "x", extract_output_schema={})
    assert cfg.analyze_low_text_threshold is None


def test_switch_off_request_is_unchanged(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {1: TEXT, 2: PHOTO}, threshold=None)
    pipe.analyze_document()
    (blocks,) = client.blocks_seen
    assert _texts(blocks) == [f"PROMPT<{pipe.markdown_with_pages_numbers}>"]
    assert len(_images(blocks)) == 2
    assert blocks[0]["type"] == "text" and all(b["type"] == "image_url" for b in blocks[1:])


def test_switch_on_skips_photo_image_and_marks_the_page(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {1: TEXT, 2: PHOTO}, threshold=150)
    pipe.analyze_document()
    (blocks,) = client.blocks_seen
    assert len(_images(blocks)) == 1
    assert _texts(blocks)[1:] == ["Seite 1:"]
    prompt = _texts(blocks)[0]
    assert "--- PAGE 2 ---" in prompt
    assert "[Kaum Text erkannt (0 Zeichen)" in prompt
    assert prompt.count("[Kaum Text erkannt") == 1


def test_all_pages_low_text_sends_prompt_only(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {1: PHOTO, 2: PHOTO, 3: PHOTO}, threshold=150)
    pipe.analyze_document()
    (blocks,) = client.blocks_seen
    assert len(blocks) == 1 and blocks[0]["type"] == "text"


def test_fifty_text_pages_send_fifty_images(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {p: TEXT for p in range(1, 51)}, threshold=None)
    pipe.analyze_document()
    assert len(_images(client.blocks_seen[0])) == 50


def test_fifty_one_pages_go_text_only_and_log_the_cap(tmp_path, monkeypatch):
    # Applies with the switch OFF too: this request would be rejected today.
    pipe, client = _pipe(tmp_path, monkeypatch, {p: TEXT for p in range(1, 52)}, threshold=None)
    with capture_logs() as logs:
        pipe.analyze_document()
    (blocks,) = client.blocks_seen
    assert _images(blocks) == []
    capped = [e for e in logs if e["event"] == "analyze_images_capped"]
    assert capped and capped[0]["pages"] == 51 and capped[0]["images_planned"] == 51


def test_telemetry_reports_images_and_low_text_pages(tmp_path, monkeypatch):
    pipe, client = _pipe(tmp_path, monkeypatch, {1: TEXT, 2: PHOTO, 3: PHOTO}, threshold=150)
    with capture_logs() as logs:
        pipe.analyze_document()
    (call,) = [e for e in logs if e["event"] == "analyze_llm_call"]
    assert call["images_sent"] == 1 and call["pages_low_text"] == 2


def test_switch_on_without_low_text_pages_is_unchanged(tmp_path, monkeypatch):
    # Labels only earn their place once an image is skipped; on a document with
    # no photo pages the experiment showed they only perturbed the grouping.
    pipe, client = _pipe(tmp_path, monkeypatch, {1: TEXT, 2: TEXT}, threshold=150)
    pipe.analyze_document()
    (blocks,) = client.blocks_seen
    assert _texts(blocks) == [f"PROMPT<{pipe.markdown_with_pages_numbers}>"]
    assert len(_images(blocks)) == 2


class _RejectImagesClient(_CaptureClient):
    """Content-filter 400 on any request with an image; accepts text-only."""

    def __init__(self):
        super().__init__()
        outer = self
        inner_create = self.chat.completions.create

        class _Err(Exception):
            status_code = 400
            body = {"error": {"code": "content_policy_violation", "message": "content safety"}}

        class _Completions:
            def create(self, **kwargs):
                blocks = kwargs["messages"][0]["content"]
                if any(b["type"] == "image_url" for b in blocks):
                    outer.blocks_seen.append(blocks)
                    raise _Err("content safety")
                return inner_create(**kwargs)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def test_content_filter_fallback_drops_labels_with_the_images(tmp_path, monkeypatch):
    pipe, _ = _pipe(tmp_path, monkeypatch, {1: TEXT, 2: PHOTO, 3: TEXT}, threshold=150)
    client = _RejectImagesClient()
    monkeypatch.setattr("core.pipeline.AzureOpenAI", lambda **kwargs: client)
    pipe.analyze_document()
    first, retry = client.blocks_seen
    assert "Seite 1:" in _texts(first)
    assert len(retry) == 1 and retry[0]["type"] == "text"
    assert pipe.analyze_vision_dropped is True


def test_cap_marks_the_pipeline(tmp_path, monkeypatch):
    pipe, _ = _pipe(tmp_path, monkeypatch, {p: TEXT for p in range(1, 52)}, threshold=None)
    pipe.analyze_document()
    assert pipe.analyze_images_capped is True


def test_no_cap_leaves_the_pipeline_unmarked(tmp_path, monkeypatch):
    pipe, _ = _pipe(tmp_path, monkeypatch, {1: TEXT}, threshold=None)
    pipe.analyze_document()
    assert pipe.analyze_images_capped is False
