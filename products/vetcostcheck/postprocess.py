"""VCC-only extraction post-processing.

The extraction prompt emits qty=null / unit=null when it cannot find a
quantity or unit. The 3C entry mask needs those fields pre-filled, so this
step coerces the defaults deterministically after extraction. This is the
single source of truth for the rule — the prompt is left unchanged.
"""
from __future__ import annotations


def postprocess_extraction(data: dict) -> dict:
    """Coerce per-item qty/unit defaults on one subdocument's extraction dict.

    qty in (None, 0) -> 1; unit that is None or blank -> "Stück".
    All other values pass through untouched. Mutates and returns `data`.
    """
    items = data.get("items")
    if not isinstance(items, list):
        return data
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("qty") in (None, 0):
            item["qty"] = 1
        unit = item.get("unit")
        if unit is None or (isinstance(unit, str) and unit.strip() == ""):
            item["unit"] = "Stück"
    return data
