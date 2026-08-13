# Subdocument returncode — design

**Date:** 2026-08-12
**Products:** vetcostcheck, bps, sanierer (all three)
**Origin:** PO request (BPS/Sanierer) — documents containing no Beleg still produce a
result that looks extractable, so 3C opens a Vorgang for nothing.

## Problem

3C bundles everything received from a customer into one Sammeldokument and submits it
without knowing whether a Rechnung/KVA is in it. When there is none, the API returns a
well-formed result full of nulls. The consumer cannot distinguish that from a real Beleg
and opens a Vorgang anyway.

The reference case (`bps_sanierer_input/null_example_pdf.pdf`, 4 pages) is a germanBroker
cover email plus Schadenmeldung (p1–2), a photo (p3), and a notice that the actual
craftsman invoices failed to attach (p4). There is genuinely no Beleg in it.

Two distinct defects produce the observed output:

1. **The splitter invents Belege.** `products/bps/analyze_overrides.py` already states that
   cover emails and Anschreiben are not independent Belege, yet analysis returned
   `number_of_invoices: 2` and the pipeline built two subdocuments from the same cover
   email. Root cause: Frage 2 of the analyze prompt asks how many Belege the document
   contains but never states that **0 is a legal answer**. The analyze output schema permits
   `minimum: 0`, but the model never sees the schema.

2. **There is no machine-readable classification.** The only signal is free-text `warnings`,
   which the consumer cannot branch on.

The PO's proposed fallback — treating `type == null` as "not a Beleg" — must not be used.
Rule 7 of the extraction prompt (`products/bps/extract_prompt.py`) instructs the model to
emit `type: null` whenever the Belegart is unclear, so genuine but ambiguous Belege also
land on null.

## Contract

Every subdocument gains two fields, placed first in the object:

```json
{
  "returncode": 200,
  "returncodeReasons": [
    "Dokument enthält nur ein Anschreiben/E-Mail von germanBroker.net.",
    "Die erwähnten Rechnungen (Kömpf 24 GmbH, Enkel & Partner GbR) sind nicht beigefügt.",
    "Seite 4 meldet fehlgeschlagene Dateianhänge."
  ],
  "type": null
}
```

| Code | Meaning | Condition |
|------|---------|-----------|
| 100 | Rechnungs-/KVA-Dokument | It is a Rechnung or Angebot/KVA — **even if individual fields could not be extracted** |
| 200 | Kein Rechnungs-/KVA-Dokument | Readable, but an Anschreiben, E-Mail, Foto, Datenschutzhinweis, Anlagenverzeichnis, … |
| 300 | Nicht lesbar | Content could not be read at all |

Guarantees:

- `returncode` is **always present** and is **always** one of 100, 200, 300.
- `returncodeReasons` is an array of German strings: empty for 100, non-empty for 200/300.
- 300 outranks 200 when both could apply — an unreadable page cannot be classified.
- `warnings` keeps its current meaning (extraction caveats such as tax-sum mismatches or
  OCR disagreements) and stays separate, so `returncodeReasons` is safe to paste into a
  Storno note.
- The response envelope is unchanged: `{number_of_subdocuments, subdocuments[]}`. There is
  no envelope-level returncode.

## Architecture

Three layers. The LLM supplies judgment; core guarantees the contract; the analyzer stops
fabricating Belege.

### Layer 1 — LLM classification (per product)

`products/{vetcostcheck,bps,sanierer}/extract_prompt.py`: one new extraction rule plus the
two fields in the inline JSON target schema.

The rule must carry this load-bearing sentence: *ein Beleg bleibt 100, auch wenn einzelne
Felder nicht ermittelbar sind.* Without it the model reaches for 200 whenever extraction
goes badly — the expensive failure direction, because a false 200 auto-cancels a legitimate
claim.

Wording is product-appropriate (what counts as 200 differs between a Handwerkerbeleg and a
Tierarztrechnung) but the numeric codes and their meanings are identical across products.

`products/{vetcostcheck,bps,sanierer}/extract_schema.json`: add both properties. These feed
the Swagger docs via `custom_openapi()` in `core/api/main.py`.

### Layer 2 — deterministic floor (core, unconditional)

`core/pipeline.py:_extract_single_subdocument()` applies the floor **after**
`product_config.postprocess_extraction` runs. Core owns it because all three products need
identical behaviour, and because VCC already occupies the `postprocess_extraction` hook with
its qty/unit coercion (`products/vetcostcheck/postprocess.py`), which stays untouched.

The floor is **fill-in-only and never overrides a valid LLM value**:

- `returncode` already 100, 200, or 300 (as an int) → keep it.
- Missing, `null`, a string such as `"100"`, or any other value → derive it:
  any of a non-null `type`, `number`, `issuedAt`, a non-empty `items`, `totals.net`, or
  `totals.gross` present → 100; otherwise → 200.
  A non-null `type` counts because a model that positively named the Belegart has classified
  the document. This is the mirror image of the rejected "`type == null` means not a Beleg"
  heuristic, not the same thing — absence stays meaningless, presence does not. Adding a
  field to this list is monotonic: it can only turn a derived 200 into a 100, never the
  reverse, so it cannot make the expensive error more likely.
