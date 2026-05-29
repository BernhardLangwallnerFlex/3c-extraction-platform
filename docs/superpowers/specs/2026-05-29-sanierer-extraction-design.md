# Sanierer Product Onboarding — Design Spec

**Date:** 2026-05-29
**Status:** Draft for review
**Author:** Bernhard + Claude
**Related:** `docs/superpowers/specs/2026-05-29-bps-extraction-design.md` (BPS — the direct template), `docs/superpowers/specs/2026-05-07-multi-product-extraction-design.md` (platform design)

## 1. What Sanierer is

**Sanierer** = extraction of restoration/remediation contractor documents (Schadensanierung — water/fire/mould damage repair) for property-insurance claims. A Sanierer (e.g. *svt Schadensanierung GmbH*) issues a quote (Angebot) or invoice (Rechnung) built from a structured **Leistungsverzeichnis (LV)** — a framework catalog of standardized positions (often tied to an insurer framework agreement, e.g. AXA Rahmenvertrag).

Domain language is German. The extraction target is defined by
`bps_sanierer_input/Sanierer_Input/Erfassungsmaske_Sanierer.docx`, which states:
*"Es reichen Belegpositionen aus, die restlichen Daten erhalten wir direkt aus dem Auftrag. Der Aufbau ist analog BPS mit dem Unterschied, dass es eine LV-Position zusätzlich gibt."*

So Sanierer is **BPS for line items only**, plus a per-item **LV-Position**. Seven sample PDFs live in `bps_sanierer_input/Sanierer_Input/`.

### 1.1 The LV-Position (the one structural addition vs BPS)

In a Sanierer document each billable line carries **two** position numbers:

```
Pos.        Beschreibung                              Menge  ME  Einzelpreis  Gesamtpreis
05.01.001.  Trocknung bis 10m² Grundfläche               1   ST    408,10 €     408,10 €
            05.04.001 Trocknung bis 10m² Grundfläche für Raum-, Wand-, ...   ← LV-Position + full LV text
```

- `position` = the document's own running number for this Beleg (e.g. `05.01.001.`).
- `lvPosition` = the reference into the underlying Leistungsverzeichnis / framework catalog (e.g. `05.04.001`), printed at the start of the detailed description line. It generally **differs** from the document position.

### 1.2 Other structural traits (handled in the prompt, not the schema)

- **Hierarchical Titel grouping:** items sit under non-billable group headers — a Titel (`01. Allgemeines`), a sub-Titel (`01.01. Einrichtung Baustelle`), then leaf positions (`01.01.001.`). Only leaf positions with a price are line items; group headers and `Übertrag`/`Zusammenstellung Titel` lines are not.
- **Percentage positions:** some lines are %-based (Aufwandspauschale, Regiekosten, Rabatt) — `Menge` is a fraction (e.g. `0,010`, `0,130`), `ME` = `%`, and the line may be negative (e.g. `AXA-Rabatt … -2,50 % … -81,80 €`).

## 2. Goal

Onboard `sanierer` as a new product:

- A `products/sanierer/` directory providing a `ProductConfig` (prompt + schema + analyze override), mirroring `products/bps/`.
- Its own Container App pair (`ca-api-sanierer` / `ca-worker-sanierer`), queue (`jobs-sanierer`), image repo (`3cix-sanierer`), and eventually domain (`3csanierer.flex-capital-scale.com`).
- **No changes to `core/`.** `deploy.sh` (already lists `sanierer`), `scripts/provision_product.sh`, and the `Dockerfile` are product-generic.

Out of scope: the custom-domain cutover (batched later with BPS + the pending vetcostcheck Task 16).

## 3. Architecture

Mirrors `products/bps/` exactly — same four files:

