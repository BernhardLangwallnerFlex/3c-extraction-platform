# Content-policy fallback and retry policy — design

**Date:** 2026-08-14
**Products:** vetcostcheck, bps, sanierer (shared core — all three)
**Origin:** BPS production failure 2026-08-14, reported by the PO as
*"Work-horse terminated unexpectedly; waitpid returned 15 (signal 15)"*.

## Problem

A 33-page BPS document failed in production. The reported error was a red herring: RQ's
work-horse was killed by SIGTERM on the *third* attempt. The first two attempts had already
failed on the real cause:

```
openai.BadRequestError: 400 — content_policy_violation
"Your input image may contain content that is not allowed by our content safety system."
```

Azure OpenAI's content safety filter rejected the request. Testing each page individually
against the same deployment isolated it to **page 20 of 33, and only page 20**: a water-damage
inspection sheet showing four close-up photos of a leaking cast-iron pipe inside a ceiling
opening. The bottom-right photo — wet, glistening, reddish-brown corrosion over saturated
insulation, with a red annotation arrow — reads at 150 dpi like a wound to an image
classifier. It is a pipe. This is a false positive on ordinary claim evidence.

The rejection happened in `analyze_document`, which sends **every page** of the document as
one request. So a single benign photo failed the whole 33-page document.

Three defects made it worse than it had to be:

1. **A permanent error is retried.** `_call_analyze_llm` and `_call_openai` both use
   `@retry(stop=stop_after_attempt(3))` with no exception filter, so a 400 is retried like a
   timeout. RQ then retried the whole job twice more, each attempt re-running both OCR engines
   (~75 s). Roughly five minutes of compute and three full OCR passes on something that could
   never succeed.
2. **No fallback.** The extraction prompt already receives the complete dual-OCR markdown;
   images are an aid, not the only signal. Nothing tried without them.
3. **The failure is unreportable.** The consumer saw `status: failed` with a work-horse
   message. Nobody can act on that.

This will recur. BPS and Sanierer documents are claim files built around damage photography;
this is a document *class*, not a freak occurrence. It was the only occurrence in the
preceding seven days, so the frequency is low — but the cost when it fires is a whole
document.

## Decision

Degrade to text-only and keep the document, rather than failing it. Record the degradation
twice: in German prose in `warnings` for a human, and in a new machine-readable
`qualityFlags` array for the consumer's code.

## Architecture

### Layer 1 — `core/llm_errors.py` (new)

Three pure functions, no I/O, no product knowledge:

- **`is_retryable(exc) -> bool`** — the tenacity predicate. Any HTTP 4xx is permanent
  **except** 408 (request timeout) and 429 (rate limit). Everything else — 5xx, connection
  errors, read timeouts, anything without a status code — stays retryable, preserving today's
  behaviour for the transient failures retries exist for.
- **`is_content_policy_rejection(exc) -> bool`** — true for a `BadRequestError` whose body
  carries `error.code == "content_policy_violation"`. Falls back to matching the message text,
  because the code field has not always been present in Azure's responses.
- **`strip_image_blocks(blocks) -> list`** — returns only the blocks whose `type` is not
  `image_url`, preserving order.

### Layer 2 — retry policy at both call sites

`core/pipeline.py:_call_analyze_llm` and `core/processors/azure_processor.py:_call_openai`
keep their own identities and their own decorators; only the policy changes:

```python
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30),
       retry=retry_if_exception(is_retryable), before_sleep=log_retry, reraise=True)
```

A non-retryable exception now propagates on the first attempt instead of the third.

### Layer 3 — the vision fallback

`call_with_vision_fallback(call_fn, client, model, blocks) -> (response, vision_dropped)`
lives in `core/llm_errors.py`. **Both** call sites route through it:
`core/pipeline.py:analyze_document()` passes `_call_analyze_llm`, and
`core/processors/azure_processor.py:AzureInvoiceProcessor.extract()` passes `_call_openai`.
Because `call_fn` is the already-decorated function, retryable errors are still retried inside
it; the fallback only ever sees an exception that survived the retry policy.

Two things it deliberately does **not** do:

- It does not re-invoke `extract()` with `use_vision=False`. That flag also selects the model
  and rebuilds the prompt; the fallback must re-send the *same* request minus the images.
- It does not switch models. The vision model handles a text-only request fine, and swapping
  to `self.model` would change extraction behaviour on top of the degradation.

The sequence:

1. Call with the full blocks. Success → `(response, False)`.
2. On a content-policy rejection → call again with `strip_image_blocks(blocks)`.
   Success → `(response, True)`.