- **Never derives 300.** Distinguishing unreadable from not-a-Beleg requires the model's
  view of the page; deterministically the two are indistinguishable.
- `returncodeReasons` is coerced to a list of strings (non-strings dropped, non-list
  replaced). If the code is 200 or 300 and the list is empty, a generic German reason is
  inserted.

Rationale for fill-in-only: an automatic 100 → 200 downgrade on a real invoice that merely
extracted badly would cause a wrongful Storno. Leaving a wrong 100 in place reproduces
today's behaviour, which a human already handles.

The derivation field names (`type`, `number`, `issuedAt`, `items`, `totals`) exist in all
three product schemas, so no per-product configuration is needed.

### Layer 3 — splitter fixes (core + per product)

**3a. Teach the analyzer that zero is legal.** All three
`products/*/analyze_overrides.py` share the identical gap — Frage 2 asks how many
Belege/Rechnungen the document contains, while only the unseen output schema permits
`minimum: 0`. In each of them: state in Frage 2 that `number_of_invoices: 0` with
`invoice_pages: {}` is a valid answer when the document contains no Beleg (only Anschreiben,
E-Mails, Fotos, …), and add *Erfinde keine Belege.* This is the actual fix for the two
invented subdocuments.

**3b. Guarantee at least one subdocument.** In
`core/pipeline.py:split_document_into_invoices()`: when `invoice_pages` is missing or empty,
emit one subdocument spanning all pages. Extraction then runs on it and classifies it 200.

3a and 3b must ship together. 3a makes zero-Beleg analyses *more* likely, so shipping it
alone would hand consumers a newly-common empty array. 3b is applied unconditionally to all
three products, giving one uniform contract.

**3c. Preserve evidence for vacuous jobs.** `cleanup_storage_artifacts()` currently skips
cleanup when there are zero subdocuments, precisely to keep vacuous extractions
reproducible. 3b makes that guard unreachable. Extend it to also skip cleanup when **no**
subdocument came back 100, so the cases this spec is about stay investigable. The retained
blobs expire under the container's existing 14-day lifecycle rule.

## Consumer impact

- **Added fields** are backwards-compatible for all three products.
- **3b is a visible behaviour change for VCC**, which is live on the cut-over domain: a
  result that today is `{"number_of_subdocuments": 0, "subdocuments": []}` becomes
  `{"number_of_subdocuments": 1, "subdocuments": [{"returncode": 200, …}]}`. The VCC
  consumer needs a heads-up before this ships.
- No HTTP-level change. The job genuinely succeeded; failing it would discard the very
  reasons the PO asked for.

## Testing

**Unit — deterministic floor** (new test module under `tests/core/`):
- Valid 100 / 200 / 300 from the LLM is preserved unchanged.
- Missing, `null`, `"100"`, `0`, and `999` each fall through to derivation.
- Derivation yields 100 when any of `number` / `issuedAt` / non-empty `items` /
  `totals.net` / `totals.gross` is present, and 200 when none is.
- The floor never emits 300.
- `returncodeReasons` coercion: non-list replaced, non-string entries dropped, generic
  German reason injected for a 200/300 with no reasons.

**Unit — split fallback** (extend `tests/core/`):
- Empty `invoice_pages` yields exactly one subdocument covering all pages.
- Non-empty `invoice_pages` is unaffected.

**Unit — composition:** `tests/products/vetcostcheck/test_postprocess.py` must still pass,
proving the core floor composes with VCC's existing hook.

**Acceptance:** `bps_sanierer_input/null_example_pdf.pdf` through `PRODUCT_NAME=bps` returns
`number_of_subdocuments: 1` and `returncode: 200` with non-empty reasons.

**Regression (the one that matters):** every real Beleg must come back 100. A false 200
auto-cancels a legitimate claim.

> `bps_sanierer_input/` is gitignored — it lives on the maintainer's machine, not in the
> repo. `3C_testdaten_pdf/` is committed.

- `bps_sanierer_input/BPS_Input/BPS_{1..7}.pdf` under `PRODUCT_NAME=bps`
- the 7 PDFs in `bps_sanierer_input/Sanierer_Input/` under `PRODUCT_NAME=sanierer`
- the 15 PDFs at the top level of `3C_testdaten_pdf/` under `PRODUCT_NAME=vetcostcheck`
  (the `Original_Testdaten/` subdirectory is not part of the regression set)

## Documentation

- `vetcostcheck_api_doc.md` publishes an example result to the VCC consumer — add both
  fields and document the three codes.
- BPS and Sanierer have no equivalent document; they are covered by Swagger via their
  `extract_schema.json`.

## Out of scope

- An envelope-level returncode. The per-subdocument field plus the ≥1 guarantee means one
  field in one place is always readable.
- Structured `{code, text}` reason objects. Free German text is what goes into a Storno; a
  sub-code enum would need maintaining and would constrain what the model can report.
- HTTP error responses for zero-Beleg documents.
- Any change to VCC's qty/unit postprocessing.
