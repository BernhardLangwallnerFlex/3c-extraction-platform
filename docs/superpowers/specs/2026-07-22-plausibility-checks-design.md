# Plausibility / Fraud-Signal Checks — Design Spec

**Status:** draft · 2026-07-22
**First product:** VetCostCheck. Most checks are product-agnostic and carry over to BPS / Sanierer; GOT domain checks are VCC-only.
**Companion doc:** `2026-07-22-plausibility-checks-property.md` (lighter, PM-facing excerpt of the output contract).

---

## 1. Goal & non-goals

**Goal.** Add a `plausibility` block to each extracted invoice (subdocument) in the
result JSON: a **raw list of independent checks**, each reporting a structured,
explainable result. This lets a customer (insurer) see *what* is off with an
invoice — arithmetic that doesn't add up, an invalid IBAN, a GOT factor outside
the legal range, PDF-metadata that suggests editing — with a human-readable reason
for each.

**Non-goals (v1), deliberately deferred:**
- **No scoring / weighting / "reliability score."** That is a separate layer that
  *reads* this block and can be **configured per client** (different gating,
  different weights). Keeping it out avoids blocking the mechanical work on
  agreement we don't have yet.
- **No pixel-level forensics** (image-manipulation / AI-generation detection).
  Unreliable on real scans, high false-positive rate, and legally indefensible as
  a black-box flag. Explicitly out of scope.
- **No auto-rejection.** The block is evidence; how it's used is the customer's
  and the scoring layer's decision.

**Design principles.**
- **Checks never break extraction.** Any check that errors or lacks inputs yields
  `skipped`, never an exception. The plausibility block is strictly additive.
- **Separate "suspicious invoice" from "noisy extraction."** Arithmetic checks are
  OCR-fragile; a fail there may implicate our pipeline, not the vet. Each check
  carries an OCR-sensitivity classification so the later scoring layer can weight
  accordingly. This raw layer only records what happened.
- **Product-agnostic core, per-product enablement.** One evaluator; a per-product
  list selects which checks run.

### 1.1 Process context: Erfassung vs Prüfung

3C's workflow has two distinct steps, and our checks split across them by **the
question they answer**:

- **Erfassung** (*this project*): "Did we capture the document faithfully?" — signals
  about **our extraction quality**, plus identity **enrichment**. Output flows through
  a **human validator** who confirms/corrects it.
- **Prüfung** (*the next step — manual today, soon a separate agentic AI step*): "Is
  the invoice correct, legitimate, compliant?" — content judgments that **assume a
  trusted, human-verified capture**.

```
ERFASSUNG (this project)                      ┌─ human validator ─┐        PRÜFUNG (next step)
extract → capture-quality checks   ─────────► │ confirms/corrects │ ─────► content validation
        + verifiedProvider (Places)           │ the verified data │        on TRUSTED input
                                              └───────────────────┘
```

Consequences that shape this spec:
- **Prüfung trusts its input** because a human signed off — so the Prüfung checks may
  physically live in the *separate* step/codebase, not here. Ownership (which step is
  responsible) is distinct from execution locus (where code runs).
- **Erfassung enrichments must be human-confirmable** — hence `verifiedProvider`
  carries provenance/`matchSource` (§3.1).
- **Capture-quality checks double as the human validator's worklist** — a failed IBAN
  checksum, a low arithmetic-confidence subdocument, or an `unresolved` Places match
  is precisely what routes a document to human attention.
- **Places runs in Erfassung**, so the human can vet `verifiedProvider` before the
  document is passed on. Because a human backstops it, the matcher (§4.6) needn't be
  perfect — ambiguous cases fall through as `unresolved`.
- **Arithmetic is built in both steps** — capture-confidence here (soft, routes to
  human), numerical validity in Prüfung (rounding-aware; §4.1). Same recompute, two
  tolerances, two meanings.

The `Step` column in the §4 inventory records each check's **ownership**.

---

## 2. Architecture & placement

### 2.1 Where it runs

The three products share the numeric core (`items[].{qty, unitPriceNet,
lineTotalNet}` + `totals{net, tax{rate,amount}, gross, discount}`), so a single
evaluator is product-agnostic.

- **Per-subdocument checks** run in a new evaluator invoked from
  `core/pipeline.py:_extract_single_subdocument()` — right after the existing
  `product_config.postprocess_extraction(result)` call (`pipeline.py:319-320`).
  The evaluator attaches `result["plausibility"]`.
- **Document-level inputs** (PDF metadata for forensics) are captured **once, early**
  in `Pipeline.__init__` from the *original* uploaded bytes — **before**
  `_fix_page_orientation` rewrites the file (`pipeline.py:97-147`), which would
  strip/alter metadata. Captured metadata is threaded into the evaluator context
  per subdocument.