3. If the text-only call also fails, raise. That is a genuine failure.

**The fallback deliberately sits outside the tenacity decorator.** Retry means "the same
request might succeed"; this sends a *different* request. Nesting it inside the retry would
re-attempt the stripped call three times and muddle the two concepts.

**Images are dropped wholesale, not bisected — decided by measurement.** Isolating the
offending page by binary search is *cheap*, not expensive: a probe omits the markdown, so
~32 low-detail images at 212 tokens each is ≈6,800 tokens ≈ **1.5 cents**, about a third of
one analyze call. Cost is therefore not the objection.

The objection is that it buys nothing measurable. The failing document was run through analyze
three times text-only and three times with all images except page 20:

| Round | Text-only | All but page 20 |
|---|---|---|
| 1 | `{1:[9-14], 2:[22,23], 3:[24,25,26], 4:[28,29,30]}` | `{1:[9,10], 2:[22,23], 3:[24,25,26], 4:[28,29,30]}` |
| 2 | `{1:[9,10], 2:[22,23], 3:[24,25,26], 4:[28,29,30]}` | identical |
| 3 | `{1:[9,10], 2:[22,23], 3:[24,25,26], 4:[28,29,30]}` | identical |

Five of six runs produced the same split; round 1's divergence reproduced as ordinary model
variance rather than an image effect, and Belege 2–4 were identical in every run. The analyze
prompt's primary boundary signals — differing Belegnummer, sender, date — are textual, which
is consistent with this result.

This is evidence from **one** document. Revisit if degraded documents show split problems in
practice; the bisect remains cheap enough to add later.

### Layer 4 — reporting the degradation

Two channels, because they serve different readers: German prose for a human in `warnings`,
and a stable token for the consumer's code in `qualityFlags`.

#### `qualityFlags` — new field

An array of ASCII tokens, **always present**, empty when nothing degraded. Placed immediately
after `returncodeReasons`, so the three metadata fields lead every subdocument:

```json
{ "returncode": 100, "returncodeReasons": [], "qualityFlags": ["VISION_DROPPED"], "type": "invoice", … }
```

| Flag | Meaning |
|---|---|
| `VISION_DROPPED` | The content filter rejected the images; this subdocument was extracted from OCR text alone |
| `SINGLE_ENGINE_OCR` | One OCR engine failed; the text came from the other one only |

**It must not be folded into `returncode`.** `returncode` answers *"is this a Beleg?"*;
`qualityFlags` answers *"how well did we read it?"*. Conflating them would let a badly-read
invoice look like a non-invoice, which auto-cancels a legitimate claim — the failure mode the
returncode design exists to avoid.

`SINGLE_ENGINE_OCR` is not new behaviour: `DualOCRProcessor` has always degraded to one engine
and fired a Sentry warning, and the consumer has never been told. This exposes an existing
reliability property rather than adding one. It is set when `DualOCRProcessor` reports the
degradation, and applies to every subdocument of that job.

Values are a closed set defined here; adding one is a documented contract change. Unknown
values must be ignored by consumers, so the set can grow without breaking them.

#### `warnings` — existing field

`warnings` is a list of German strings on each subdocument. Two new entries, appended after
the product's `postprocess_extraction` hook and after the returncode floor, so they survive
both:

- **Analyze degraded** — the split ran without images, which affects every subdocument, so the
  warning is appended to **all** of them:
  > `Die Aufteilung des Dokuments erfolgte ohne Bildanalyse, da der Inhaltsfilter des KI-Dienstes mindestens eine Seite abgelehnt hat. Die Zuordnung von Seiten zu Belegen kann ungenauer sein.`

- **Extraction degraded** — appended to **that subdocument only**:
  > `Die Extraktion dieses Belegs erfolgte nur anhand des OCR-Textes, da der Inhaltsfilter des KI-Dienstes das Seitenbild abgelehnt hat. Einzelne Werte können ungenauer sein.`

`Pipeline.analyze_vision_dropped` is set in `analyze_document()` and read in
`_extract_single_subdocument()`. If `warnings` is absent or not a list, it is created. It must
default to `False` on the Pipeline instance, because several existing tests construct
`Pipeline` via `object.__new__` and never run `analyze_document()`.

Ordering within `_extract_single_subdocument` is fixed: extract → product
`postprocess_extraction` hook → `apply_returncode_floor` → append warnings. The floor returns
a new dict with `returncode` and `returncodeReasons` first, so appending afterwards mutates
the list in place on the dict the consumer receives, and both field-order guarantees hold.

