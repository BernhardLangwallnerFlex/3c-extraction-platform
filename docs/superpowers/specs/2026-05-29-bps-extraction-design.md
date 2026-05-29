# BPS Product Onboarding — Design Spec

**Date:** 2026-05-29
**Status:** Draft for review
**Author:** Bernhard + Claude
**Related:** `docs/superpowers/specs/2026-05-07-multi-product-extraction-design.md` (platform design), `docs/superpowers/plans/2026-05-07-multi-product-platform-refactor.md` (vetcostcheck cutover — the proven template)

## 1. What BPS is

**BPS = "Belegprüfung Sach"** — receipt/document verification for property & contents insurance (Hausrat / Wohngebäude). When a policyholder has a claim (e.g. burglary, water damage), a tradesman (Handwerker) issues a quote (Angebot) or invoice (Rechnung) for the repair. The insurer needs that document's data extracted and structured so it can be verified against the claim.

Domain language is German. The extraction target is defined by `bps_sanierer_input/BPS_Input/Erfassungsmaske_BPS.docx` ("Erfassungs-/Prüfmaske Belegprüfung Sach"). Seven sample PDFs live in `bps_sanierer_input/BPS_Input/` (`BPS_1.pdf` … `BPS_7.pdf`).

Like vetcostcheck, **one PDF can contain 1–N Belege**, and they must be returned all at once, separated per document. PDFs also frequently include a **cover email** (the forwarding message to the insurer) as the first page(s) — these are not themselves Belege.

## 2. Goal

Onboard `bps` as a new product on the multi-product platform:

- A `products/bps/` directory providing a `ProductConfig` (prompt + schema + analyze override).
- Its own Container App pair (`ca-api-bps` / `ca-worker-bps`), queue (`jobs-bps`), image repo (`3cix-bps`), and eventually domain (`3cbps.flex-capital-scale.com`).
- **No changes to `core/`.** The platform refactor already made core product-agnostic; `deploy.sh`, `scripts/provision_product.sh` (now SENTRY-optional), and the `Dockerfile` are already product-parameterized.

Out of scope for this spec: the custom-domain cutover (batched later with Sanierer + the pending vetcostcheck Task 16), and the Sanierer product (its own spec — it will be defined as "analog BPS" + an `lvPosition` field).

## 3. Architecture

BPS mirrors `products/vetcostcheck/` exactly — the same four files:

```
products/bps/
├── __init__.py
├── product.py            # exports CONFIG: ProductConfig
├── extract_prompt.py     # build_extract_prompt(...) — German BPS extraction prompt + JSON target schema
├── extract_schema.json   # JSON Schema documenting the output (validation target; not enforced at runtime)
└── analyze_overrides.py  # build_analyze_prompt(...) + ANALYZE_OUTPUT_SCHEMA — BPS split prompt
```

`product.py` follows the vet template:

```python
CONFIG = ProductConfig(
    name="bps",
    extract_prompt_builder=build_extract_prompt,
    extract_output_schema=_EXTRACT_SCHEMA,
    analyze_prompt_builder=build_analyze_prompt,
    analyze_output_schema=ANALYZE_OUTPUT_SCHEMA,
)
```

The shared `core.pipeline.Pipeline` runs unchanged: OCR → `analyze_document()` (uses `analyze_prompt_builder`) → `split_document_into_invoices()` → `extract_data_from_subdocuments()` (uses `extract_prompt_builder`).

### 3.1 Core-compatibility constraints (must-honor)

These come from how `core/pipeline.py` consumes the product config — violating them breaks the splitter:

1. **Analyze output keys are fixed.** The pipeline splits on `analysis_dict["invoice_pages"]` (pipeline.py:218) and reads `invoice_number_of_items` (pipeline.py:297). BPS's analyze prompt therefore emits the **same JSON keys as vet**: `pages_with_invoice_information`, `number_of_invoices`, `invoice_pages`, `invoice_number_of_items`. Only the prompt *wording* is BPS-specific. `invoice_animals` is simply omitted — the pipeline defaults gracefully (pipeline.py:286–294).
   - Naming note: the keys keep the literal word "invoice" for core compatibility even though BPS calls them "Belege". This is intentional; do not rename without changing core.
2. **Extract builder signature is fixed.** The pipeline calls the extract builder with `ocr_text=`, `animal_information=`, and `expected_items=` (pipeline.py:308–314). BPS's `build_extract_prompt(*, ocr_text="", animal_information=None, expected_items=None)` must **accept `animal_information` and ignore it**.

