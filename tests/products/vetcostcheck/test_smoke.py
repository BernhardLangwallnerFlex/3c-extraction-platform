"""Smoke test for the vetcostcheck product.

Verifies that ProductConfig loads, the prompt builder is callable, and the
schema is parseable. Does NOT run an end-to-end extraction (that's what the
regression check is for during the migration).
"""
from core.product import load_product_config


def test_vetcostcheck_config_loads(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    config = load_product_config()
    assert config.name == "vetcostcheck"
    assert callable(config.extract_prompt_builder)
    assert isinstance(config.extract_output_schema, dict)
    assert config.extract_output_schema  # non-empty


def test_vetcostcheck_extract_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    config = load_product_config()
    prompt = config.extract_prompt_builder(ocr_text="dummy ocr", animal_information={})
    assert isinstance(prompt, str)
    assert len(prompt) > 100  # the vet prompt is long; sanity check