- **Cross-subdocument rollup** (the `summary` tally) is computed in
  `extract_data_from_subdocuments()` after the subdocument loop (`pipeline.py:341`).

### 2.2 Check registry & context

Each check is a **pure function** with a uniform signature:

```
CheckFn = Callable[[CheckContext], CheckResult]
```

`CheckContext` carries everything a check might need, so no check reaches into
pipeline internals:

```
CheckContext:
    subdoc: dict            # the extracted invoice (post postprocess_extraction)
    pdf_metadata: dict|None # captured early from original bytes (forensics)
    catalogue: GotCatalogue|None  # loaded GOT reference (domain checks)
    product: str            # "vetcostcheck" | "bps" | "sanierer"
    invoice_date: date|None # resolved from issuedAt/serviceDates, for catalogue versioning
```

A **registry** maps `check_id -> CheckFn`. Enablement is per-product:

- Extend `core/product.py:ProductConfig` with `enabled_checks: list[str]`
  (default = all product-agnostic checks). VCC additionally enables
  `got_code_known`, `got_factor_in_range`.
- The evaluator runs each enabled check inside a `try/except` that converts any
  exception to `status="skipped"` with a diagnostic `detail`, and logs via
  structlog (consistent with existing resilience patterns).

This keeps each check independently testable and the wiring declarative.

### 2.3 External (identity) checks — a special family

Every check above is **pure, offline, free, instant**. `sender_business_verified`
(§4.6) is different: it makes a **paid external call** (Google Places) and egresses
the sender's name/address. It needs a distinct execution model:

- **Separate stage**, not the pure `postprocess_extraction` hook. Runs with a **hard
  timeout**; any error/timeout → `status="skipped"`, never blocking the job.
- **Cache** via the **Postgres provider registry** (§4.6.1) — not Redis: the cache is a
  durable, human-curated asset, not ephemeral. Vets recur (and the German population is
  finite, ~11k), so hit rate trends to ~100% and most invoices cost zero API calls. The
  experiment (Appendix B) additionally caches raw responses on disk for offline replay.
- **Feature-flag per product/client** (`enabled_checks`), because it costs money and
  is **gated on data-governance sign-off** (§9).
- **Optional LLM tiebreak** (§4.6) is itself cached and only fires on the ambiguous
  middle band — deterministic matching handles the clear cases for free.

The result still lands in `plausibility.checks[sender_business_verified]` for
uniformity; the richer canonical match is surfaced separately as `verifiedProvider`
(§3).

---

## 3. Output contract

Attached per subdocument as `result["plausibility"]`:

```json
{
  "version": "0.1",
  "checks": {
    "<check_id>": {
      "category": "arithmetic | format | domain | forensics | duplicate | identity",
      "status":   "pass | warn | fail | skipped",
      "detail":   "human-readable German explanation",
      "evidence": { }   // optional, structured numbers/values behind the result
    }
  },
  "summary": { "pass": 0, "warn": 0, "fail": 0, "skipped": 0, "total": 0 }
}
```

### 3.1 `verifiedProvider` — canonical-provider enrichment (separate property)

The `sender_business_verified` check yields a pass/warn *status*. Separately, when a
confident match is found, we surface the **canonical business record** as its own
subdocument-level property (sibling of `plausibility`). This is enrichment, not a
check: it corrects our OCR (canonical name/address), gives a stable `placeId` (also
useful for `duplicate_invoice`), and lets a client show a "Google-verified provider"
badge. It runs in **Erfassung** and is **human-confirmable** before the document
passes to Prüfung (§1.1) — hence the `matchSource`/`reviewedBy` provenance fields.

```json
"verifiedProvider": {
  "verified": true,
  "source": "google_places",
  "matchSource": "auto_high_confidence",   // provenance — see states below
  "reviewedBy": null,                       // set to validator id/name once a human touches it
  "placeId": "ChIJ…",
  "name": "Tierarztpraxis Klink & Dühnen",
  "formattedAddress": "…",
  "location": { "lat": 0.0, "lng": 0.0 },
  "businessStatus": "OPERATIONAL",
  "isVeterinary": true,
  "phone": "…",
  "rating": 4.6,
  "userRatingCount": 427,
  "matchConfidence": 0.92,          // from the matcher (§4.6)
  "nameMismatch": true,             // extracted name differs materially from canonical
  "extractedName": "Klinik & Dünen Tierärztliche Praxis"
}
```

