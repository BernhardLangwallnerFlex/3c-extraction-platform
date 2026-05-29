"""bps ProductConfig — Belegprüfung Sach extraction."""
from __future__ import annotations

import json
from pathlib import Path

from core.product import ProductConfig
from products.bps.analyze_overrides import (
    ANALYZE_OUTPUT_SCHEMA,
    build_analyze_prompt,
)
from products.bps.extract_prompt import build_extract_prompt

_HERE = Path(__file__).resolve().parent

with (_HERE / "extract_schema.json").open() as fh:
    _EXTRACT_SCHEMA = json.load(fh)


CONFIG = ProductConfig(
    name="bps",
    extract_prompt_builder=build_extract_prompt,
    extract_output_schema=_EXTRACT_SCHEMA,
    analyze_prompt_builder=build_analyze_prompt,
    analyze_output_schema=ANALYZE_OUTPUT_SCHEMA,
)
