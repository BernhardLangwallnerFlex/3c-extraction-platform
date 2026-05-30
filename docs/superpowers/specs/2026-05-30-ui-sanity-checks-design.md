# UI Sanity Checks — Design & Handover Spec

**Date:** 2026-05-30
**Audience:** the session integrating this into the testing UI (VetCostCheck / BPS / Sanierer)
**Scope:** UI-only. No API changes. The UI computes everything client-side from the JSON it already receives.

---

## 1. Purpose

The testing UI displays extraction results for three products. We want a small set of
**arithmetic sanity checks** rendered alongside each result so a human can spot a bad
extraction at a glance — e.g. line items that don't add up to the total, a VAT amount
that doesn't match the rate, a gross that isn't net + tax.

These checks do **not** validate the extraction against the source PDF. They only check
**internal numeric consistency** of the JSON the API returns. A green result means "the
numbers are self-consistent", not "the extraction is correct".

---

## 2. The deliverable: one pure function

Implement a single pure function:

```ts
evaluate(apiResult: ApiResult): SanityReport
```

- **Input** — the `result` object from `GET /job/{job_id}` (see §3).
- **Output** — a normalized `SanityReport` (see §6) that the render layer maps directly to UI.
- **Pure** — no network, no I/O, no API changes. All arithmetic lives here; the rendering
  layer holds zero business logic. This makes the function trivially unit-testable against
  saved sample JSON (see §9).
- **Product-agnostic** — the same function handles all three products. They share the
  `items[]` + `totals{}` shape this spec relies on. Product-specific quirks are handled by
  the rules below (they are tolerated, not special-cased per product).

---

## 3. Input shape (what the UI receives)

`GET /job/{job_id}` returns:

```jsonc
{
  "job_id": "…",
  "status": "finished",          // only evaluate when status === "finished"
  "result": {
    "number_of_subdocuments": 3,
    "subdocuments": [ /* one object per sub-document */ ]
  }
}
```

`evaluate()` takes the `result` object. Each entry in `subdocuments[]` has this
(arithmetic-relevant) shape, identical across products:

```jsonc
{
  "type": "invoice",
  "currency": "EUR",
  "number": "…",
  "issuedAt": "2026-05-01",
  "items": [
    {
      "qty": 12.0,
      "unitPriceNet": 4.50,
      "lineTotalNet": 54.00,
      "taxRate": null,            // per-line VAT %, usually null
      "discount": null,           // per-line discount, usually null
      "name": "…"
      // vetcostcheck items also carry got / animal — IGNORE for checks
      // bps/sanierer items also carry position / lvPosition / unitCode — IGNORE for checks
    }
  ],
  "totals": {
    "net": 3190.12,
    "tax": { "rate": 19.0, "amount": 606.12 },
    "gross": 3796.24,
    "discount": null
  },
  "warnings": [ "…" ]            // model-emitted notes; display separately, not a check
}
```

**Defensive parsing:** any field may be missing or `null`. Treat a missing key the same as
`null`. `items` may be empty or absent. Never throw — a missing input produces a `skipped`
check, never a crash.

---

## 4. The four checks

Each check runs **per subdocument** and yields a verdict: `pass | warn | fail | skipped`.
`skipped` means a required input was `null`/missing — it is **grey (➖), never a fail**.

A numeric comparison classifies the absolute delta `Δ = |computed − reported|` against the
reference `ref = |reported|` using the tolerance band in §5.

### Check 1 — Line-item math (per item)
`qty × unitPriceNet ≈ lineTotalNet`

- Skip a line if `qty`, `unitPriceNet`, or `lineTotalNet` is null. Reference `ref = |lineTotalNet|`.
- If the line has a non-null `discount`, also accept either discount interpretation and pass
  if **any** lands in tolerance:
  - `qty × unitPriceNet − discount` (discount as absolute amount)
  - `qty × unitPriceNet × (1 − discount / 100)` (discount as percentage)
