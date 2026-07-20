# VCC + BPS Field Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three PM-requested extraction-output changes: VCC `sender.iban`/`sender.bic` (duplicated from `payment`), VCC deterministic `qty`/`unit` defaults, and BPS inferred `tradeType`.

**Architecture:** Two independent work streams. Stream A (VCC) = schema + prompt edit plus a new product-agnostic `postprocess_extraction` hook on `ProductConfig` invoked per subdocument in the pipeline. Stream B (BPS) = schema + prompt edit only; the new field rides the existing pass-through `result` dict.

**Tech Stack:** Python 3.11, pytest, FastAPI, Azure OpenAI. No new dependencies.

## Global Constraints

- Products are configured via `products/<name>/product.py` exporting `CONFIG: ProductConfig`; schema lives in `extract_schema.json`, prompt in `extract_prompt.py`.
- Prompt files build the JSON-Ziel-Schema as a literal string; VCC uses `{{`/`}}` (str.format-escaped braces), BPS uses plain `{`/`}`. Preserve each file's existing brace style when editing.
- Run tests from the repo root with `python -m pytest`.
- Deploy: two separate deploys, **unique tags** each (never `latest`) — e.g. `./deploy.sh v20260720a`.
- No API-layer changes: `JobStatusResponse.result` is already a pass-through dict.

---

### Task 1: VCC — duplicate `iban`/`bic` onto `sender`

**Files:**
- Modify: `products/vetcostcheck/extract_schema.json:22`
- Modify: `products/vetcostcheck/extract_prompt.py:76` (sender block), `:57` (rule 14)
- Test: `tests/products/vetcostcheck/test_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: VCC extract schema `properties.sender.properties` now includes `iban` and `bic` (both `["string","null"]`); `payment` retains them unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/products/vetcostcheck/test_smoke.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/products/vetcostcheck/test_smoke.py -v`
Expected: FAIL — `KeyError: 'iban'` on `sender_props` / prompt count is 1.

- [ ] **Step 3: Edit the schema**

In `products/vetcostcheck/extract_schema.json`, the `sender` properties end at `vatId` (line 22). Change:

```json
        "vatId": {"type": ["string", "null"]}
```

to:

```json
        "vatId": {"type": ["string", "null"]},
        "iban": {"type": ["string", "null"]},
        "bic": {"type": ["string", "null"]}
```

Leave the `payment` block (lines 35-43) unchanged.

- [ ] **Step 4: Edit the prompt**

In `products/vetcostcheck/extract_prompt.py`, the sender block ends with `vatId` (line 76):

```python
"  \"contactMail\": \"string|null\",\n"
"  \"vatId\": \"string|null\"\n"
"}},\n"
```

Change the `vatId` line and add two lines:

```python
"  \"contactMail\": \"string|null\",\n"
"  \"vatId\": \"string|null\",\n"
"  \"iban\": \"string|null\",\n"
"  \"bic\": \"string|null\"\n"
"}},\n"
```

Then extend rule 14 (line 57) so the model knows to fill both. Change:

```python
"14. IBAN (DE) = 22 Zeichen; BIC = 8 oder 11 Zeichen, upper-case.\n"
```

to:

```python
"14. IBAN (DE) = 22 Zeichen; BIC = 8 oder 11 Zeichen, upper-case. Die IBAN und BIC des Belegerstellers in 'sender' UND in 'payment' eintragen (identische Werte).\n"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/products/vetcostcheck/test_smoke.py -v`
Expected: PASS (all tests in file).

- [ ] **Step 6: Commit**

```bash
git add products/vetcostcheck/extract_schema.json products/vetcostcheck/extract_prompt.py tests/products/vetcostcheck/test_smoke.py
git commit -m "VCC: duplicate iban/bic onto sender (non-breaking)"
```

---

### Task 2: VCC — `postprocess_extraction` pure function