## 4. Output schema (per sub-document)

The pipeline wraps per-document results in the envelope `{ "number_of_subdocuments": int, "subdocuments": [ <per-doc object> ] }` (unchanged from vet). Each per-document object mirrors the vetcostcheck schema with domain deltas.

### 4.1 Deltas from the vetcostcheck per-document schema

**Kept from vet (same field names/shape):** `type`, `currency`, `number`, `issuedAt`, `sender`, `payment`, `recipient`, `items[]`, `totals`, `warnings`.

**Dropped (vet-only):** `clinicians`, `animals`, `diagnoses`, `serviceDates`, and item-level `got`, `animal`.

**Added (BPS-specific):**
- `serviceProvider` (Dienstleister) — top-level party object.
- `policyholder` (Versicherungsnehmer) — top-level party object.
- `damageLocation` (Schadenort) — top-level location object.
- item-level `position`, `unitCode`, `taxRate`, `discount`.

**Changed semantics:**
- `type` enum → `"invoice"` (Rechnung) | `"quote"` (Angebot) | `null` (Belegart).
- `sender` = the **Belegersteller / Handwerker** (the firm that issued this Beleg). Its IBAN lives in `payment.iban` (as in vet); `vatId` holds the USt-IdNr.

### 4.2 Full per-document schema

```jsonc
{
  "type":      "invoice | quote | null",   // Belegart
  "currency":  "string|null",              // ISO 4217, e.g. EUR
  "number":    "string|null",              // Belegnummer (Rechnungs-/Angebotsnummer)
  "issuedAt":  "string|null",              // Belegdatum, YYYY-MM-DD

  // Belegersteller / Handwerker — the firm that issued the Beleg
  "sender": {
    "companyName": "string|null",
    "address": "string|null", "postcode": "string|null", "city": "string|null", "country": "string|null",
    "contactPhone": "string|null", "contactMail": "string|null",
    "vatId": "string|null"                 // USt-IdNr
  },

  // Dienstleister — service provider (often identical to sender)
  "serviceProvider": {
    "companyName": "string|null",
    "address": "string|null", "postcode": "string|null", "city": "string|null", "country": "string|null",
    "contactPhone": "string|null", "contactMail": "string|null",
    "vatId": "string|null"
  },

  "payment": {
    "iban": "string|null", "bic": "string|null", "bankName": "string|null",
    "dueDate": "string|null"               // YYYY-MM-DD
  },

  // Rechnungsanschrift — to whom the Beleg is addressed
  "recipient": {
    "companyName": "string|null",
    "contactFirstname": "string|null", "contactName": "string|null",
    "street": "string|null", "postcode": "string|null", "city": "string|null", "country": "string|null",
    "contactPhone": "string|null", "contactMail": "string|null"
  },

  // Versicherungsnehmer — policyholder
  "policyholder": {
    "name": "string|null",
    "address": "string|null", "postcode": "string|null", "city": "string|null", "country": "string|null"
  },

  // Schadenort — place of damage (≈ recipient address ~95%; may instead come from the
  // cover email / Betreff line). Emit a warning when it diverges from the invoice address.
  "damageLocation": {
    "address": "string|null", "postcode": "string|null", "city": "string|null", "country": "string|null"
  },

  "items": [
    {
      "position":     "string|null",       // Positionsnummer (running line number per Beleg)
      "name":         "string|null",       // Beschreibung — full position text (may span lines)
      "qty":          "number|null",       // Menge
      "unit":         "string|null",       // raw/canonical unit text, e.g. "Stk", "m²"
      "unitCode":     "integer",           // 0–30 enum (see 4.3); default 0 (PIECE) when none fits
      "unitPriceNet": "number|null",       // E-Preis
      "lineTotalNet": "number|null",       // G-Preis
      "taxRate":      "number|null",       // MwSt % for this line (usually null; tax at totals level)
      "discount":     "number|null",       // Rabatt/Skonto for this line (usually null)
      "source": { "snippet": "string" }    // verbatim source text for traceability
    }
  ],

  "totals": {
    "net":   "number|null",
    "tax":   { "rate": "number|null", "amount": "number|null" },
    "gross": "number|null",
    "discount": "number|null"              // Rabatt/Skonto at document level
  },

  "warnings": ["string"]
}
```

### 4.3 Unit enum (`unitCode`)