- Negative `lineTotalNet` (Sanierer *Rabatt* lines) is valid — the signed product should still match.
- **Check verdict** = the worst verdict across non-skipped lines.
- **Display annotation**: `"M/N ok"` where N = lines with all three inputs present, M = lines that passed.
- If every line is skipped → check is `skipped`.

### Check 2 — Items sum to net
`Σ lineTotalNet ≈ totals.net`

- Sum over lines with non-null `lineTotalNet`. Reference `ref = |totals.net|`.
- **Discount ambiguity:** if the direct compare is not a pass **and** `totals.discount` is
  non-null, also test `Σ − totals.discount`. Pass if either interpretation fits.
- **Incomplete sum:** if at least one line has a null `lineTotalNet`, the sum is unreliable —
  annotate `"computed from M/N lines"` and **cap the best achievable verdict at `warn`**
  (never green when lines are missing).
- Skip if `totals.net` is null, or no line has a numeric `lineTotalNet`.

### Check 3 — Tax check
`totals.net × (rate / 100) ≈ totals.tax.amount`

- `rate = totals.tax.rate`. Reference `ref = |totals.tax.amount|`.
- Skip if `totals.net`, `totals.tax.rate`, or `totals.tax.amount` is null.
- **Mixed VAT:** if any `item.taxRate` is non-null and differs from `totals.tax.rate`, a single
  blended-rate check can't hold — **downgrade a `fail` to `warn`** and annotate `"mixed rates"`.
  (A `pass` stays a `pass`.)

### Check 4 — Gross check
`totals.net + totals.tax.amount ≈ totals.gross`

- Reference `ref = |totals.gross|`.
- **Discount:** if the direct compare is not a pass **and** `totals.discount` is non-null, also
  test `totals.net − totals.discount + totals.tax.amount`. Pass if either fits.
- Skip if `totals.net`, `totals.tax.amount`, or `totals.gross` is null.

> **Design note — the "try-then-tolerate" pattern.** Checks 1, 2, and 4 each try a primary
> formula and, only if it doesn't already pass, fall back to a discount-adjusted
> interpretation. This absorbs known accounting ambiguities (discount as a separate total vs.
> baked into line totals; absolute vs. percentage discounts) without going noisy. A genuinely
> wrong number fails *every* interpretation and still surfaces as `fail`.

---

## 5. Tolerance band

A combined absolute-OR-relative band, so both small and large invoices behave sensibly:

```
pass   Δ ≤ max(0.02, 0.001 × ref)      // €0.02 or 0.1%
warn   Δ ≤ max(1.00, 0.010 × ref)      // €1.00 or 1.0%
fail   otherwise
```

- `ref` = absolute value of the **reported** quantity the computed value is checked against
  (per check, as named in §4).
- When `ref` is `0` or near-zero, the relative term vanishes and the absolute floor applies
  (pass ≤ €0.02, warn ≤ €1.00).
- Put these three numbers (`0.02`, `1.00`, `0.001`, `0.010`) in **one config constant** so they
  are tunable in a single place.

Currency normalization: all amounts are assumed to be in the subdocument's `currency`. Checks
do **not** convert currencies; they assume one currency per subdocument (true in practice).

---

## 6. Output: the `SanityReport` schema

```ts
type Verdict = "pass" | "warn" | "fail" | "skipped";

interface CheckResult {
  id: "lineItems" | "itemsSumNet" | "tax" | "gross";
  label: string;            // e.g. "Line-item math"
  verdict: Verdict;
  computed: number | null;  // null when skipped
  reported: number | null;  // null when skipped
  delta: number | null;     // computed − reported (signed), null when skipped
  note?: string;            // e.g. "17/17 ok", "mixed rates", "computed from 15/17 lines",
                            //      "skipped: totals.net missing"
}

interface SubdocReport {
  index: number;            // position in subdocuments[]
  number: string | null;    // subdoc "number" (Belegnummer), for labeling
  currency: string | null;
  verdict: Verdict;         // worst-of its four checks (see §7)
  checks: CheckResult[];    // always 4 entries, in the order above
}

interface SanityReport {
  verdict: Verdict;         // worst-of all subdocs (the top rollup badge)
  subdocCount: number;
  subdocs: SubdocReport[];
}
```

