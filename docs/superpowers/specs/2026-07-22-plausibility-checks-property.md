# Plausibility Checks — new result property (proposal)

**Status:** draft for PM review · 2026-07-22
**First product:** VetCostCheck (most checks carry over to BPS / Sanierer; GOT rules are VCC-only)

## What this is

A new `plausibility` block added to **each subdocument** (= each invoice) in the
extraction result JSON. It is a **raw list of checks**: every check runs
independently and reports its own result. Each check answers one narrow question
about the invoice ("does the IBAN checksum validate?", "does net + tax = gross?").

**Deliberately out of scope for v1:** any scoring, weighting, or "reliability
score". That is a separate, later layer that *reads* this block — and can be
configured per client (different clients may weight or gate differently). This
property is just the honest, explainable evidence; interpretation comes on top.

## Per-check shape

Every check emits the same object:

| field | meaning |
|-------|---------|
| `category` | `arithmetic` · `format` · `domain` · `forensics` · `duplicate` |
| `status` | `pass` · `warn` · `fail` · `skipped` |
| `detail` | human-readable German explanation (drives UI / adjuster view) |
| `evidence` | *(optional)* structured numbers behind the result |

**Status vocabulary**

- `pass` — check ran, no problem.
- `warn` — minor deviation, likely rounding or OCR noise (inside a tolerance band).
- `fail` — clear violation worth a human look.
- `skipped` — check could not run (required field missing, or feature not yet
  enabled). **Not** a failure — this is what keeps sparse invoices from looking
  fraudulent.

> **On OCR vs. the vet:** arithmetic checks are *OCR-fragile* — a single misread
> digit can trip them, so a fail there may implicate our pipeline, not the vet.
> Format/forensics checks are *OCR-robust*. The scoring layer (later) can weight
> accordingly; this raw layer just records what happened.

## Mock — populated example (one VCC subdocument)

```json
{
  "plausibility": {
    "version": "0.1",
    "checks": {
      "line_item_math": {
        "category": "arithmetic",
        "status": "pass",
        "detail": "Alle 3 Positionen: Menge × Einzelpreis = Positionssumme (±0,02 €).",
        "evidence": { "linesChecked": 3, "linesFailed": 0, "maxDeltaEur": 0.0 }
      },
      "items_sum_to_net": {
        "category": "arithmetic",
        "status": "warn",
        "detail": "Summe der Positionen 134,50 € weicht um 0,05 € vom Netto 134,45 € ab (Rundung).",
        "evidence": { "sumLineTotalsEur": 134.5, "statedNetEur": 134.45, "deltaEur": 0.05 }
      },
      "tax_consistent": {
        "category": "arithmetic",
        "status": "pass",
        "detail": "Netto 134,45 € × 19 % = 25,55 € entspricht ausgewiesener MwSt.",
        "evidence": { "expectedTaxEur": 25.55, "statedTaxEur": 25.55, "deltaEur": 0.0 }
      },
      "gross_consistent": {
        "category": "arithmetic",
        "status": "pass",
        "detail": "Netto 134,45 € + MwSt 25,55 € = Brutto 160,00 €.",
        "evidence": { "expectedGrossEur": 160.0, "statedGrossEur": 160.0, "deltaEur": 0.0 }
      },
      "iban_valid": {
        "category": "format",
        "status": "pass",
        "detail": "IBAN-Prüfsumme (ISO 13616) korrekt.",
        "evidence": { "iban": "DE89 3704 0044 0532 0130 00", "countryChecked": "DE" }
      },
      "bic_valid": {
        "category": "format",
        "status": "pass",
        "detail": "BIC-Format gültig.",
        "evidence": { "bic": "COBADEFFXXX" }
      },
      "vat_id_valid": {
        "category": "format",
        "status": "skipped",
        "detail": "Keine USt-IdNr. auf der Rechnung gefunden.",
        "evidence": null
      },
      "vat_rate_plausible": {
        "category": "format",
        "status": "pass",
        "detail": "Steuersatz 19 % ist in Deutschland zulässig.",
        "evidence": { "rate": 19, "allowed": [19, 7, 0] }
      },
      "dates_plausible": {
        "category": "format",
        "status": "pass",
        "detail": "Rechnungsdatum 10.07.2026 liegt nicht in der Zukunft; Leistungsdatum 08.07.2026 davor.",
        "evidence": { "issuedAt": "2026-07-10", "earliestServiceDate": "2026-07-08" }
      },
      "got_code_known": {
        "category": "domain",
        "status": "pass",
        "detail": "Alle 3 GOT-Ziffern im Katalog gefunden.",
        "evidence": { "codesChecked": ["1", "22", "504"], "unknownCodes": [] }
      },
      "got_factor_in_range": {
        "category": "domain",
        "status": "fail",
        "detail": "Position 'Injektion s.c.' mit Faktor 4,5× überschreitet die Regelspanne (max. 3×, bis 4× nur mit Begründung).",
        "evidence": { "worstPosition": "Injektion s.c.", "code": "22", "multiplier": 4.5, "maxAllowed": 3.0 }
      },
      "pdf_incremental_updates": {
        "category": "forensics",
        "status": "pass",
        "detail": "Keine nachträglichen inkrementellen Änderungen im PDF gefunden. (Experimentell — Producer/Datum dienen nur als Kontext, kein eigener Status.)",
        "evidence": {
          "incrementalUpdates": 0,
          "producer": "ReportLab PDF Library",
          "creatorTool": null,
          "creationDate": "2026-07-10T09:14:00Z",
          "modDate": "2026-07-10T09:14:00Z"
        }
      },
      "duplicate_invoice": {
        "category": "duplicate",
        "status": "skipped",
        "detail": "Dublettenabgleich noch nicht aktiviert (spätere Phase).",
        "evidence": null
      }
    },
    "summary": { "pass": 9, "warn": 1, "fail": 1, "skipped": 2, "total": 13 }
  }
}
```

