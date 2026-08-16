"""The returncode contract is identical across all three products.

Product wording differs (what counts as "not a Beleg" is different for a
Handwerkerrechnung and a Tierarztrechnung) but the codes, the schema and the
anti-downgrade instruction must not drift apart between products.
"""
import pytest

from core.product import load_product_config

PRODUCTS = ["vetcostcheck", "bps", "sanierer"]


@pytest.fixture(params=PRODUCTS)
def config(request, monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", request.param)
    return load_product_config()


def test_prompt_teaches_all_three_codes(config):
    prompt = config.extract_prompt_builder(ocr_text="x", subdocument_context=None)
    assert "returncode" in prompt
    assert "returncodeReasons" in prompt
    for code in ("100", "200", "300"):
        assert code in prompt


def test_prompt_carries_the_anti_downgrade_sentence(config):
    # The single most important line in this feature: without it the model
    # emits 200 whenever extraction went badly, which auto-cancels real claims.
    prompt = config.extract_prompt_builder(ocr_text="x", subdocument_context=None)
    assert "Ein Beleg bleibt 100, auch wenn einzelne Felder nicht ermittelbar sind" in prompt


def test_schema_publishes_the_metadata_fields_first(config):
    props = config.extract_output_schema["properties"]
    assert list(props)[:3] == ["returncode", "returncodeReasons", "qualityFlags"]
    assert props["returncode"]["enum"] == [100, 200, 300]
    assert props["returncode"]["type"] == "integer"
    assert props["returncodeReasons"]["type"] == "array"
    assert props["returncodeReasons"]["items"] == {"type": "string"}
    flags = props["qualityFlags"]
    assert flags["type"] == "array"
    assert flags["items"]["enum"] == ["VISION_DROPPED", "SINGLE_ENGINE_OCR"]


def test_analyze_prompt_allows_zero_belege(config):
    # The splitter used to invent Belege out of cover emails because the prompt
    # never said 0 was allowed — the schema permits it, but the model never
    # sees the schema.
    prompt = config.analyze_prompt_builder(markdown_text="--- PAGE 1 --- x")
    assert "'number_of_invoices': 0" in prompt
    assert "Erfinde keine" in prompt
    # Rendered, not template: .format() must have consumed the doubled braces.
    assert "{{" not in prompt


def test_analyze_schema_still_permits_zero(config):
    assert config.analyze_output_schema["properties"]["number_of_invoices"]["minimum"] == 0