The render layer reads only this object. It never re-touches the raw API JSON for math.

---

## 7. Rollup rule

Severity order: **`fail` > `warn` > `pass` > `skipped`**.

- A subdoc's `verdict` = the most severe verdict among its four checks, **ignoring `skipped`**.
- The job-level `verdict` = the most severe subdoc verdict.
- If *every* relevant check is `skipped` (no numbers anywhere), the verdict is `skipped`.

---

## 8. Suggested presentation

Per the agreed mockups (illustrative — final styling is the UI session's call):

```
Job   ❌ 1 issue across 3 docs
──────────────────────────────
Doc 1 (R-2026-014)   ✅ all ok
Doc 2 (R-2026-015)   ❌ gross off €120.00
Doc 3 (R-2026-016)   ⚠️ tax off €0.40
```

Expanded per-subdoc panel (exactly the four checks of §4):

```
✅ Line-item math      17/17 ok
✅ Items → net         Δ €0.01
⚠️ Tax check           off by €0.40   (mixed rates)
❌ Gross check         off by €120.00
```

A `skipped` check renders the same way with its note, e.g.
`➖ Tax check   skipped: totals.net missing`.

Rendering guidance:
- Icons: `pass ✅` · `warn ⚠️` · `fail ❌` · `skipped ➖`.
- Show the signed delta and, on hover/expand, `computed` vs `reported`.
- Render the subdoc's own `warnings[]` array near the checks but visually distinct — those are
  model notes, **not** sanity-check output.
- The top rollup badge should be always visible; per-subdoc detail can be collapsible.

---

## 9. Testing the function

`evaluate()` is pure, so test it with saved JSON fixtures — no UI needed:

- A clean invoice → all `pass`.
- An invoice with a deliberately broken `gross` → Gross check `fail`, others `pass`, rollup `fail`.
- A subdoc with `totals.net = null` → Tax/Items/Gross `skipped` with explanatory notes.
- A Sanierer doc with a negative *Rabatt* line → Line-item math still `pass`.
- A multi-rate invoice (`item.taxRate` varies) → Tax check `warn` (not `fail`) with `"mixed rates"`.
- A multi-subdoc result where one subdoc fails → rollup `fail`, others reported independently.

Sample real outputs already exist in `temp/extracted_data_*.json` and `3C_testdaten_json/`
to seed fixtures.

---

## 10. Edge cases & assumptions (read before implementing)

1. **Only evaluate `status === "finished"`.** For other statuses, render nothing (or a spinner).
2. **Empty / single subdoc.** `number_of_subdocuments` may be 0 or 1; handle both. Rollup of an
   empty result is `skipped`.
3. **Floating-point.** Use the §5 band, never `===` on floats. Don't pre-round inputs.
4. **`null` vs `0`.** `0` is a real value and participates in math; `null`/missing → `skipped`.
   Distinguish them carefully (`x == null` catches both null and undefined in JS).
5. **Negative values are legal** (discount/Rabatt lines, credit notes). Use signed arithmetic;
   take absolute value only for `ref` and `Δ`.
6. **`warnings[]` is not a check.** Display it, but it does not affect any verdict.
7. **Discount field semantics are genuinely ambiguous** in the data; the try-then-tolerate rule
   (§4 note) is the agreed handling. If a real document surfaces a third interpretation, extend
   the fallback list in one place rather than special-casing a product.
8. **No cross-subdoc checks** (e.g. summing all subdocs to a PDF grand total). Out of scope —
   each subdoc is an independent Beleg.

---

## 11. Out of scope (explicitly not in v1)

- Validation against the source PDF / OCR text.
- Field-presence/quality checks beyond arithmetic (e.g. "is `issuedAt` a plausible date").
- Currency conversion or multi-currency reconciliation.
- Persisting or exporting check results.
- Any API/backend change.
