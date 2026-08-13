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


def test_schema_publishes_both_fields_first(config):
    props = config.extract_output_schema["properties"]
    assert list(props)[:2] == ["returncode", "returncodeReasons"]
    assert props["returncode"]["enum"] == [100, 200, 300]
    assert props["returncode"]["type"] == "integer"
    assert props["returncodeReasons"]["type"] == "array"
    assert props["returncodeReasons"]["items"] == {"type": "string"}