**`matchSource` states** (six):
- `auto_high_confidence` — deterministic matcher was confident (§4.6 tier 1)
- `auto_llm_tiebreak` — resolved by the LLM tiebreak (§4.6 tier 2)
- `human_confirmed` — validator accepted the auto match
- `human_corrected` — validator replaced the auto match with the right business
- `human_added` — no auto match; validator supplied the provider
- `unresolved` — no confident auto match; awaiting/without human resolution

`nameMismatch: true` with a strong match (right postcode, vet, many reviews) is the
signal that **our extraction misread the name** — an extraction-quality flag routed to
the human validator, not a fraud flag. Absent any match the property is
`{ "verified": false, "source": "google_places", "matchSource": "unresolved" }`.

- **`checks` is keyed by id** (not an array): clean lookups; the future scoring
  config references ids directly.
- **`version`** lets consumers branch as the layer grows.
- **`status` vocabulary:** `pass` (ran, fine) · `warn` (minor deviation, inside a
  tolerance band — likely rounding/OCR) · `fail` (clear violation, worth a look) ·
  `skipped` (couldn't run: field missing or feature not yet enabled — **not** a
  failure).
- **`summary`** is a **raw tally only** (count of statuses) — not a score. Exists so
  the UI can show "1 failed, 2 warnings" at a glance.

A fully-populated mock (mixed statuses across all checks) lives in the companion
PM doc.

---

## 4. Check inventory

| id | category | step (owner) | products | OCR-sensitivity | phase |
|----|----------|--------------|----------|-----------------|-------|
| `line_item_math` | arithmetic | **both** | all | fragile | 1 |
| `items_sum_to_net` | arithmetic | **both** | all | fragile | 1 |
| `tax_consistent` | arithmetic | **both** | all | fragile | 1 |
| `gross_consistent` | arithmetic | **both** | all | fragile | 1 |
| `iban_valid` | format | Erfassung | all | robust | 1 |
| `bic_valid` | format | Erfassung | all | robust | 1 |
| `vat_id_valid` | format | Erfassung | all | robust | 1 |
| `vat_rate_plausible` | format | Prüfung | all | robust | 1 |
| `dates_plausible` | format | Prüfung | all | robust | 1 |
| `got_code_known` | domain | Prüfung | VCC | robust | 2 |
| `got_factor_in_range` | domain | Prüfung | VCC | robust | 2 |
| `pdf_incremental_updates` | forensics | Prüfung | all | robust | 3 (experimental) |
| `duplicate_invoice` | duplicate | Prüfung | all | robust | 4 |
| `sender_business_verified` | identity | Erfassung | all | robust (external) | 5 |

> **Step = ownership**, per §1.1. `both` = built twice (capture-confidence in
> Erfassung, numerical validity in Prüfung). Prüfung-owned checks assume a
> human-verified capture and may live in the separate Prüfung step, not this repo —
> execution locus is a separate decision (§9).

> **14 checks.** Two originally-planned forensics checks (`pdf_producer_plausible`,
> `pdf_dates_consistent`) were **dropped as status-producing checks** after the
> 2026-07-22 test-data probe (Appendix A) — they false-positive on legitimate
> invoices. Their raw values survive as `evidence` on the remaining forensics check.
> `sender_business_verified` is the only **external/paid** check (Appendix B) and is
> data-governance-gated.

### 4.1 Arithmetic (phase 1) — ported from the client-side sanity spec

Reuse the tolerance model from the (deleted) `2026-05-30-ui-sanity-checks-design.md`:

- `pass` when `Δ ≤ max(0.02, 0.001 × ref)`
- `warn` when `Δ ≤ max(1.00, 0.010 × ref)`
- else `fail`
- Missing operands → `skipped`. Mixed VAT rates downgrade a tax `fail` to `warn`.

Checks: `line_item_math` (`qty × unitPriceNet ≈ lineTotalNet`, per line, rolled up
to worst-of), `items_sum_to_net` (`Σ lineTotalNet ≈ totals.net`), `tax_consistent`
(`net × rate/100 ≈ tax.amount`), `gross_consistent` (`net + tax.amount ≈ gross`).
`evidence` carries expected/observed/deltaEur so the UI shows the *why*.

**Built in both steps (§1.1), same recompute, two meanings:**
- **Erfassung — capture-confidence.** A residual beyond tolerance means *"our numbers
  may be misread"* → routes the subdocument to the human validator. Never a verdict,
  never blocks extraction. Soft.
- **Prüfung — numerical validity.** On human-verified numbers, a residual is a genuine
  content finding — but **rounding-aware**: German invoices legitimately differ by
  cents depending on per-line vs per-total VAT rounding and sub-cent precision, so the
  `warn` band absorbs normal rounding and `fail` fires only on material deviation. (A
  hard equality check is wrong here — this was a concrete PM learning.)

### 4.2 Format (phase 1)