Each degradation also emits a structlog event and a
`sentry_sdk.capture_message(..., level="warning")`, matching the existing DualOCR-degradation
pattern: the job succeeds, and the degradation is still alertable. Distinct event names for
the two cases so alert rules need not parse a message string.

`returncode` is unaffected. A text-only extraction classifies exactly as any other.

### Layer 5 — stop the doomed RQ retries

`process_file` catches a non-retryable client error, sets the current RQ job's
`retries_left = 0`, and re-raises. Without this, the `Retry(max=2)` at enqueue time still
re-runs the entire pipeline twice — including both OCR engines — before anyone sees, for
example, a 401 from a rotated key. The narrower tenacity policy alone does not prevent this;
it only makes each doomed attempt fail faster.

## Consumer impact

**Additive only.** `qualityFlags` is a new field; nothing existing changes shape or meaning. A
document that would previously have failed outright now returns a normal result carrying one
warning and one flag.

This ships **in the same release as the returncode feature**, so 3C integrates both in one
pass rather than two. That is the point of the timing: they are implementing `returncode`
now, and a second contract change a month later costs them another cycle.

`extract_schema.json` gains `qualityFlags` for all three products, which publishes it through
Swagger, and `vetcostcheck_api_doc.md` documents both flag values.

## Release

**Not an independent hotfix.** `promote.sh` refuses any tag that is not currently deployed on
the product's test app, so pushing this to production on its own would mean displacing the
returncode build that 3C is integrating against. Both changes therefore ship as one release:
implement on `main`, deploy to test alongside returncode, promote once 3C approves.

The accepted cost is that content-policy failures continue in production until then — roughly
one document per week, visible and recoverable, since the upload survives (cleanup runs only
on success) and can be resubmitted.

## Testing

**Unit — `is_retryable`:** 400, 401, 403, 404 are not retryable; 408 and 429 are; 500, 502,
503 are; a connection error with no status code is; a bare `Exception` is (fail toward
today's behaviour).

**Unit — `is_content_policy_rejection`:** true for the real Azure body shape captured from
production; true when only the message text matches and `code` is absent; false for other
400s.

**Unit — `strip_image_blocks`:** removes every `image_url` block, keeps text blocks in order,
leaves a text-only list unchanged.

**Unit — `call_with_vision_fallback`:** clean call returns `vision_dropped=False` and never
calls twice; a content-policy rejection followed by success returns `vision_dropped=True` and
the second call carries no image blocks; rejection on both calls raises; a non-content-policy
error propagates without a second call.

**Pipeline — warning propagation:** analyze degradation puts the analyze warning on every
subdocument; extraction degradation puts the extraction warning on only the affected one; a
subdocument with no prior `warnings` key gets a well-formed list; existing warnings are
preserved.

**Pipeline — `qualityFlags`:** present and empty on a clean job; carries `VISION_DROPPED` on
every subdocument after an analyze degradation and on only the affected one after an
extraction degradation; carries `SINGLE_ENGINE_OCR` on every subdocument when DualOCR
degraded; carries both, without duplicates, when both happened; sits immediately after
`returncodeReasons` in key order.

**Products:** all three `extract_schema.json` files declare `qualityFlags` with the closed
enum, checked by the existing parameterized contract test in
`tests/products/test_returncode_contract.py`.

**Pipeline — composition:** the existing returncode-floor and VCC-postprocess tests still
pass, proving the warning append composes with both.

**Acceptance (manual, maintainer's machine):** the production document that triggered this —
33 pages, page 20 flagged — must complete and return subdocuments carrying the analyze
warning. The file is customer data and is not committed; it was retained in
`uploads-bps/` because cleanup runs only on success, and expires under the 14-day lifecycle
rule.

**Regression:** `scripts/returncode_sweep.py --expect 100` over the three corpora must stay
green — the retry-policy change touches every LLM call in the pipeline.

## Out of scope

- **KEDA scale-in killing in-flight jobs.** A separate root cause: RQ removes a job from the
  queue list the moment it is dequeued, so a running job looks like an empty queue to a
  `listLength` trigger, and the 30-second polling interval misses the brief re-enqueue windows
  on retry — so the 300 s cooldown never resets. This produced the SIGTERM that made the
  reported error confusing, but it did not cause the failure. It needs its own spec and a
  scaling-rule decision.
- **Bisecting to the offending page.** Rejected on measured evidence, not cost — see Layer 3.
- **An independent hotfix release.** Rejected — see Release above.
- **Asking Azure to relax the content filter for this deployment.** A configuration and policy
  question, not a code one. Worth raising separately if the frequency rises.
