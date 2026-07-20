# VCC + BPS field updates — design

**Date:** 2026-07-20
**Products:** vetcostcheck (VCC), bps (BPS)
**Status:** approved for implementation

Three PM-requested extraction-output changes across two products. Independent of
each other; grouped into **two work streams / two deploys** because A1+A2 share
VCC's files and B1 is BPS-only.

---

## Work stream A — VCC

### A1. Surface `iban` + `bic` on `sender` (additive, non-breaking)

**Ask (PM):** "ein Kunde hätte gerne dass wir noch BIC und IBAN des
Belegerstellers erfassen."

**Finding:** VCC already extracts the practice's bank details as
`payment.iban` / `payment.bic` (present in both `extract_schema.json` and the
prompt, incl. rule 14 for the DE IBAN=22 / BIC=8-or-11 format). The customer
wants them on the Belegersteller, i.e. the `sender`.

**Decision:** **Duplicate**, do not move. Add `iban`/`bic` to `sender` **and**
keep them in `payment`. This is deliberately non-breaking — any existing 3C
consumer reading `payment.iban`/`payment.bic` keeps working and can migrate to
`sender.*` at their own pace.

**Changes:**
- `products/vetcostcheck/extract_schema.json`
  - Add `"iban": {"type": ["string","null"]}` and `"bic": {"type": ["string","null"]}`
    to the `sender` properties.
  - Leave `payment.iban` / `payment.bic` in place.
- `products/vetcostcheck/extract_prompt.py`
  - Add `iban`/`bic` to the `sender` block of the JSON-Ziel-Schema (in addition
    to the existing `payment` block).
  - Rule 14 (IBAN/BIC format) is unchanged.
  - Both objects describe the same source values (the Belegersteller's bank
    details); the model fills both identically.

### A2. Default qty/unit via deterministic post-processing

**Ask (PM):** vet Prüfer want `qty=1` / `unit="Stück"` instead of null/empty
when the model finds no quantity/unit — the employees' entry mask has empty
fields that otherwise need manual filling.

**Decision:** **Deterministic post-process** (not a prompt change). The prompt
still emits `qty=null` / `unit=null` when it can't find a value (rule 9,
unchanged); a VCC-only post-processing step coerces the defaults. This guarantees
no empty qty/unit ever reaches the entry mask, independent of model behavior.
Post-processing is the single source of truth for this rule.

**Coercion (per line item in `items[]`):**
- `qty` in `(None, 0)` → `1`
- `unit` in `(None, "")` (after strip) → `"Stück"`

Only these two fields; all other extracted values pass through untouched. A
model-emitted `qty=0` is treated as "not found" and coerced to `1` (0-quantity
line items are not meaningful in this domain).

**Changes:**
- `core/product.py` — add optional hook to `ProductConfig`:
  ```python
  postprocess_extraction: Callable[[dict], dict] | None = None
  ```
  Product-agnostic. `None` → no-op (BPS, Sanierer unaffected).
- `core/pipeline.py` — in `_extract_single_subdocument`, after
  `processor.extract(...)` returns the subdocument dict, apply
  `self.product_config.postprocess_extraction(result)` when it is not `None`,
  and return the transformed dict. Applied per subdocument so it covers the
  multi-invoice case.
- `products/vetcostcheck/postprocess.py` (new) — pure function
  `postprocess_extraction(data: dict) -> dict` implementing the coercion above.
  Defensive: tolerates missing `items` key / non-list items.
- `products/vetcostcheck/product.py` — wire
  `postprocess_extraction=postprocess_extraction` into `CONFIG`.

---

## Work stream B — BPS

### B1. Add inferred `tradeType` (Gewerk)

**Ask (PM):** deliver the Gewerk per Beleg. It is **not printed on the invoice**
— the model infers it from the company name / description of work. Ideal is a
`TradeType` element filled with the English text.

**Decision — single LLM pass:** classification happens in the **same extraction
call**, not a separate pass. The model already has the company name and every
line item in context, so inferring the trade is nearly free. **Fallback (not
built now):** if accuracy on a test batch is poor, add a dedicated
classification LLM pass over the condensed extraction result. Documented here so
the option is on record.

**Decision — nullable:** when there is genuinely no signal, emit `null` (not
`MISC`). This lets reviewers distinguish "model couldn't tell" from a confident
`MISC`. `MISC` (=Sonstige) is reserved for trades that are identifiable but
outside the enum.

**Enum (19 values, English; German meaning for prompt guidance):**

| tradeType | Übersetzung |
|---|---|
| LOCKSMITH | Schlüsseldienst |
| ROAD_CONSTRUCTION | Straßen-/Tiefbau |
| ADVERTISING | Werbung/Grafik |
| DRAIN_CLEANING | Kanal-/Rohrreinigung |
| GARDENING | Garten-/Landschaftsbau |
| RESTORATION | Sanierer/Bautrocknung |
| METAL_CONSTRUCTION | Metallbau/Tore/Markisen |
| MISC | Sonstige |
| CARPENTER | Zimmermann |
| ROOFER | Dachdecker |
| HEATING_INSTALLATION | Heizung-Sanitärinstallateur |
| ELECTRICIAN | Elektriker/Elektroinstallateur |
| GLAZIER | Fensterbauer/Glaser |
| CABINET_MAKER | Tischler/Schreiner |
| TILER | Fliesenleger |
| DRYWALL_BUILDER | Trockenbauer |
| BRICK_LAYER | Maurer/Putzer |
| FLOORER | Bodenleger/Parkettleger |
| PAINTER | Maler/Lackierer |

**Changes:**
- `products/bps/extract_schema.json` — new **top-level** property:
  ```json
  "tradeType": {"type": ["string","null"], "enum": [<19 values>, null],
    "description": "Gewerk, inferred from company name / work description; not printed on the Beleg. null when no signal."}
  ```
- `products/bps/extract_prompt.py`
  - New extraction rule: infer the Gewerk from Firmenname / Leistungsbeschreibung,
    output the English enum value; `MISC` for identifiable-but-not-listed;
    `null` when no signal. Include the German→English mapping table above as
    guidance.
  - Add `"tradeType": "..."` to the JSON-Ziel-Schema block.
- **No API-layer change:** `JobStatusResponse.result` is already a pass-through
  dict (commit 67755cc), so the new field flows through unmodified.

---

## Testing (both streams)

- Update the affected per-product smoke tests
  (`tests/products/vetcostcheck/test_smoke.py`,
  `tests/products/bps/test_smoke.py`) to assert the new output shape:
  - VCC: `sender.iban`/`sender.bic` keys present; post-process yields no
    `qty in (None,0)` and no empty `unit` in `items`.
  - BPS: `tradeType` present and (when non-null) a member of the enum.
- Add a focused unit test for `vetcostcheck.postprocess.postprocess_extraction`
  covering: null→default, 0→1, empty-string unit→"Stück", populated values
  untouched, missing/empty `items`.
- Before each deploy, run a real extraction via `scripts/extract_local.py` on a
  representative PDF for that product and eyeball the new fields — especially
  BPS `tradeType` accuracy, which gates the single-pass-vs-second-pass decision.

## Deploy

Two separate deploys, **unique tags** each (never `latest`). Ship Work stream A
first, then B.

## Out of scope / follow-ups

- Sanierer is untouched.
- BPS second classification pass (only if single-pass accuracy is insufficient).
- 3C consumer migration from `payment.*` to `sender.*` for VCC bank details
  (their side; enabled but not required by A1).