From the Erfassungsmaske. `unit` carries the raw/canonical string; `unitCode` carries the mapped integer. **Default to `0` (PIECE / "Stk") when no enum value fits or the unit is blank** (per the form).

| code | name | label | | code | name | label |
|---|---|---|---|---|---|---|
| 0 | PIECE | Stk | | 16 | PERCENT | % |
| 1 | MILLIMETER | mm | | 17 | LITER | l |
| 2 | SQUARE_MILLIMETER | mm² | | 18 | RUNNING_METER | lm |
| 3 | CUBIC_MILLIMETER | mm³ | | 19 | LUMP | pauschal |
| 4 | ZENTIMETER | cm | | 20 | KILOWATT_HOUR | kWh |
| 5 | SQUARE_ZENTIMETER | cm² | | 21 | PAIR | Paar |
| 6 | CUBIC_ZENTIMETER | cm³ | | 22 | TON | t |
| 7 | METER | m | | 23 | AW | AW |
| 8 | SQUARE_METER | m² | | 24 | SET | Satz |
| 9 | CUBIC_METER | m³ | | 25 | STG | Stange |
| 10 | WEEK | Woche | | 26 | GRAM | g |
| 11 | MONTH | Monat | | 27 | PIECE_PER_WEEK | StWo |
| 12 | KILOGRAM | kg | | 28 | OTHER | Sonstige |
| 13 | HOUR | Std | | 29 | KILOWATT_PEAK | Kilowatt Peak |
| 14 | DAY | Tag | | 30 | DEGREE | Grad |
| 15 | KILOMETER | km | | | | |

## 5. Analyze / split override

`products/bps/analyze_overrides.py` adapts the vet analyze prompt:

- **Retermed for BPS:** "Belegprüfung Sach", Belege/Angebot/Rechnung, Handwerker — instead of Tierarzt/Rechnung-only.
- **Animal questions removed** (vet Q5/Q6). No `invoice_animals` in the output.
- **Email/cover-page rule added:** pages that are only a forwarding email / cover message are **not** independent Belege; they attach to the Beleg they accompany (or to none). The email's subject/Betreff may carry Schadenort and the claim reference — usable as context but not a Beleg.
- **Boundary rules kept from vet:** different Beleg numbers ⇒ separate Belege; payment-terminal / privacy-notice pages are not their own Belege; use page images to spot different letterheads/layouts.
- **Output keys (unchanged from vet, for core compatibility):** `pages_with_invoice_information`, `number_of_invoices`, `invoice_pages`, `invoice_number_of_items`.

`ANALYZE_OUTPUT_SCHEMA` documents those four keys (no `invoice_animals`).

## 6. Validation & testing

- **Local prompt iteration (native mode) before any Azure provisioning.** Run the pipeline against all seven `BPS_*.pdf` samples with `STORAGE_BACKEND=local` and tune `extract_prompt.py` / `analyze_overrides.py` until output is correct. Spot-check: multi-Beleg splitting, email-page skipping, Schadenort vs Rechnungsanschrift divergence (e.g. `BPS_2.pdf`: damage at Irene Seeger/Ettlingen, invoice to Volker Steinbach/Leinfelden), unit mapping, totals arithmetic.
- **Unit tests** mirroring `tests/products/vetcostcheck/test_smoke.py`: config loads, `extract_prompt_builder` is callable and produces a long string, schema parses, `build_extract_prompt` accepts and ignores `animal_information`.
- **Deploy + e2e** only once local output looks right: `./deploy.sh bps <tag>` → `scripts/provision_product.sh bps <tag>` → `test_api.py` against `ca-api-bps` (a fresh per-product `INVOICE_API_KEY` is fine here — no existing client contract to preserve, unlike vetcostcheck).

## 7. Open questions / risks

- **Schadenort extraction reliability.** It legitimately lives outside the invoice address in some Belege (in the cover email / Betreff). The prompt should try invoice-address-first, fall back to email/Betreff context, and warn on divergence. Accuracy here is the main thing to watch during local iteration.
- **Dienstleister vs Belegersteller in practice.** Modeled as two parties; samples seen so far have them identical. If iteration shows they're never distinct in real BPS docs, `serviceProvider` can be dropped in a later revision (cheap, additive change).
- **Per-line `taxRate` / `discount`.** Included for completeness; most Belege carry tax/discount only at the totals level. Expected to be `null` on most lines.
- **`BPS_3.pdf` is 7.7 MB** — likely many scanned pages; a good stress test for OCR + splitting.
