"""Pipeline-level test: the product's postprocess_extraction hook is actually
applied to each subdocument's extraction result inside
`Pipeline._extract_single_subdocument`.

This locks in the wiring that unit tests of the pure function and the config
smoke tests cannot catch: a refactor that dropped the `if ... is not None`
block, or bypassed the shared method on one execution path, would slip past
`test_vetcostcheck_postprocess_wired` but fail here.
"""
import pytest

from core.pipeline import Pipeline, SubdocumentArtifact
from core.product import ProductConfig, load_product_config
from core.returncode import GENERIC_REASON


class _FakeStorage:
    def materialize_to_local(self, key):
        return "/tmp/does-not-need-to-exist.png"


class _FakeProcessor:
    """Stands in for the real LLM processor; returns a fixed extraction dict."""

    def __init__(self, result):
        self._result = result

    def extract(self, *args, **kwargs):
        return self._result


class _RaisingProcessor:
    """Stands in for the real LLM processor; records/fails if `extract` is
    ever reached. Used to prove the empty-markdown short-circuit happens
    before the processor is called at all.
    """

    def __init__(self):
        self.called = False

    def extract(self, *args, **kwargs):
        self.called = True
        raise AssertionError("processor.extract must not be called for empty markdown")


def _make_pipeline(product_config):
    """Build a Pipeline without running __init__ (which does OCR/PDF I/O)."""
    pipe = object.__new__(Pipeline)
    pipe.storage = _FakeStorage()
    pipe.analysis_dict = {}
    pipe.product_config = product_config
    return pipe


_SUBDOC = SubdocumentArtifact(
    document_number=1,
    page_numbers=[1],
    markdown="dummy markdown",
    md_key="k.md",
    pdf_key="k.pdf",
    image_key="k.png",
)


def test_pipeline_applies_vcc_postprocess_hook(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    pipe = _make_pipeline(load_product_config())
    processor = _FakeProcessor({"items": [{"qty": None, "unit": None}]})

    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    # The VCC hook coerced the empty qty/unit produced by the processor.
    assert result["items"][0]["qty"] == 1
    assert result["items"][0]["unit"] == "Stück"


def test_pipeline_passthrough_when_no_hook():
    # A product with no postprocess hook must leave the processor output alone —
    # proves the coercion above comes from the hook, not from anywhere else.
    cfg = ProductConfig(
        name="no_hook_test",
        extract_prompt_builder=lambda **kwargs: "prompt",
        extract_output_schema={},
    )
    pipe = _make_pipeline(cfg)
    processor = _FakeProcessor({"items": [{"qty": None, "unit": None}]})

    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert result["items"][0]["qty"] is None
    assert result["items"][0]["unit"] is None


def test_pipeline_applies_returncode_floor_after_the_hook(monkeypatch):
    # The floor is unconditional and runs last, so a product with a hook still
    # gets the guaranteed contract — and the hook's output is what it judges.
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    pipe = _make_pipeline(load_product_config())
    processor = _FakeProcessor({"items": [{"qty": None, "unit": None}]})

    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert list(result)[:2] == ["returncode", "returncodeReasons"]
    assert result["returncode"] == 100  # non-empty items are Beleg evidence
    assert result["returncodeReasons"] == []
    assert result["items"][0]["qty"] == 1  # the VCC hook still ran


def test_pipeline_floor_applies_without_a_hook():
    cfg = ProductConfig(
        name="no_hook_test",
        extract_prompt_builder=lambda **kwargs: "prompt",
        extract_output_schema={},
    )
    pipe = _make_pipeline(cfg)
    processor = _FakeProcessor({"type": None, "number": None, "items": []})

    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert result["returncode"] == 200
    assert result["returncodeReasons"] == [GENERIC_REASON]


@pytest.mark.parametrize("empty_markdown", ["", "   ", "\n\n  \t"])
def test_pipeline_empty_markdown_returns_300_without_calling_processor(empty_markdown):
    # A blank scan / photo-only page yields empty OCR markdown. This is direct
    # evidence of unreadability, known before any LLM call — the processor
    # must never be reached, and no ValueError("Not enough markdown text...")
    # should escape and fail the whole job.
    cfg = ProductConfig(
        name="no_hook_test",
        extract_prompt_builder=lambda **kwargs: "prompt",
        extract_output_schema={},
    )
    pipe = _make_pipeline(cfg)
    subdoc = SubdocumentArtifact(
        document_number=1,
        page_numbers=[1],
        markdown=empty_markdown,
        md_key="k.md",
        pdf_key="k.pdf",
        image_key="k.png",
    )
    processor = _RaisingProcessor()

    result = pipe._extract_single_subdocument(subdoc, processor)

    assert processor.called is False
    assert result == {
        "returncode": 300,
        "returncodeReasons": ["Der Inhalt konnte nicht gelesen werden."],
    }


def test_pipeline_non_dict_extraction_result_coerced_to_generic_200():
    # extract_json_from_response can hand back a list (the model replied with
    # a bare JSON array). Neither the postprocess hook nor the returncode
    # floor can call .get() on that — it must be coerced to {} before the
    # floor runs, so the job classifies 200 with the generic reason instead
    # of crashing with an AttributeError.
    cfg = ProductConfig(
        name="no_hook_test",
        extract_prompt_builder=lambda **kwargs: "prompt",
        extract_output_schema={},
    )
    pipe = _make_pipeline(cfg)
    processor = _FakeProcessor([{"unexpected": "list"}])

    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert result["returncode"] == 200
    assert result["returncodeReasons"] == [GENERIC_REASON]
