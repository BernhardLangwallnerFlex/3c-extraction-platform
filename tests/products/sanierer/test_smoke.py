"""Smoke test for the sanierer product. Mirrors the bps smoke test."""
from core.product import load_product_config


def test_sanierer_config_loads(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "sanierer")
    config = load_product_config()
    assert config.name == "sanierer"
    assert callable(config.extract_prompt_builder)
    assert callable(config.analyze_prompt_builder)
    assert isinstance(config.extract_output_schema, dict)
    assert config.extract_output_schema  # non-empty


def test_sanierer_extract_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "sanierer")
    config = load_product_config()
    # subdocument_context must be accepted and ignored by Sanierer.
    prompt = config.extract_prompt_builder(ocr_text="dummy ocr", subdocument_context=None)
    assert isinstance(prompt, str)
    assert len(prompt) > 500
    assert "lvPosition" in prompt


def test_sanierer_analyze_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "sanierer")
    config = load_product_config()
    prompt = config.analyze_prompt_builder(markdown_text="--- PAGE 1 --- x")
    assert isinstance(prompt, str)
    assert "invoice_pages" in prompt
