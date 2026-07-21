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
    prompt = config.extract_prompt_builder(ocr_text="dummy ocr", subdocument_context={})
    assert isinstance(prompt, str)
    assert len(prompt) > 100  # the vet prompt is long; sanity check


def test_vetcostcheck_sender_has_bank_fields_duplicated(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    config = load_product_config()
    props = config.extract_output_schema["properties"]
    sender_props = props["sender"]["properties"]
    payment_props = props["payment"]["properties"]
    # duplicated onto sender
    assert sender_props["iban"] == {"type": ["string", "null"]}
    assert sender_props["bic"] == {"type": ["string", "null"]}
    # still present on payment (non-breaking)
    assert "iban" in payment_props and "bic" in payment_props


def test_vetcostcheck_prompt_lists_iban_in_two_places(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    config = load_product_config()
    prompt = config.extract_prompt_builder(ocr_text="x", subdocument_context={})
    # once under sender, once under payment
    assert prompt.count('"iban"') >= 2
    assert prompt.count('"bic"') >= 2


def test_vetcostcheck_postprocess_wired(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    config = load_product_config()
    assert callable(config.postprocess_extraction)
    out = config.postprocess_extraction({"items": [{"qty": None, "unit": None}]})
    assert out["items"][0]["qty"] == 1
    assert out["items"][0]["unit"] == "Stück"