> `summary` is a **raw tally only** — a count of statuses, not a score. It exists
> so the UI can show "1 failed, 2 warnings" at a glance. No weighting is applied.

## Check inventory

| id | category | products | OCR-sensitivity | phase |
|----|----------|----------|-----------------|-------|
| `line_item_math` | arithmetic | all | fragile | 1 |
| `items_sum_to_net` | arithmetic | all | fragile | 1 |
| `tax_consistent` | arithmetic | all | fragile | 1 |
| `gross_consistent` | arithmetic | all | fragile | 1 |
| `iban_valid` | format | all | robust | 1 |
| `bic_valid` | format | all | robust | 1 |
| `vat_id_valid` | format | all | robust | 1 |
| `vat_rate_plausible` | format | all | robust | 1 |
| `dates_plausible` | format | all | robust | 1 |
| `got_code_known` | domain | VCC | robust | 2 (needs GOT catalogue) |
| `got_factor_in_range` | domain | VCC | robust | 2 (needs GOT catalogue) |
| `pdf_incremental_updates` | forensics | all | robust | 3 (experimental) |
| `duplicate_invoice` | duplicate | all | robust | 4 (needs persistence) |

> **13 checks.** `pdf_producer_plausible` and `pdf_dates_consistent` were dropped as
> checks after a probe of 44 real invoices showed they false-positive on legitimate
> documents (producers are dozens of legit backends incl. office/browser print; date
> gaps are normal for OCR/ERP). Their raw values are kept only as `evidence`.

**Phases** are a suggested build order, not commitments:
1. arithmetic + format — cheapest, arithmetic is already designed (ported from the
   client-side sanity-check spec)
2. GOT domain checks — gated on building the GOT catalogue (next work item)
3. PDF metadata forensics — **experimental / low-weight**; weak on real data, and
   needs manipulated samples before it can be trusted
4. duplicate detection — needs a fingerprint store (content hash + PDF DocumentID);
   heaviest, clearly later
