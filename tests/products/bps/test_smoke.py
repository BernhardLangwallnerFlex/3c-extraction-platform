"""Smoke test for the bps product. Mirrors the vetcostcheck smoke test."""
from core.product import load_product_config


def test_bps_config_loads(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    assert config.name == "bps"
    assert callable(config.extract_prompt_builder)
    assert callable(config.analyze_prompt_builder)
    assert isinstance(config.extract_output_schema, dict)
    assert config.extract_output_schema  # non-empty


def test_bps_extract_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    # subdocument_context must be accepted and ignored by BPS.
    prompt = config.extract_prompt_builder(ocr_text="dummy ocr", subdocument_context=None)
    assert isinstance(prompt, str)
    assert len(prompt) > 500


def test_bps_analyze_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    prompt = config.analyze_prompt_builder(markdown_text="--- PAGE 1 --- x")
    assert isinstance(prompt, str)
    assert "invoice_pages" in prompt


def test_bps_has_no_postprocess(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    assert config.postprocess_extraction is None


def test_bps_schema_has_tradetype(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    tt = config.extract_output_schema["properties"]["tradeType"]
    assert tt["type"] == ["string", "null"]
    non_null = [v for v in tt["enum"] if v is not None]
    assert len(non_null) == 19
    assert "PAINTER" in non_null and "LOCKSMITH" in non_null and "MISC" in non_null
    assert None in tt["enum"]


def test_bps_prompt_mentions_tradetype(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    prompt = config.extract_prompt_builder(ocr_text="x", subdocument_context=None)
    assert "tradeType" in prompt
    assert "PAINTER" in prompt
    assert "Gewerk" in prompt