```
products/sanierer/
├── __init__.py
├── product.py            # exports CONFIG: ProductConfig (name="sanierer")
├── extract_prompt.py     # build_extract_prompt(*, ocr_text="", subdocument_context=None, expected_items=None)
├── extract_schema.json   # output JSON Schema (documentation/validation target)
└── analyze_overrides.py  # build_analyze_prompt(...) + ANALYZE_OUTPUT_SCHEMA
```

### 3.1 Core-compatibility constraints (identical to BPS)

1. **Analyze output keys are fixed.** The analyze prompt emits `pages_with_invoice_information`, `number_of_invoices`, `invoice_pages`, `invoice_number_of_items` (core/pipeline.py consumes `invoice_pages` and `invoice_number_of_items`). No `subdocument_context` is produced.
2. **Extract builder signature.** `build_extract_prompt(*, ocr_text="", subdocument_context=None, expected_items=None)` — `subdocument_context` is accepted and ignored (Sanierer produces none).

## 4. Output schema (per sub-document)

The pipeline wraps results in `{ "number_of_subdocuments": int, "subdocuments": [ <per-doc object> ] }`. Each per-document object is **items-focused** — it drops the BPS party/header objects (those come from the Auftrag) and adds `lvPosition` to each item.

**vs the BPS per-document schema:**
- **Dropped:** `sender`, `serviceProvider`, `payment`, `recipient`, `policyholder`, `damageLocation`.
- **Kept:** `type`, `currency`, `number`, `issuedAt`, `items[]`, `totals`, `warnings`.
- **Added (item-level):** `lvPosition`.

```jsonc
{
  "type":      "invoice | quote | null",   // Belegart: invoice=Rechnung, quote=Angebot
  "currency":  "string|null",              // ISO 4217, e.g. EUR
  "number":    "string|null",              // Belegnummer (Angebots-/Rechnungsnummer)
  "issuedAt":  "string|null",              // Belegdatum, YYYY-MM-DD

  "items": [
    {
      "position":     "string|null",       // document running position, e.g. "05.01.001."
      "lvPosition":   "string|null",        // LV reference, e.g. "05.04.001" (Sanierer-specific)
      "name":         "string|null",        // Beschreibung — full position text
      "qty":          "number|null",        // Menge (may be fractional for % positions)
      "unit":         "string|null",        // ME raw text, e.g. "M2", "ST", "H", "%"
      "unitCode":     "integer",            // 0–30 enum (see 4.1); default 0 (PIECE)
      "unitPriceNet": "number|null",        // Einzelpreis
      "lineTotalNet": "number|null",        // Gesamtpreis (negative for Rabatt lines)
      "taxRate":      "number|null",        // per-line MwSt % (usually null)
      "discount":     "number|null",        // per-line Rabatt/Skonto (usually null)
      "source": { "snippet": "string" }
    }
  ],

  "totals": {
    "net":   "number|null",                // Nettogesamtpreis
    "tax":   { "rate": "number|null", "amount": "number|null" },  // Umsatzsteuer
    "gross": "number|null",                // Gesamtsumme
    "discount": "number|null"              // document-level Rabatt/Skonto
  },

  "warnings": ["string"]
}
```

### 4.1 Unit enum (`unitCode`)

Same fixed 0–30 enum as BPS (from the shared Erfassungsmaske unit list). `unit` carries the raw ME text; `unitCode` the mapped integer; **default 0 (PIECE / "Stk")** when blank or unmappable. Sanierer commonly uses: `ST`→0, `M2`→8 (m²), `H`→13 (Std/hour), `%`→16, `M`→7, `lfm`/`lm`→18.

| code | label | | code | label | | code | label |
|---|---|---|---|---|---|---|---|
| 0 | Stk | | 11 | Monat | | 22 | t |
| 1 | mm | | 12 | kg | | 23 | AW |
| 2 | mm² | | 13 | Std | | 24 | Satz |
| 3 | mm³ | | 14 | Tag | | 25 | Stange |
| 4 | cm | | 15 | km | | 26 | g |
| 5 | cm² | | 16 | % | | 27 | StWo |
| 6 | cm³ | | 17 | l | | 28 | Sonstige |
| 7 | m | | 18 | lm | | 29 | Kilowatt Peak |
| 8 | m² | | 19 | pauschal | | 30 | Grad |
| 9 | m³ | | 20 | kWh | | | |
| 10 | Woche | | 21 | Paar | | | |