**Files:**
- Create: `products/vetcostcheck/postprocess.py`
- Test: `tests/products/vetcostcheck/test_postprocess.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `products.vetcostcheck.postprocess.postprocess_extraction(data: dict) -> dict` — mutates and returns the per-subdocument extraction dict; coerces each `items[]` entry's `qty in (None, 0) -> 1` and `unit` (None or blank) -> `"Stück"`. Tolerates missing/non-list `items` and non-dict entries.

- [ ] **Step 1: Write the failing test**

Create `tests/products/vetcostcheck/test_postprocess.py`:

```python
from products.vetcostcheck.postprocess import postprocess_extraction


def test_qty_none_and_unit_none_get_defaults():
    data = {"items": [{"name": "X", "qty": None, "unit": None}]}
    out = postprocess_extraction(data)
    assert out["items"][0]["qty"] == 1
    assert out["items"][0]["unit"] == "Stück"


def test_qty_zero_becomes_one():
    data = {"items": [{"name": "X", "qty": 0, "unit": "Stk"}]}
    out = postprocess_extraction(data)
    assert out["items"][0]["qty"] == 1
    assert out["items"][0]["unit"] == "Stk"


def test_blank_unit_becomes_stueck():
    data = {"items": [{"name": "X", "qty": 2, "unit": "   "}]}
    out = postprocess_extraction(data)
    assert out["items"][0]["unit"] == "Stück"


def test_populated_values_untouched():
    data = {"items": [{"name": "X", "qty": 3, "unit": "Tabletten"}]}
    out = postprocess_extraction(data)
    assert out["items"][0]["qty"] == 3
    assert out["items"][0]["unit"] == "Tabletten"


def test_missing_items_key_is_noop():
    assert postprocess_extraction({}) == {}


def test_non_list_items_is_noop():
    data = {"items": None}
    assert postprocess_extraction(data) == {"items": None}