- `iban_valid` — ISO 13616 mod-97 checksum. Doubles as an OCR canary (checksum
  catches single-digit misreads). Missing IBAN → `skipped`.
- `bic_valid` — BIC format regex (if present, else `skipped`).
- `vat_id_valid` — USt-IdNr format (DE regex now; other-country regexes later).
  VIES *online* validation is out of scope (external call). Missing → `skipped`.
- `vat_rate_plausible` — `tax.rate ∈ {19, 7, 0}` (DE). Unknown rate → `warn`.
- `dates_plausible` — `issuedAt` parseable and not in the future; earliest service
  date ≤ issue date. Unparseable/missing → `skipped`.

### 4.3 Domain — GOT (phase 2)

See §5 for the catalogue these depend on.

**`got_code_known`.** For each line item with a GOT code:
1. Normalize the code (trim whitespace, handle species suffix, e.g. `20f`/`20g`).
2. Look up candidates in the catalogue for the invoice's species.
3. **Codes are not unique** (3C's `Besonderheiten` sheet: one number → many texts).
   So disambiguate by description **text similarity** against candidates:
   - confident single match → `pass`
   - code found but text ambiguous among multiple entries → `warn` (route to manual)
   - code not in catalogue at all → `warn` (could be a catalogue gap / non-Kleintier
     code / OCR typo — **not** `fail`, we don't punish the vet for our gaps)
4. `evidence`: matched code(s), matched description, similarity, `unknownCodes[]`.

**`got_factor_in_range`.** For each `standard`-type position (see §5.3):
1. Effective factor is computed from the amount and the catalogue base rate:
   `effective_factor = unitPriceNet / base_net_eur` (more robust than trusting the
   LLM's extracted `got.multiplier`; the reported multiplier is cross-checked and a
   material disagreement is noted in `evidence`).
2. Status by legal range (§5, GOT §2):
   - `effective_factor ≤ 3.0` → `pass`
   - `3.0 < effective_factor ≤ 4.0` → `warn` (legal only with a written
     Honorarvereinbarung we can't see → flag for review)
   - `> 4.0` → `fail`
3. Non-`standard` positions (fixed fee, time-based, per-unit) → `skipped` for this
   check (a Notdienstgebühr or time-based item has no meaningful factor). `qty` is
   respected for `per_unit` positions.
4. Rolled up to worst-of across line items; `evidence` names the worst position.

### 4.4 Forensics (phase 3, **experimental**) — PDF metadata, OCR-independent

**Reality check (Appendix A):** a probe of 44 real originals showed PDF metadata is
a *weak, low-coverage* signal here. Legitimate invoices come from dozens of backends
(SAP, Ghostscript, ReportLab, eDocPrintPro, PDFCreator, LibreOffice, ABBYY) —
including office suites and browser "print to PDF". There is **no scanner baseline**
to deviate from, `CreationDate ≠ ModDate` is normal (OCR/ERP re-saves), and **no file
carried an `xmpMM:History` edit trail** (only Adobe tools write those, and they don't
appear in this population). So the confident detectors we imagined don't hold.

Consequences:
- **Dropped as checks:** `pdf_producer_plausible` (producer denylist) and
  `pdf_dates_consistent` — both false-positive on legitimate documents. Their raw
  values are retained as `evidence` for a human, but produce **no status**.
- **`pdf_incremental_updates`** — the one retained check. Post-hoc incremental saves /
  appended xref (`/Prev`, multiple `%%EOF`) → `warn`. Base rate in the probe was
  **0/44**, so a hit is genuinely unusual — but we have **no manipulated positive
  samples** to validate it. Ships **behind a flag / low-weight, marked experimental**
  until we have real doctored examples to tune against.

Read from the **original** PDF bytes, captured in `Pipeline.__init__` **before**
orientation correction rewrites the file (the probe confirmed our own pipeline
otherwise overwrites producer→MuPDF and dates→processing-time). This is
**document-scoped**: one result per uploaded file, attached at the top level (not
duplicated per subdocument, since every split inherits identical file metadata).
`evidence` carries producer, creator/CreatorTool, CreationDate, ModDate, and the
incremental-update counts.

### 4.5 Duplicate (phase 4) — needs persistence

`duplicate_invoice` — fingerprint matched against a store of prior submissions.
Fingerprint inputs:
- content-derived: sender + invoice number + gross + date, plus a fuzzy content hash;
- **metadata-derived (from the probe, Appendix A): `xmpMM:DocumentID` /
  `xmpMM:InstanceID`.** These are stable UUIDs that survive re-submission — the probe
  found the *same* DocumentID across 3–4 re-uploaded files — so they're a cheap, exact
  fingerprint for identical documents, complementing the fuzzy content hash.

Requires a persistent fingerprint store, so it's the last phase. Until enabled it
emits `skipped` with a "not yet activated" detail.

### 4.6 Identity — `sender_business_verified` (phase 5, external)

Soft corroboration that the invoice sender is a real, findable business — validated
against **Google Places API (New) Text Search**. Product-agnostic (for BPS/Sanierer
the type check targets the relevant trade instead of `veterinary_care`). Validated on
live data — see Appendix B; effectively every real vet was located, and the bonus
signals (`businessStatus`, `veterinary_care` type, review count) all fired.

**Query strategy (from Appendix B):** primary = `name + city`; fallback = `name`
only; full address only as a tiebreak (it can misfire to a closed sub-listing); phone
adds little and is often missing.

**Field mask = Pro tier (cost decision, Appendix C):** request `id, displayName,
formattedAddress, businessStatus, types, location` — all **Pro-tier** fields. We
**omit** `rating`, `userRatingCount`, and phone: those are **Enterprise-tier** fields,
and Google bills each call at the highest tier requested, so including them would move
every call to Enterprise (5× smaller free cap: 1,000 vs 5,000/month). The only thing
lost is the review-count legitimacy signal — a fair trade for staying Pro. Revisit if
review count proves worth Enterprise.

**Matching cascade (deterministic first, LLM tiebreak):**
1. **Tier 1 — deterministic (free).** Normalize (lowercase, strip punctuation,
   tokenize) and score candidates by: **token-set overlap** of names + **postcode
   match** + **city match** + **`veterinary_care` type**. A vet-typed business at the
   exact postcode is a strong match even at low raw name similarity (full-string
   similarity alone gave false negatives on OCR errors and naming variants —
   Appendix B). (Review count would help here but is Enterprise-tier, so omitted — see
   field-mask note above.)
2. **Tier 2 — LLM tiebreak (only the ambiguous middle band).** If Tier 1 is neither
   clearly-match nor clearly-no-match, one brief LLM call receives the extracted
   sender + the top 1–3 candidates and returns same-business yes/no/uncertain. Cached
   by (sender, candidate-ids); fires rarely, so cost/latency stay bounded.

**Status:**
- confident match, operational, right type → `pass`
- weak/ambiguous match (after tiebreak), or `CLOSED_TEMPORARILY`, or type mismatch →
  `warn`
- **no findable match → `warn`, never `fail`** — absence from Google ≠ fraud (small/
  new/rural practices, OCR-mangled names, billing entity ≠ storefront name)
- `CLOSED_PERMANENTLY` (esp. vs invoice date) → `warn`
- no usable sender name, API down/timeout, or feature disabled → `skipped`

On a confident match the canonical record is written to `verifiedProvider` (§3.1),
including `matchConfidence` and `nameMismatch` (the latter doubling as an
extraction-quality signal). `evidence` on the check carries the winning query
variant, matched name, name similarity, and which tier decided.

### 4.6.1 Provider cache / registry (Postgres)

Caching justifies itself here on **latency** (no per-invoice network round-trip),
**resilience** (Places down ≠ check fails), and — chiefly — because the store becomes
a **durable, human-curated canonical vet registry**. Cost is the *weakest* reason:
with caching + the finite German vet population, steady-state Places spend trends to
~$0 (Appendix C).

**Store = Postgres, not Redis.** The cache is a durable asset, not ephemeral — the
validator's confirmed corrections must not live somewhere evictable. Postgres also
gives native fuzzy matching (`pg_trgm` trigram similarity + GIN index) and is
queryable. Note this is **new infra** (Azure Database for PostgreSQL); the data is
tiny (~11k rows max), so a small instance suffices. Redis may later be a read-through
hot layer, but Postgres is the system of record.

**Two tables — resolver + registry** (separates "messy name → identity" from
"identity → data"):

```
provider_alias                          provider
  normalized_name   (indexed)             place_id        (PK)
  city / postcode                         payload         (full Places record)
  place_id  --> provider                  business_status
  match_source                            types
  seen_count / last_seen_at               last_verified_at
```

**Lookup flow per invoice:**
1. Normalize extracted sender name (+ city/postcode).
2. **Exact** key hit on `provider_alias` → return `provider`. (free, instant)
3. Miss → **trigram fuzzy** search on `provider_alias`, **scoped to the same
   city/postcode**, conservative threshold → hit → return, record the new alias.
4. Still ambiguous / miss → **call Places**, upsert `provider` + `provider_alias`.

**Two rules that matter:**
- **Bias toward calling Places when uncertain.** A false cache hit returns the *wrong*
  vet — worse than paying ~$0.03. So scope fuzzy matches by city/postcode, keep the
  threshold high, and fall through to the API rather than guess. `place_id` (not the
  name) is the identity anchor — and also feeds `duplicate_invoice` (§4.5).
- **Human-correction flywheel.** When the validator confirms/corrects a
  `verifiedProvider` (§1.1), write the (messy extracted name → correct `place_id`) back
  as an alias with `match_source = human_corrected`. The next time that OCR variant
  appears, it resolves to the human-verified answer **without any Places call**. The
  registry compounds in value over time.

**Staleness.** `business_status` changes (a practice closes), so a pure cache would
serve a stale "operational" forever. `last_verified_at` drives a **refresh policy** —
re-fetch a `provider` older than ~6–12 months (lazily on next access). Tiny cost;
keeps `CLOSED_PERMANENTLY` meaningful.

**Phasing:** v1 = exact-key cache + human write-back (call Places on miss, no fuzzy);
v2 = add `pg_trgm` fuzzy lookup + refresh policy. The check can even ship cache-less
first (free tier covers it) and gain the registry later.

---

## 5. GOT catalogue (the phase-2 dependency)

### 5.1 Scope & source of truth

- **Scope v1: Kleintier — Hund + Katze.** Covers VCC's real invoice volume; smaller
  and faster to verify than the full Gebührenverzeichnis. Pferd / farm animals
  deferred.
- **Source of truth: the official `GOT_2022.pdf`** (BGBl text). 3C's Excel files
  (`GOT_Hund_2022.xlsx`, `GOT_Katze_2022.xlsx`, `GOT_LV_Hund_Katze_Pferd.xlsx`)
  are used as a **pre-digested draft + cross-check**, not the runtime source. Their
  `Hinweise` sheet is a convenient transcription of the legal rules; `Besonderheiten`
  captures the non-unique-code and per-unit caveats.

**Why PDF-as-truth:** amendment-readiness. When the pending GOT amendment lands, we
re-run the build against the new PDF and bump the version — no dependence on someone
re-deriving spreadsheets by hand.

### 5.2 Build pipeline (offline, versioned artifact)

`scripts/build_got_catalogue.py`:
1. **Parse** the xlsx sheets into draft rows (fast start; amounts pre-computed at
   1×/2×/3×/4×).
2. **Clean/normalize:** drop section-header and blank/placeholder rows; trim codes;
   split species suffixes; dedupe; classify `position_type`.
3. **Verify against `GOT_2022.pdf`:** confirm base rates and descriptions against the
   authoritative text (assisted parse of the PDF, then manual sign-off on
   discrepancies — a wrong base rate silently corrupts every future check).
4. **Emit** a clean, versioned artifact `core/reference/got/got_2022.json` (checked
   into the repo, bundled in the image). Human-reviewable diff on every rebuild.

Runtime: the catalogue is loaded once and cached; product-agnostic module. Checks
receive it via `CheckContext.catalogue`.

### 5.3 Catalogue schema

```
meta:
  version: "got-2022"
  valid_from: "2022-11-22"
  valid_to: null            # set when superseded
  source: "BGBl. … / GOT_2022.pdf"
positions[]:
  code: "20"                # normalized
  species: "hund" | "katze"
  description: "Allgemeine Untersuchung mit Beratung"
  base_net_eur: 13.47       # 1× (einfacher Satz)
  max_3x_net_eur: 40.41     # precomputed convenience (3× standard cap)
  position_type: "standard" | "fixed" | "time_based" | "per_unit"
  usage_notes: "…"          # from Vorkommen (e.g. "nur einmal pro Rechnung")
rules:                      # global, from Hinweise (GOT §2 etc.)
  factor_range: { min: 1.0, standard_max: 3.0, hard_max: 4.0 }
  notdienstgebuehr_net_eur: 50.00
  vat_rate_default: 19
```

### 5.4 Versioning & amendment-readiness

- One artifact **per GOT version** (`got_2022.json`, later `got_2026.json`), each
  with `valid_from`/`valid_to`.
- A small loader selects the applicable catalogue by the invoice's date
  (`CheckContext.invoice_date`), so historical invoices are checked against the GOT
  that was in force when they were issued.
- Rebuild + version-bump is the whole amendment workflow. No code change to the
  checks.

---

## 6. The scoring layer (deferred — described only as a seam)

Not built in v1. Documented so we don't design ourselves into a corner: a future
per-client scoring module consumes `plausibility.checks`, applies client-specific
weights (weighting OCR-robust checks higher, tolerating OCR-fragile ones), and
produces whatever the client needs (score, risk band, gate decision). Because the
raw layer already carries `category`, `status`, and OCR-sensitivity (via the check
inventory), the scoring layer needs no new data from us — only configuration.

---

## 7. Resilience & testing

- **Resilience:** every check is wrapped so failures degrade to `skipped`; the
  plausibility block is additive and can never fail a job. Catalogue-load failure
  disables only the domain checks (they emit `skipped`).
- **Testing:**
  - Pure unit tests per check (pass/warn/fail/skipped boundary cases, esp.
    arithmetic tolerance bands and GOT factor thresholds).
  - Golden-file tests: sample invoices → expected `plausibility` block.
  - Catalogue build test: a fixture xlsx/PDF slice → expected catalogue rows,
    covering non-unique codes and each `position_type`.
  - Pipeline test extending the existing `postprocess_extraction` per-subdoc test to
    assert the block is attached.

---

## 8. Build phases (sequencing, not commitments)

1. **Framework + arithmetic + format.** Evaluator, registry, context, output
   contract, `summary`, per-product enablement. Arithmetic is already designed
   (ported); format checks are self-contained. Ships value on its own.
2. **GOT domain checks + catalogue.** Build pipeline → `got_2022.json` → the two
   domain checks with text-matching and position-type awareness. VCC-only.
3. **PDF-metadata forensics (experimental).** Early metadata capture + the single
   `pdf_incremental_updates` check, behind a flag / low-weight. Product-agnostic.
   **Blocked on** obtaining real manipulated samples before it can be trusted or
   weighted; until then it's evidence-gathering, not a headline signal.
4. **Duplicate detection.** Fingerprint store + matching (content hash +
   DocumentID/InstanceID). Heaviest; last.
5. **Identity verification (Google Places).** External `sender_business_verified`
   check + `verifiedProvider` enrichment, with the deterministic→LLM matching cascade
   and Redis cache. **Gated on data-governance sign-off** (§9). Concept validated
   (Appendix B); can slot in independently once cleared.

---

## 9. Open questions

1. **Check starter set** — is the 14-check inventory the right v1 scope? (PM to
   confirm; add/drop.)
2. **`vat_id_valid` depth** — format-only now; is VIES online validation ever
   wanted (adds an external dependency + latency)?
3. **Manipulated samples for forensics** — `pdf_incremental_updates` can't be
   validated or weighted without real doctored invoices. Can 3C/the customer supply
   any? Without them, forensics stays experimental/evidence-only.
4. **Duplicate scope** — dedupe within a single upload only, or across all historical
   submissions (bigger persistence story)?
5. **Data governance for Google Places** — `sender_business_verified` egresses sender
   name/address to a US third party. Acceptable under client agreements / does it need
   a DPA first? Go/no-go for the whole identity check. (Experiment first, per the PM.)
6. **LLM tiebreak budget** — confirm the ambiguous-band LLM call is worth its
   cost/latency vs. just emitting `warn` and letting a human decide.
7. **Execution locus of Prüfung-owned checks** (§1.1) — are the Prüfung checks (GOT,
   arithmetic-validity, forensics, dedup, rate/date plausibility) built in the
   separate Prüfung step/codebase, or computed opportunistically here and tagged as
   Prüfung findings? Ownership is settled (the table); where the code runs is not.

---

## Appendix A — Test-data forensics probe (2026-07-22)

Probed 44 real *original* uploads (BPS_Input ×7, Sanierer_Input ×7, VCC `temp/`
originals ×30) with PyMuPDF, reading Info dict + XMP from original bytes.

**Metadata is per *file*, not per page** — one Info dict (+ optional XMP) in the
trailer; no per-page producer/date exists in PDF. Page *layout* (text vs image) is
per-page and varies within a bundle. Multi-invoice PDFs → all subdocuments share one
metadata set (⇒ forensics is document-scoped). Our own split/orientation steps
rewrite metadata, so forensics must read the **original** bytes.

**Producers (all legitimate invoices):** SAP NetWeaver, Stimulsoft Reports, d.velop
AG, Aspose.PDF for .NET, eDocPrintPro, GPL Ghostscript, ReportLab, OpenPDF,
LuraDocument, ABBYY FineReader Server, **LibreOffice 7.3**, **Skia/PDF (Chrome print)**.
→ No "scanner baseline"; office/browser producers are normal ⇒ producer denylist
unusable.

**Dates:** `CreationDate == ModDate` in ~20/30 VCC files, but `≠` is also common and
legitimate (ABBYY OCR re-save, SAP, LibreOffice). ⇒ date-gap check unusable.

**Incremental updates** (`>1 %%EOF` or `/Prev`): **0/44**. Clean base rate, but no
positive samples to validate against.

**XMP:** present in 20/26 sampled (sparse — 6 have none, several are empty stubs).
`CreatorTool` just mirrors the producer. **No `xmpMM:History` edit trail in any
file** (only Adobe tools write those). ⇒ edit-history forensics is a dead end here.

**Useful for dedup:** `xmpMM:DocumentID`/`InstanceID` are stable UUIDs that repeat
across re-uploads — observed the same DocumentID across 4 files (a SAP form) and
across 3 files (an ABBYY output). Fed into `duplicate_invoice` (§4.5).

Probe scripts: `scratchpad/pdf_forensics_probe.py`, `scratchpad/xmp_probe.py`.

---

## Appendix B — Google Places identity probe (2026-07-22)

Live dry run against Google Places API (New) Text Search: 10 real VCC vet PDFs →
page-1 vision extraction of the sender → 4 query variants each → 29 real API calls
(all cached). Script + cache: `places_experiment/` (`run_places_experiment.py`,
`cache/`, `senders/`, `results.json`).

**Coverage.** 7/10 cleared a *strict* rule (full-string name_sim ≥ 0.6 AND vet type).
The 3 that didn't were **not Places failures**:
- one was **our OCR error** — extracted "Klinik & Dünen…"; Places returned the real
  "Tierarztpraxis Klink & Dühnen" (same postcode, vet, 427 reviews). Places *corrected
  us*.
- one was a **naming variant** — "Tierarztpraxis Ebeling…" vs canonical "Tierärztliche
  Gemeinschaftspraxis Ebeling Gb" (same postcode, 363 reviews); token overlap matches,
  full-string similarity doesn't.
- one had **no extractable sender** (page-1 read returned null) → `skipped`.

⇒ Effectively every findable real vet was located. Motivates the smarter matcher
(§4.6): token-set overlap + postcode/city + vet type, not full-string similarity.
(Review count was informative here but is Enterprise-tier, so dropped for cost —
Appendix C.)

**Bonus signals — all fired on live data:**
- `businessStatus`: one practice returned a `CLOSED_PERMANENTLY` sub-listing on the
  *address* query but `OPERATIONAL` (1513 reviews) on the *name/city* query.
- `veterinary_care` type: True on every match — strong disambiguator.
- Review counts 18–1657: strong legitimacy proxy (a fabricated clinic has none).

**Match-key verdict:** `name_only` and `name+city` matched as well as or better than
`name+address`; **full address sometimes hurt** (biased to the closed sub-listing);
`name+phone` added nothing and phone was often unextracted. ⇒ primary `name + city`,
fallback `name`, address as tiebreak only.

**Cross-cutting insight:** name mismatch on an otherwise-strong match = *our*
extraction error, not fraud ⇒ surfaced as `verifiedProvider.nameMismatch` (§3.1), an
extraction-quality signal, and reinforces keeping extraction-confidence separate from
invoice-integrity.

---

## Appendix C — Google Places cost analysis (2026-07-24)

Source: Google Maps Platform pricing list (official, last updated 2026-07-20).

**Billing model.** Pay-per-request; the **field mask sets the price tier** — Google
bills each Text Search call at the *highest* tier of any field requested. Each SKU has
its own monthly free cap, then a per-1,000 rate (volume discounts above 100k/mo).

**Field → tier:** `displayName, formattedAddress, businessStatus, types, location` are
**Pro**; `rating, userRatingCount, nationalPhoneNumber` are **Enterprise**.

| Text Search SKU | Free/mo | Rate (0–100k) | Per call |
|---|---|---|---|
| **Pro** (our chosen mask) | **5,000** | $32.00 / 1,000 | **$0.032** |
| Enterprise (adds rating/reviews/phone) | 1,000 | $35.00 / 1,000 | $0.035 |

Per-call delta is small; the **free cap differs 5×**, which dominates at low volume ⇒
we take **Pro** (§4.6), losing only the review-count signal.

**Monthly cost by *billable* lookups** (= distinct vets after cache, not invoices):

| Distinct lookups/mo | Pro ($32/1k, 5k free) | Enterprise ($35/1k, 1k free) |
|---|---|---|
| 1,000 | $0 | $0 |
| 5,000 | $0 | $140 |
| 10,000 | $160 | $315 |
| 50,000 | $1,440 | $1,715 |

**Bounded by population.** Germany has ~10–11k vet practices. With the Postgres
registry (§4.6.1), billable calls ≈ distinct practices seen; full national coverage is
a **one-time ~11k calls** (~$350 at Enterprise, largely free-tier-absorbed at Pro),
then only new/changed practices. Steady-state Places spend → ~$0. VCC realistically
sits in the top rows. The `sender_business_verified` **LLM tiebreak** is a separate,
small Azure OpenAI cost (fires only on ambiguous matches), not Places.