## 5. Extraction prompt (beyond the BPS prompt)

The Sanierer extraction prompt is the BPS prompt adapted for Schadensanierung, with these additions:

1. **Two position numbers per item.** Capture `position` (the document's running Pos., e.g. `05.01.001.`) and `lvPosition` (the LV reference printed at the start of the description detail line, e.g. `05.04.001`). They usually differ; if only one number is present, set `position` and leave `lvPosition` null.
2. **Skip hierarchical group headers.** Titel (`01. Allgemeines`) and sub-Titel (`01.01. …`) rows have no Menge/price and are NOT items. Only leaf positions (`xx.xx.xxx`) with a price are items.
3. **Percentage / surcharge / discount positions.** When `ME` is `%`, set `unit="%"`, `unitCode=16`, `qty` = the fractional value shown (e.g. `0.010`), and capture the resulting `lineTotalNet` (negative for Rabatt). Keep them as items.
4. **Ignore running-total artifacts.** `Übertrag`, `Zusammenstellung Titel`, `Summe …`, `SE Basis`, `Nettogesamtpreis`, `Umsatzsteuer`, `Gesamtsumme` are not items; the final summary block feeds `totals`.
5. **Validation.** Σ `items.lineTotalNet` ≈ `totals.net` (±0.02 tolerance, allowing for Rabatt lines); `totals.net + totals.tax.amount` ≈ `totals.gross`; note discrepancies in `warnings`.

No `subdocument_context` section (Sanierer produces none).

## 6. Analyze / split override

`products/sanierer/analyze_overrides.py` is the BPS analyze override retermed for Schadensanierung (Belege/Angebot/Rechnung, "Schadensanierung" instead of "Belegprüfung Sach"). Output keys unchanged (`pages_with_invoice_information`, `number_of_invoices`, `invoice_pages`, `invoice_number_of_items`); the email/cover-page rule and Beleg-boundary rules carry over. Most Sanierer PDFs are a single multi-page document, but the same 1–N splitting applies.

## 7. Validation & testing

- **Local prompt iteration (native mode) before any Azure provisioning** via `scripts/extract_local.py` against all seven `Sanierer_Input/*.pdf`. Spot-check: `position` vs `lvPosition` separation, Titel headers skipped, %-positions and negative Rabatt lines, unit mapping, Σitems ≈ net and net+tax ≈ gross.
- **Unit tests** mirroring `tests/products/bps/test_smoke.py`: config loads, builders callable, schema parses, `build_extract_prompt` accepts and ignores `subdocument_context`.
- **Deploy + e2e** only once local output looks right: `./deploy.sh sanierer <tag>` → `scripts/provision_product.sh sanierer <tag>` → `test_api.py` against `ca-api-sanierer` (fresh per-product `INVOICE_API_KEY`).

## 8. Open questions / risks

- **LV-Position reliability.** The LV reference is embedded mid-description and visually secondary; the prompt must reliably distinguish it from the document position. Main thing to watch during local iteration.
- **Hierarchical position depth.** Samples show 3-level numbering (`05.01.001.`); if deeper/variant numbering appears, the "leaf = billable" heuristic may need tightening.
- **Items-only scope.** Header fields (Sanierer firm, Schadenort, Schadennummer) are intentionally NOT extracted (from the Auftrag). If experts later want any of them, they are additive — the BPS schema already models them.
- **Mixed Beleg types in samples.** The 7 samples include Angebot (`AN…`, `…Angebot`), Rechnung, Verkaufsrechnung, and others — confirm the `invoice`/`quote` enum covers them, or extend during iteration.
