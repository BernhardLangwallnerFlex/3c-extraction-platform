"""Pipeline-level test: the product's postprocess_extraction hook is actually
applied to each subdocument's extraction result inside
`Pipeline._extract_single_subdocument`.

This locks in the wiring that unit tests of the pure function and the config
smoke tests cannot catch: a refactor that dropped the `if ... is not None`
block, or bypassed the shared method on one execution path, would slip past
`test_vetcostcheck_postprocess_wired` but fail here.
"""
from core.pipeline import Pipeline, SubdocumentArtifact
from core.product import ProductConfig, load_product_config


class _FakeStorage:
    def materialize_to_local(self, key):
        return "/tmp/does-not-need-to-exist.png"


class _FakeProcessor:
    """Stands in for the real LLM processor; returns a fixed extraction dict."""

    def __init__(self, result):
        self._result = result

    def extract(self, *args, **kwargs):
        return self._result


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
