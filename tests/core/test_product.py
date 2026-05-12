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