def test_non_dict_item_skipped():
    data = {"items": ["not a dict", {"qty": None, "unit": None}]}
    out = postprocess_extraction(data)
    assert out["items"][0] == "not a dict"
    assert out["items"][1]["qty"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/products/vetcostcheck/test_postprocess.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'products.vetcostcheck.postprocess'`.

- [ ] **Step 3: Write minimal implementation**

Create `products/vetcostcheck/postprocess.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/products/vetcostcheck/test_postprocess.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add products/vetcostcheck/postprocess.py tests/products/vetcostcheck/test_postprocess.py
git commit -m "VCC: add qty/unit default post-processor (pure fn)"
```

---

### Task 3: Wire the post-process hook into core + VCC config

**Files:**
- Modify: `core/product.py:26` (add field)
- Modify: `core/pipeline.py:307-318` (apply hook in `_extract_single_subdocument`)
- Modify: `products/vetcostcheck/product.py`
- Test: `tests/products/vetcostcheck/test_smoke.py`, `tests/products/bps/test_smoke.py`

**Interfaces:**
- Consumes: `products.vetcostcheck.postprocess.postprocess_extraction` (Task 2).
- Produces: `ProductConfig.postprocess_extraction: Callable[[dict], dict] | None` (default `None`); pipeline applies it per subdocument when set. VCC `CONFIG.postprocess_extraction` is wired; BPS/Sanierer remain `None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/products/vetcostcheck/test_smoke.py`:

```python
def test_vetcostcheck_postprocess_wired(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    config = load_product_config()
    assert callable(config.postprocess_extraction)
    out = config.postprocess_extraction({"items": [{"qty": None, "unit": None}]})
    assert out["items"][0]["qty"] == 1
    assert out["items"][0]["unit"] == "Stück"
```

Add to `tests/products/bps/test_smoke.py`:

```python
def test_bps_has_no_postprocess(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    assert config.postprocess_extraction is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/products/vetcostcheck/test_smoke.py::test_vetcostcheck_postprocess_wired tests/products/bps/test_smoke.py::test_bps_has_no_postprocess -v`
Expected: FAIL — `AttributeError: 'ProductConfig' object has no attribute 'postprocess_extraction'`.

- [ ] **Step 3: Add the field to `ProductConfig`**

In `core/product.py`, add the field after `analyze_output_schema` (line 24), before `extra` (line 26). Add `Callable` is already imported. Result:

```python
    analyze_prompt_builder: Callable[..., str] | None = None
    analyze_output_schema: dict | None = None

    # Optional product-specific transform applied to each subdocument's
    # extraction dict after the LLM call. None = no-op.
    postprocess_extraction: Callable[[dict], dict] | None = None

    extra: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 4: Apply the hook in the pipeline**

In `core/pipeline.py`, `_extract_single_subdocument` currently ends (lines 307-318) with `return processor.extract(...)`. Change the tail from:

```python
        return processor.extract(
            str(local_image),
            use_ocr=True,
            use_vision=True,
            markdown_text=subdoc.markdown,
            prompt=self.product_config.extract_prompt_builder(
                ocr_text=ocr_text,
                subdocument_context=subdocument_context,
                expected_items=expected_items,
            ),
            subdocument_context=subdocument_context,
        )
```

to:

```python
        result = processor.extract(
            str(local_image),
            use_ocr=True,
            use_vision=True,
            markdown_text=subdoc.markdown,
            prompt=self.product_config.extract_prompt_builder(
                ocr_text=ocr_text,
                subdocument_context=subdocument_context,
                expected_items=expected_items,
            ),
            subdocument_context=subdocument_context,
        )
        if self.product_config.postprocess_extraction is not None:
            result = self.product_config.postprocess_extraction(result)
        return result
```

- [ ] **Step 5: Wire into the VCC config**

In `products/vetcostcheck/product.py`, add the import and the config field. After the existing `from products.vetcostcheck.extract_prompt import build_extract_prompt` (line 12), add:

```python
from products.vetcostcheck.postprocess import postprocess_extraction
```

Then in the `CONFIG = ProductConfig(...)` block, add the field:

```python
CONFIG = ProductConfig(
    name="vetcostcheck",
    extract_prompt_builder=build_extract_prompt,
    extract_output_schema=_EXTRACT_SCHEMA,
    analyze_prompt_builder=build_analyze_prompt,
    analyze_output_schema=ANALYZE_OUTPUT_SCHEMA,
    postprocess_extraction=postprocess_extraction,
)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/products/vetcostcheck/test_smoke.py tests/products/bps/test_smoke.py tests/core/test_product.py -v`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add core/product.py core/pipeline.py products/vetcostcheck/product.py tests/products/vetcostcheck/test_smoke.py tests/products/bps/test_smoke.py
git commit -m "core: add per-product postprocess_extraction hook; wire VCC qty/unit defaults"
```

---

### Task 4: BPS — inferred `tradeType`

**Files:**
- Modify: `products/bps/extract_schema.json:6-10` (add top-level property)
- Modify: `products/bps/extract_prompt.py` (new rule ~22 + schema block line ~71)
- Test: `tests/products/bps/test_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: BPS extract schema top-level `tradeType` = `{"type": ["string","null"], "enum": [<19 values>, null]}`; prompt instructs single-pass inference (null when no signal).

The 19 enum values (English), in this order:
`LOCKSMITH, ROAD_CONSTRUCTION, ADVERTISING, DRAIN_CLEANING, GARDENING, RESTORATION, METAL_CONSTRUCTION, MISC, CARPENTER, ROOFER, HEATING_INSTALLATION, ELECTRICIAN, GLAZIER, CABINET_MAKER, TILER, DRYWALL_BUILDER, BRICK_LAYER, FLOORER, PAINTER`

- [ ] **Step 1: Write the failing test**

Add to `tests/products/bps/test_smoke.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/products/bps/test_smoke.py -v`
Expected: FAIL — `KeyError: 'tradeType'`.

- [ ] **Step 3: Edit the schema**

In `products/bps/extract_schema.json`, add `tradeType` as the first property after `issuedAt` (line 10). Change:

```json
    "issuedAt": {"type": ["string", "null"], "description": "Belegdatum, YYYY-MM-DD"},
    "sender": {
```

to:

```json
    "issuedAt": {"type": ["string", "null"], "description": "Belegdatum, YYYY-MM-DD"},
    "tradeType": {
      "type": ["string", "null"],
      "enum": ["LOCKSMITH", "ROAD_CONSTRUCTION", "ADVERTISING", "DRAIN_CLEANING", "GARDENING", "RESTORATION", "METAL_CONSTRUCTION", "MISC", "CARPENTER", "ROOFER", "HEATING_INSTALLATION", "ELECTRICIAN", "GLAZIER", "CABINET_MAKER", "TILER", "DRYWALL_BUILDER", "BRICK_LAYER", "FLOORER", "PAINTER", null],
      "description": "Gewerk, inferred from company name / work description; NOT printed on the Beleg. null when no signal."
    },
    "sender": {
```

- [ ] **Step 4: Add the extraction rule to the prompt**

In `products/bps/extract_prompt.py`, insert a new rule after rule 20 (line 64, "Beigefügte E-Mails ...") and before rule 21. Insert:

```python
"22. Gewerk ('tradeType'): Das Gewerk steht NICHT auf dem Beleg. Leite es aus dem Firmennamen und/oder der Leistungsbeschreibung ab und gib den englischen Enum-Wert zurück. Wenn ein Gewerk erkennbar, aber nicht in der Liste ist → MISC. Wenn kein Anhaltspunkt erkennbar ist → null (nicht raten). Zuordnung (deutsch → Enum): Schlüsseldienst=LOCKSMITH, Straßen-/Tiefbau=ROAD_CONSTRUCTION, Werbung/Grafik=ADVERTISING, Kanal-/Rohrreinigung=DRAIN_CLEANING, Garten-/Landschaftsbau=GARDENING, Sanierer/Bautrocknung=RESTORATION, Metallbau/Tore/Markisen=METAL_CONSTRUCTION, Sonstige=MISC, Zimmermann=CARPENTER, Dachdecker=ROOFER, Heizung-Sanitärinstallateur=HEATING_INSTALLATION, Elektriker/Elektroinstallateur=ELECTRICIAN, Fensterbauer/Glaser=GLAZIER, Tischler/Schreiner=CABINET_MAKER, Fliesenleger=TILER, Trockenbauer=DRYWALL_BUILDER, Maurer/Putzer=BRICK_LAYER, Bodenleger/Parkettleger=FLOORER, Maler/Lackierer=PAINTER.\n"
```

Note: the existing block ends rule 21 with `\n\n` before the schema. Renumber is not required (rule 20 stays; new rule is 22 after 21). To keep numeric order, place this block **after** rule 21 (line 65) instead — append it as rule 22 right before the `"JSON-Ziel-Schema:\n"` line, keeping the `\n\n` separator on the last rule. Concretely, change:

```python
"21. Alle Positionen in der Reihenfolge des Belegs extrahieren. Achte besonders darauf, ALLE Zeilen innerhalb von Tabellen zu erfassen — nicht zusammenfassen, nicht deduplizieren.\n\n"
"JSON-Ziel-Schema:\n"
```

to:

```python
"21. Alle Positionen in der Reihenfolge des Belegs extrahieren. Achte besonders darauf, ALLE Zeilen innerhalb von Tabellen zu erfassen — nicht zusammenfassen, nicht deduplizieren.\n"
"22. Gewerk ('tradeType'): Das Gewerk steht NICHT auf dem Beleg. Leite es aus dem Firmennamen und/oder der Leistungsbeschreibung ab und gib den englischen Enum-Wert zurück. Wenn ein Gewerk erkennbar, aber nicht in der Liste ist → MISC. Wenn kein Anhaltspunkt erkennbar ist → null (nicht raten). Zuordnung (deutsch → Enum): Schlüsseldienst=LOCKSMITH, Straßen-/Tiefbau=ROAD_CONSTRUCTION, Werbung/Grafik=ADVERTISING, Kanal-/Rohrreinigung=DRAIN_CLEANING, Garten-/Landschaftsbau=GARDENING, Sanierer/Bautrocknung=RESTORATION, Metallbau/Tore/Markisen=METAL_CONSTRUCTION, Sonstige=MISC, Zimmermann=CARPENTER, Dachdecker=ROOFER, Heizung-Sanitärinstallateur=HEATING_INSTALLATION, Elektriker/Elektroinstallateur=ELECTRICIAN, Fensterbauer/Glaser=GLAZIER, Tischler/Schreiner=CABINET_MAKER, Fliesenleger=TILER, Trockenbauer=DRYWALL_BUILDER, Maurer/Putzer=BRICK_LAYER, Bodenleger/Parkettleger=FLOORER, Maler/Lackierer=PAINTER.\n\n"
"JSON-Ziel-Schema:\n"
```

- [ ] **Step 5: Add `tradeType` to the JSON-Ziel-Schema block**

In the same file, the schema block has `"issuedAt": "YYYY-MM-DD|null",` (line 71) followed by `"sender": {`. Change:

```python
"\"issuedAt\": \"YYYY-MM-DD|null\",\n"
"\"sender\": {\n"
```

to:

```python
"\"issuedAt\": \"YYYY-MM-DD|null\",\n"
"\"tradeType\": \"<einer der Enum-Werte>|null\",\n"
"\"sender\": {\n"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/products/bps/test_smoke.py -v`
Expected: PASS (all tests in file).

- [ ] **Step 7: Commit**

```bash
git add products/bps/extract_schema.json products/bps/extract_prompt.py tests/products/bps/test_smoke.py
git commit -m "BPS: add inferred tradeType (single-pass, nullable enum)"
```

---

### Task 5: End-to-end eyeball + deploy (manual gate)

**Files:** none (verification + deploy only).

- [ ] **Step 1: Full test suite green**

Run: `python -m pytest -q`
Expected: PASS (no regressions).

- [ ] **Step 2: VCC real extraction eyeball**

Run against a VCC test PDF with visible bank details and at least one line item lacking qty/unit:

```bash
PRODUCT_NAME=vetcostcheck STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 \
    python scripts/extract_local.py <path/to/vcc.pdf> > /tmp/vcc_out.json 2>/tmp/vcc.log
```

Confirm in `/tmp/vcc_out.json`: `sender.iban`/`sender.bic` populated (matching `payment.*`), and every `items[].qty` is a number ≥1 with a non-empty `unit`.

- [ ] **Step 3: Deploy VCC (unique tag)**

Deploy the VCC container app pair with a unique tag (see `deploy.sh`). Notify the PM that `sender.iban`/`sender.bic` are now available (and `payment.*` still works — consumer can migrate later).

- [ ] **Step 4: BPS real extraction eyeball (gates single-pass decision)**

Run against several BPS test PDFs in `bps_sanierer_input/BPS_Input/`:

```bash
PRODUCT_NAME=bps STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 \
    python scripts/extract_local.py bps_sanierer_input/BPS_Input/BPS_1.pdf > /tmp/bps_out.json 2>/tmp/bps.log
```

Confirm each subdocument has a `tradeType` that is null or a valid enum value, and spot-check that non-null values are plausible given the firm/description. **If accuracy is poor across the batch**, stop and revisit the spec's documented fallback (a dedicated second classification pass) before deploying.

- [ ] **Step 5: Deploy BPS (unique tag)**

Deploy the BPS container app pair with a unique tag.

---

## Self-Review

**Spec coverage:**
- A1 (sender iban/bic, duplicated) → Task 1. ✓
- A2 (qty/unit deterministic post-process, VCC-only) → Tasks 2 + 3. ✓
- B1 (BPS tradeType, single-pass, nullable enum, 19 values, mapping table) → Task 4. ✓
- Testing (smoke updates, postprocess unit test, real extraction eyeball) → in Tasks 1–4 + Task 5. ✓
- Deploy (two deploys, unique tags, A before B) → Task 5. ✓
- No API change (pass-through result) → Global Constraints. ✓

**Placeholder scan:** none — all code shown; `<19 values>` in the interface block is expanded to the literal list in Task 4 Step 3.

**Type consistency:** `postprocess_extraction(data: dict) -> dict` is identical in Task 2 (definition), Task 3 (field type `Callable[[dict], dict] | None`, wiring), and the pipeline call site. Enum value list identical between schema (Step 3), prompt rule (Step 4), and test (Step 1).
