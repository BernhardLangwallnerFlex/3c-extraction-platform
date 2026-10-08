import pytest
from core.product import load_product_config


def test_load_requires_product_name(monkeypatch):
    monkeypatch.delenv("PRODUCT_NAME", raising=False)
    with pytest.raises(RuntimeError, match="PRODUCT_NAME"):
        load_product_config()


def test_load_with_explicit_name_only(monkeypatch):
    monkeypatch.delenv("PRODUCT_NAME", raising=False)
    with pytest.raises(ModuleNotFoundError):
        load_product_config("nonexistent_product")


@pytest.mark.parametrize("name, expected", [("bps", 200), ("sanierer", 200), ("vetcostcheck", None)])
def test_low_text_threshold_per_product(name, expected):
    assert load_product_config(name).analyze_low_text_threshold == expected
