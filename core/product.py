"""Product configuration — the single contract between core and a product."""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ProductConfig:
    """Configuration injected by a product directory into core.

    A product owns its extraction prompt + schema and may optionally override
    the analysis/splitting prompt + schema. Everything else (OCR, storage,
    queue mechanics, API surface) is shared.
    """
    name: str

    extract_prompt_builder: Callable[..., str]
    extract_output_schema: dict

    analyze_prompt_builder: Callable[..., str] | None = None
    analyze_output_schema: dict | None = None

    # Optional product-specific transform applied to each subdocument's
    # extraction dict after the LLM call. None = no-op.
    postprocess_extraction: Callable[[dict], dict] | None = None

    extra: dict[str, Any] = field(default_factory=dict)


def load_product_config(name: str | None = None) -> ProductConfig:
    """Load `products.<name>.product:CONFIG`.

    `name` defaults to the `PRODUCT_NAME` env var. Each Container App sets this
    once at deployment time; locally it's set per `docker compose` invocation.
    """
    name = name or os.environ.get("PRODUCT_NAME")
    if not name:
        raise RuntimeError(
            "PRODUCT_NAME is not set. Each product's Container App must set it; "
            "for local dev, e.g. `PRODUCT_NAME=vetcostcheck docker compose up`."
        )
    module = importlib.import_module(f"products.{name}.product")
    config = getattr(module, "CONFIG", None)
    if not isinstance(config, ProductConfig):
        raise RuntimeError(
            f"products.{name}.product must export CONFIG: ProductConfig (got {type(config).__name__})"
        )
    return config
