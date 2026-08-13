# Subdocument returncode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every extracted subdocument carries a machine-readable `returncode` (100 = Beleg, 200 = kein Beleg, 300 = nicht lesbar) plus German `returncodeReasons`, so 3C can stop opening a Vorgang for documents that contain no invoice.

**Architecture:** Three layers. The LLM classifies (per-product prompt rule + schema). Core applies a deterministic, fill-in-only floor after the product's postprocess hook, guaranteeing the field always exists and is always one of the three codes. The splitter stops fabricating Belege (analyze prompts learn that zero is a legal answer) and instead guarantees at least one subdocument, so there is always somewhere for the code to live.

**Tech Stack:** Python 3.11, pytest 9.x, structlog, FastAPI/RQ (untouched by this plan).

**Spec:** `docs/superpowers/specs/2026-08-12-returncode-design.md` — read it before Task 1.

## Global Constraints

- `returncode` is **always present** on every subdocument and is **always** the Python `int` 100, 200 or 300 — never a string, never null, never absent.
- `returncodeReasons` is **always present** and is **always a list of strings**: empty for 100, non-empty for 200 and 300.
- `returncode` and `returncodeReasons` are the **first two keys** of the subdocument object.
- The core floor is **fill-in-only**. It never overrides a valid LLM value and **never derives 300**. A false 200 auto-cancels a legitimate claim, so the derivation must never be more eager to emit 200 than the rules stated in Task 1.
- `warnings` keeps its current meaning and content. Nothing in this plan writes to it.
- The response envelope stays `{number_of_subdocuments, subdocuments[]}`. No envelope-level returncode.
- The codes and their meanings are **identical across all three products** (vetcostcheck, bps, sanierer). Only the German wording describing what counts as 200 differs per product.
- All three analyze prompt templates are consumed via `str.format()`. Any literal `{` or `}` added to them **must be doubled** (`{{` / `}}`) or `build_analyze_prompt` raises at runtime.
- Test command: `.venv/bin/python -m pytest tests/ -q`. The suite is at 79 passing before this plan; it must never go red between tasks.
- No linter is configured. Match surrounding style: `from __future__ import annotations`, double quotes, structlog via the module-level `_telemetry`.
- Deployment for this branch is **test tier only** (`./deploy.sh all <tag> test`). Do not run `scripts/promote.sh`. Deployment is not part of any task — it happens after the branch is merged.

---

## File Structure

**Created:**
- `core/returncode.py` — the deterministic floor. Pure functions, no I/O, no product knowledge.
- `tests/core/test_returncode.py` — unit tests for the floor.
- `tests/core/test_pipeline_split_fallback.py` — unit tests for the ≥1-subdocument guarantee.
- `tests/products/test_returncode_contract.py` — parameterized across all three products: prompts and schemas carry the contract.
- `scripts/returncode_sweep.py` — corpus sweep that prints one returncode per subdocument. The regression gate.

**Modified:**
- `core/pipeline.py` — wire the floor into `_extract_single_subdocument`; add the split fallback to `split_document_into_invoices`; extend the cleanup guard in `cleanup_storage_artifacts`.
- `products/{vetcostcheck,bps,sanierer}/extract_prompt.py` — one new rule + two new lines in the inline JSON-Ziel-Schema.
- `products/{vetcostcheck,bps,sanierer}/extract_schema.json` — two new properties, first in `properties`.
- `products/{vetcostcheck,bps,sanierer}/analyze_overrides.py` — Frage 2 learns that 0 is legal.
- `tests/core/test_pipeline_cleanup.py` — existing fixtures must now supply `extraction_result_json` (Task 2 makes the cleanup guard read it).
- `tests/core/test_pipeline_postprocess.py` — one new test proving the floor composes with the VCC hook.
- `vetcostcheck_api_doc.md` — document both fields for 3C's developers.

---

### Task 1: Deterministic returncode floor in core

Creates the module that makes the contract a guarantee rather than a hope, and wires it into the one place every subdocument's result passes through.

**Files:**
- Create: `core/returncode.py`
- Create: `tests/core/test_returncode.py`
- Modify: `core/pipeline.py` (import at top; `_extract_single_subdocument` return at line 335)
- Modify: `tests/core/test_pipeline_postprocess.py` (add one composition test)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `core.returncode.apply_returncode_floor(result: dict) -> dict` — returns a **new** dict with `returncode` (int) and `returncodeReasons` (list[str]) as its first two keys, all other keys preserved in their original order. Also exports `VALID_RETURNCODES: tuple[int, int, int]` and `GENERIC_REASON: str`. Task 2 relies on the emitted `returncode` being an `int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_returncode.py`:

```python
"""The deterministic returncode floor.

The floor's job is to make `returncode` a guarantee: always present, always one
of 100/200/300, whatever the LLM did or didn't emit. It is fill-in-only — a
valid LLM classification is never second-guessed, because an automatic
100 -> 200 downgrade on a real invoice that merely extracted badly would cause
a wrongful Storno.
"""
import pytest

from core.returncode import GENERIC_REASON, apply_returncode_floor


def _beleg(**overrides):
    """An extraction result that looks like a real Beleg."""
    base = {"type": "invoice", "number": "R-123", "issuedAt": "2026-01-05",
            "items": [{"name": "Arbeit"}], "totals": {"net": 100.0, "gross": 119.0}}
    base.update(overrides)
    return base


def _empty():
    """An extraction result with no evidence of a Beleg at all."""
    return {"type": None, "number": None, "issuedAt": None,
            "items": [], "totals": {"net": None, "gross": None}}


@pytest.mark.parametrize("code", [100, 200, 300])
def test_valid_llm_code_is_preserved(code):
    result = apply_returncode_floor(_beleg(returncode=code, returncodeReasons=["Grund"]))
    assert result["returncode"] == code


def test_valid_200_survives_even_though_evidence_would_derive_100():
    # Fill-in-only: the floor must not "correct" the model on a document the
    # model looked at and we did not.
    result = apply_returncode_floor(_beleg(returncode=200, returncodeReasons=["Nur Anschreiben."]))
    assert result["returncode"] == 200
    assert result["returncodeReasons"] == ["Nur Anschreiben."]


@pytest.mark.parametrize("bad", [None, "100", 0, 999, 100.0, True, [100]])
def test_invalid_code_falls_through_to_derivation(bad):
    # Each of these is "not an int in {100,200,300}" and must be re-derived
    # rather than passed through to the consumer.
    result = apply_returncode_floor(_beleg(returncode=bad))
    assert result["returncode"] == 100


def test_missing_code_falls_through_to_derivation():
    payload = _beleg()
    payload.pop("returncode", None)
    assert apply_returncode_floor(payload)["returncode"] == 100


@pytest.mark.parametrize("field,value", [
    ("number", "R-1"),
    ("issuedAt", "2026-01-05"),
    ("items", [{"name": "x"}]),
])
def test_any_single_evidence_field_derives_100(field, value):
    payload = _empty()
    payload[field] = value
    assert apply_returncode_floor(payload)["returncode"] == 100


@pytest.mark.parametrize("total_field", ["net", "gross"])
def test_totals_evidence_derives_100(total_field):
    payload = _empty()
    payload["totals"] = {total_field: 42.0}
    assert apply_returncode_floor(payload)["returncode"] == 100


def test_zero_total_counts_as_evidence():
    # 0.0 is a value the model read off the page, not a missing field.
    payload = _empty()
    payload["totals"] = {"net": 0.0}
    assert apply_returncode_floor(payload)["returncode"] == 100


def test_blank_strings_are_not_evidence():
    payload = _empty()
    payload["number"] = "   "
    assert apply_returncode_floor(payload)["returncode"] == 200


def test_no_evidence_derives_200():
    assert apply_returncode_floor(_empty())["returncode"] == 200


def test_floor_never_derives_300():
    # Deterministically, "unreadable" and "not a Beleg" are indistinguishable.
    # Only the model, which saw the page, may say 300.
    payload = _empty()
    payload.pop("items")
    payload.pop("totals")
    assert apply_returncode_floor(payload)["returncode"] == 200


def test_100_gets_an_empty_reason_list():
    result = apply_returncode_floor(_beleg(returncode=100, returncodeReasons=["übrig"]))
    assert result["returncode"] == 100
    assert result["returncodeReasons"] == []


@pytest.mark.parametrize("code", [200, 300])
def test_generic_reason_injected_when_empty(code):
    result = apply_returncode_floor(_empty() | {"returncode": code, "returncodeReasons": []})
    assert result["returncodeReasons"] == [GENERIC_REASON]


def test_non_list_reasons_are_replaced():
    result = apply_returncode_floor(_empty() | {"returncode": 200, "returncodeReasons": "kein Beleg"})
    assert result["returncodeReasons"] == [GENERIC_REASON]


def test_non_string_reason_entries_are_dropped():
    payload = _empty() | {"returncode": 200, "returncodeReasons": ["echt", 7, None, "  ", "auch echt"]}
    assert apply_returncode_floor(payload)["returncodeReasons"] == ["echt", "auch echt"]


def test_both_fields_come_first_and_nothing_else_is_touched():
    payload = _beleg(returncode=100)
    result = apply_returncode_floor(payload)
    assert list(result)[:2] == ["returncode", "returncodeReasons"]
    assert result["type"] == "invoice"
    assert result["items"] == [{"name": "Arbeit"}]
    assert result["totals"] == {"net": 100.0, "gross": 119.0}
    # Remaining keys keep their original relative order.
    assert list(result)[2:] == ["type", "number", "issuedAt", "items", "totals"]


def test_input_dict_is_not_mutated():
    payload = _beleg()
    apply_returncode_floor(payload)
    assert "returncode" not in payload


def test_warnings_are_untouched():
    payload = _beleg(warnings=["Steuersumme weicht ab"])
    assert apply_returncode_floor(payload)["warnings"] == ["Steuersumme weicht ab"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_returncode.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'core.returncode'`

- [ ] **Step 3: Write the module**

Create `core/returncode.py`:

```python
"""Deterministic returncode floor applied to every extraction result.

The LLM classifies each subdocument (100 = Beleg, 200 = kein Beleg,
300 = nicht lesbar). This module turns that into a contract the consumer can
branch on: the field is always present and always one of those three ints,
even when the model omits it, nulls it, or returns the wrong type.

Fill-in only, by design. An automatic 100 -> 200 downgrade on a real invoice
that merely extracted badly would auto-cancel a legitimate claim. Leaving a
wrong 100 in place reproduces today's behaviour, which a human already handles.
The asymmetry is deliberate: 200 is the expensive direction to be wrong in.

Never derives 300. Distinguishing "unreadable" from "not a Beleg" needs the
model's view of the page; from the extracted fields alone the two are
indistinguishable.
"""
from __future__ import annotations

VALID_RETURNCODES = (100, 200, 300)

GENERIC_REASON = "Das Dokument enthält keinen auswertbaren Beleg."


def _present(value) -> bool:
    """A value the model actually read off the page.

    `0`/`0.0` counts (a zero total is a reading, not a gap); None and
    whitespace-only strings do not.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _has_beleg_evidence(result: dict) -> bool:
    """Any single field that only a Beleg would carry."""
    if _present(result.get("number")) or _present(result.get("issuedAt")):
        return True
    items = result.get("items")
    if isinstance(items, list) and items:
        return True
    totals = result.get("totals")
    if isinstance(totals, dict) and (_present(totals.get("net")) or _present(totals.get("gross"))):
        return True
    return False


def _is_valid_code(value) -> bool:
    # bool is a subclass of int; True must not sneak through as a code.
    return isinstance(value, int) and not isinstance(value, bool) and value in VALID_RETURNCODES


def _coerce_reasons(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry.strip() for entry in value if isinstance(entry, str) and entry.strip()]


def apply_returncode_floor(result: dict) -> dict:
    """Return a copy of `result` with a guaranteed returncode + reasons, first."""
    code = result.get("returncode")
    if not _is_valid_code(code):
        code = 100 if _has_beleg_evidence(result) else 200

    reasons = _coerce_reasons(result.get("returncodeReasons"))
    if code == 100:
        reasons = []
    elif not reasons:
        reasons = [GENERIC_REASON]

    rest = {k: v for k, v in result.items() if k not in ("returncode", "returncodeReasons")}
    return {"returncode": code, "returncodeReasons": reasons, **rest}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_returncode.py -q`
Expected: PASS (all tests)

- [ ] **Step 5: Wire the floor into the pipeline**

In `core/pipeline.py`, add the import immediately after the existing `from core.product import ProductConfig` line (currently line 29 — the last import in the file's header):

```python
from core.returncode import apply_returncode_floor
```

Then change the tail of `_extract_single_subdocument` (currently `core/pipeline.py:333-335`) from:

```python
        if self.product_config.postprocess_extraction is not None:
            result = self.product_config.postprocess_extraction(result)
        return result
```

to:

```python
        if self.product_config.postprocess_extraction is not None:
            result = self.product_config.postprocess_extraction(result)
        # Unconditional and last: all three products need the identical
        # guarantee, and VCC already occupies the postprocess hook. Running
        # after the hook means the floor judges the values the consumer will
        # actually receive.
        return apply_returncode_floor(result)
```

- [ ] **Step 6: Add the composition test**

Append to `tests/core/test_pipeline_postprocess.py`:

```python
def test_pipeline_applies_returncode_floor_after_the_hook(monkeypatch):
    # The floor is unconditional and runs last, so a product with a hook still
    # gets the guaranteed contract — and the hook's output is what it judges.
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    pipe = _make_pipeline(load_product_config())
    processor = _FakeProcessor({"items": [{"qty": None, "unit": None}]})

    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert list(result)[:2] == ["returncode", "returncodeReasons"]
    assert result["returncode"] == 100  # non-empty items are Beleg evidence
    assert result["returncodeReasons"] == []
    assert result["items"][0]["qty"] == 1  # the VCC hook still ran


def test_pipeline_floor_applies_without_a_hook():
    cfg = ProductConfig(
        name="no_hook_test",
        extract_prompt_builder=lambda **kwargs: "prompt",
        extract_output_schema={},
    )
    pipe = _make_pipeline(cfg)
    processor = _FakeProcessor({"type": None, "number": None, "items": []})

    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert result["returncode"] == 200
    assert result["returncodeReasons"] == [GENERIC_REASON]
```

Add to that file's imports:

```python
from core.returncode import GENERIC_REASON
```

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS. The two pre-existing tests in `test_pipeline_postprocess.py` still pass unchanged — the floor only adds keys.

- [ ] **Step 8: Commit**

```bash
git add core/returncode.py core/pipeline.py tests/core/test_returncode.py tests/core/test_pipeline_postprocess.py
git commit -m "feat: guarantee a returncode on every extracted subdocument"
```

---

### Task 2: Always at least one subdocument, and keep the evidence

Two coupled pipeline changes. The fallback (spec 3b) guarantees there is always a subdocument to carry a returncode. The cleanup guard (spec 3c) is a direct consequence: the fallback makes the existing "no subdocuments" guard unreachable, so the evidence-preserving behaviour has to move to a returncode-aware condition.

**Files:**
- Modify: `core/pipeline.py` (`split_document_into_invoices` at line 223; `cleanup_storage_artifacts` at line 362)
- Create: `tests/core/test_pipeline_split_fallback.py`
- Modify: `tests/core/test_pipeline_cleanup.py`

**Interfaces:**
- Consumes: `returncode` as an `int` on each entry of `self.extraction_result_json["subdocuments"]` (Task 1).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing split-fallback tests**

Create `tests/core/test_pipeline_split_fallback.py`:

```python
"""The >=1-subdocument guarantee.

When the analyzer reports no Beleg, the pipeline used to emit an empty
`subdocuments` array — nothing for the consumer to branch on. It now emits one
subdocument spanning every page, which extraction then classifies (almost
always 200). This is what makes "read subdocuments[].returncode" a contract
rather than a usually-works.
"""
from pathlib import Path

import fitz
import pytest

from core.pipeline import Pipeline


class _CollectingStorage:
    def __init__(self):
        self.written = []

    def write_text(self, key, text):
        self.written.append(key)

    def write_bytes(self, key, data, content_type=None):
        self.written.append(key)


@pytest.fixture
def three_page_pdf(tmp_path):
    doc = fitz.open()
    for n in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Seite {n + 1}")
    path = tmp_path / "input.pdf"
    doc.save(path)
    doc.close()
    return path


def _make_pipeline(analysis_dict, pdf_path, work_dir):
    pipe = object.__new__(Pipeline)
    pipe.storage = _CollectingStorage()
    pipe.analysis_dict = analysis_dict
    pipe.file_type = "pdf"
    pipe.local_input_path = str(pdf_path)
    pipe.work_dir = Path(work_dir)
    pipe.output_prefix = "az://invoices/processed-bps"
    pipe.stem = "abc"
    pipe.subdocuments = []
    pipe.markdown_by_page = {1: "seite eins", 2: "seite zwei", 3: "seite drei"}
    return pipe


@pytest.mark.parametrize("analysis", [
    {"invoice_pages": {}},
    {"invoice_pages": None},
    {},  # key absent entirely
])
def test_empty_invoice_pages_yields_one_subdocument_over_all_pages(analysis, three_page_pdf, tmp_path):
    pipe = _make_pipeline(analysis, three_page_pdf, tmp_path)

    pipe.split_document_into_invoices()

    assert len(pipe.subdocuments) == 1
    assert pipe.subdocuments[0].page_numbers == [1, 2, 3]
    assert pipe.subdocuments[0].document_number == 1


def test_non_empty_invoice_pages_is_unaffected(three_page_pdf, tmp_path):
    pipe = _make_pipeline({"invoice_pages": {"R-1": [1], "R-2": [2, 3]}}, three_page_pdf, tmp_path)

    pipe.split_document_into_invoices()

    assert [sd.page_numbers for sd in pipe.subdocuments] == [[1], [2, 3]]
    assert pipe._invoice_key_to_doc_number == {"R-1": 1, "R-2": 2}


def test_no_pages_at_all_produces_no_subdocument(three_page_pdf, tmp_path):
    # Nothing to render; the fallback must not crash trying to build an image
    # out of zero pages.
    pipe = _make_pipeline({"invoice_pages": {}}, three_page_pdf, tmp_path)
    pipe.markdown_by_page = {}

    pipe.split_document_into_invoices()

    assert pipe.subdocuments == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_split_fallback.py -q`
Expected: FAIL — `KeyError: 'invoice_pages'` on the absent-key case, and `assert 0 == 1` on the empty cases.

- [ ] **Step 3: Add the fallback**

In `core/pipeline.py:split_document_into_invoices`, replace:

```python
        self._invoice_key_to_doc_number = {}

        with fitz.open(self.local_input_path) as doc:
            for seq_idx, (invoice_key, page_numbers) in enumerate(self.analysis_dict["invoice_pages"].items(), start=1):
```

with:

```python
        self._invoice_key_to_doc_number = {}

        invoice_pages = self.analysis_dict.get("invoice_pages") or {}
        if not invoice_pages:
            # The analyzer found no Beleg. Emit one subdocument spanning every
            # page instead of an empty result: extraction then classifies it
            # (200, almost always), so the consumer always has a returncode to
            # read. An empty `subdocuments` array is not something they can
            # branch on, which is the whole problem this feature fixes.
            all_pages = sorted(self.markdown_by_page)
            if all_pages:
                invoice_pages = {"1": all_pages}
                _telemetry.warning(
                    "split_no_invoice_pages_fallback",
                    reason="analyze returned no invoice_pages — emitting one subdocument over all pages",
                    page_count=len(all_pages),
                )
            else:
                # No OCR'd pages at all: there is nothing to render into a
                # subdocument. Bail out rather than crash on an empty image list.
                _telemetry.warning(
                    "split_no_pages_at_all",
                    reason="no OCR pages available — cannot emit a fallback subdocument",
                )
                return

        with fitz.open(self.local_input_path) as doc:
            for seq_idx, (invoice_key, page_numbers) in enumerate(invoice_pages.items(), start=1):
```

- [ ] **Step 4: Run the split tests**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_split_fallback.py -q`
Expected: PASS

- [ ] **Step 5: Write the failing cleanup tests**

In `tests/core/test_pipeline_cleanup.py`, first update the shared factory so every existing test still describes a job that produced a real Beleg. Replace:

```python
def _make_pipeline(storage, subdoc_count=2):
    pipe = object.__new__(Pipeline)
    pipe.storage = storage
    pipe.file_key = "az://invoices/uploads-bps/abc.pdf"
    pipe.subdocuments = [_subdoc(n) for n in range(1, subdoc_count + 1)]
    pipe.output_prefix = "az://invoices/processed-bps/"
    pipe.stem = "abc"
    return pipe
```

with:

```python
def _make_pipeline(storage, subdoc_count=2, returncodes=None):
    """Default: every subdocument came back 100 (a normal, cleanable job).

    Pass `returncodes` to describe a job where no Beleg was found — cleanup
    must then keep the artifacts.
    """
    pipe = object.__new__(Pipeline)
    pipe.storage = storage
    pipe.file_key = "az://invoices/uploads-bps/abc.pdf"
    pipe.subdocuments = [_subdoc(n) for n in range(1, subdoc_count + 1)]
    pipe.output_prefix = "az://invoices/processed-bps/"
    pipe.stem = "abc"
    if returncodes is None:
        returncodes = [100] * subdoc_count
    pipe.extraction_result_json = {
        "number_of_subdocuments": len(returncodes),
        "subdocuments": [{"returncode": rc, "returncodeReasons": []} for rc in returncodes],
    }
    return pipe
```

Then append these tests to the same file:

```python
@pytest.mark.parametrize("returncodes", [[200], [300], [200, 300], [200, 200]])
def test_no_beleg_keeps_everything_and_warns(monkeypatch, returncodes):
    # The case this feature exists for: a Sammeldokument with no invoice in it.
    # It now arrives as one or more subdocuments classified 200/300 rather than
    # as zero subdocuments, so the old guard would never fire. Keep the evidence.
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage, subdoc_count=len(returncodes), returncodes=returncodes)

    warnings = []
    monkeypatch.setattr(
        "core.pipeline._telemetry.warning",
        lambda event, **kw: warnings.append((event, kw)),
    )

    assert pipe.cleanup_storage_artifacts() == 0
    assert storage.deleted == []
    assert [event for event, _ in warnings] == ["artifact_cleanup_skipped_no_beleg"]
    assert warnings[0][1]["file_key"] == pipe.file_key


def test_one_beleg_among_rejects_still_cleans_up():
    # A mixed bundle is a normal, successful job — the Beleg was extracted.
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage, subdoc_count=3, returncodes=[200, 100, 300])

    assert pipe.cleanup_storage_artifacts() == 10  # 1 upload + 3 subdocs x 3


def test_missing_extraction_result_keeps_everything():
    # Defensive: cleanup running before extraction stored its result means
    # something went wrong. Never destroy the upload on that path.
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage, subdoc_count=1)
    del pipe.extraction_result_json

    assert pipe.cleanup_storage_artifacts() == 0
    assert storage.deleted == []
```

- [ ] **Step 6: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_pipeline_cleanup.py -q`
Expected: the three new tests FAIL (cleanup still deletes everything: `assert 7 == 0`). The pre-existing tests still pass — the factory change only adds an attribute.

- [ ] **Step 7: Extend the cleanup guard**

In `core/pipeline.py:cleanup_storage_artifacts`, immediately after the existing `if not self.subdocuments:` block (which ends `return 0` at line 392), insert:

```python
        # Task 3b guarantees at least one subdocument, so the guard above is now
        # only reachable when OCR produced no pages at all. The case a human
        # actually needs to reproduce — a Sammeldokument with no Beleg in it —
        # now arrives as subdocuments classified 200/300. Keep those artifacts
        # too; the result JSON alone doesn't show what the pages looked like.
        # Distinct event name so alerting can tell this apart from the
        # zero-subdocument case above.
        extraction = getattr(self, "extraction_result_json", None) or {}
        subdoc_results = extraction.get("subdocuments") or []
        if not any(isinstance(sd, dict) and sd.get("returncode") == 100 for sd in subdoc_results):
            _telemetry.warning(
                "artifact_cleanup_skipped_no_beleg",
                reason="no subdocument classified 100 — keeping artifacts for investigation",
                file_key=self.file_key,
            )
            return 0
```

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add core/pipeline.py tests/core/test_pipeline_split_fallback.py tests/core/test_pipeline_cleanup.py
git commit -m "feat: always emit at least one subdocument, keep artifacts when no Beleg found"
```

---

### Task 3: LLM classification — extraction prompts and schemas

Teaches all three products' extraction prompts to classify, and publishes the two fields in the schemas that feed Swagger via `custom_openapi()`.

**Files:**
- Modify: `products/bps/extract_prompt.py`, `products/sanierer/extract_prompt.py`, `products/vetcostcheck/extract_prompt.py`
- Modify: `products/bps/extract_schema.json`, `products/sanierer/extract_schema.json`, `products/vetcostcheck/extract_schema.json`
- Create: `tests/products/test_returncode_contract.py`

**Interfaces:**
- Consumes: the code semantics fixed in Task 1 (`core.returncode.VALID_RETURNCODES`).
- Produces: nothing later tasks depend on.

**Critical:** the load-bearing sentence is *"Ein Beleg bleibt 100, auch wenn einzelne Felder nicht ermittelbar sind."* Without it the model reaches for 200 whenever extraction goes badly, and a false 200 auto-cancels a legitimate claim. It must appear in all three prompts.

- [ ] **Step 1: Write the failing contract test**

Create `tests/products/test_returncode_contract.py`:

```python
"""The returncode contract is identical across all three products.

Product wording differs (what counts as "not a Beleg" is different for a
Handwerkerrechnung and a Tierarztrechnung) but the codes, the schema and the
anti-downgrade instruction must not drift apart between products.
"""
import pytest

from core.product import load_product_config

PRODUCTS = ["vetcostcheck", "bps", "sanierer"]


@pytest.fixture(params=PRODUCTS)
def config(request, monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", request.param)
    return load_product_config()


def test_prompt_teaches_all_three_codes(config):
    prompt = config.extract_prompt_builder(ocr_text="x", subdocument_context=None)
    assert "returncode" in prompt
    assert "returncodeReasons" in prompt
    for code in ("100", "200", "300"):
        assert code in prompt


def test_prompt_carries_the_anti_downgrade_sentence(config):
    # The single most important line in this feature: without it the model
    # emits 200 whenever extraction went badly, which auto-cancels real claims.
    prompt = config.extract_prompt_builder(ocr_text="x", subdocument_context=None)
    assert "Ein Beleg bleibt 100, auch wenn einzelne Felder nicht ermittelbar sind" in prompt


def test_schema_publishes_both_fields_first(config):
    props = config.extract_output_schema["properties"]
    assert list(props)[:2] == ["returncode", "returncodeReasons"]
    assert props["returncode"]["enum"] == [100, 200, 300]
    assert props["returncode"]["type"] == "integer"
    assert props["returncodeReasons"]["type"] == "array"
    assert props["returncodeReasons"]["items"] == {"type": "string"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/products/test_returncode_contract.py -q`
Expected: FAIL — 9 failures (3 products × 3 tests), `assert 'returncode' in prompt`.

- [ ] **Step 3: Add the BPS rule**

In `products/bps/extract_prompt.py`, insert after rule 22 (the `tradeType` rule ending `...Maler/Lackierer=PAINTER.\n\n"`). Change that line's trailing `\n\n` to `\n`, then add:

```python
"23. Klassifizierung ('returncode'): Entscheide ZUERST, ob dieses Dokument überhaupt ein Beleg ist.\n"
"    100 = Rechnung oder Angebot/Kostenvoranschlag eines Handwerkers oder Dienstleisters. Ein Beleg bleibt 100, auch wenn einzelne Felder nicht ermittelbar sind — eine unvollständige oder schwierige Extraktion ist KEIN Grund für 200.\n"
"    200 = lesbar, aber kein Beleg: reines Anschreiben, weiterleitende E-Mail, Schadenmeldung, Foto, Datenschutzhinweis, Anlagenverzeichnis, Versicherungskorrespondenz o. Ä.\n"
"    300 = der Inhalt konnte überhaupt nicht gelesen werden (leere, unlesbare oder vollständig verrauschte Seiten).\n"
"    Bei 200 und 300: begründe in 'returncodeReasons' mit 1-3 kurzen deutschen Sätzen, warum kein Beleg vorliegt bzw. warum das Dokument nicht lesbar ist. Bei 100: leeres Array [].\n"
"    Im Zweifel zwischen 100 und 200 wähle 100. 300 hat Vorrang vor 200.\n\n"
```

Then in the same file's `JSON-Ziel-Schema` block, insert immediately after the opening `"{\n"` line and before `"\"type\": ...`:

```python
"\"returncode\": 100,\n"
"\"returncodeReasons\": [\"string\"],\n"
```

- [ ] **Step 4: Add the Sanierer rule**

In `products/sanierer/extract_prompt.py`, insert after rule 19 (ending `...nicht deduplizieren.\n\n"`). Change that line's trailing `\n\n` to `\n`, then add:

```python
"20. Klassifizierung ('returncode'): Entscheide ZUERST, ob dieses Dokument überhaupt ein Beleg ist.\n"
"    100 = Rechnung oder Angebot/Kostenvoranschlag über Sanierungs- oder Handwerkerleistungen. Ein Beleg bleibt 100, auch wenn einzelne Felder nicht ermittelbar sind — eine unvollständige oder schwierige Extraktion ist KEIN Grund für 200.\n"
"    200 = lesbar, aber kein Beleg: reines Anschreiben, weiterleitende E-Mail, Schadenmeldung, Auftragsbestätigung ohne Positionen, Foto, Datenschutzhinweis, Anlagenverzeichnis o. Ä.\n"
"    300 = der Inhalt konnte überhaupt nicht gelesen werden (leere, unlesbare oder vollständig verrauschte Seiten).\n"
"    Bei 200 und 300: begründe in 'returncodeReasons' mit 1-3 kurzen deutschen Sätzen, warum kein Beleg vorliegt bzw. warum das Dokument nicht lesbar ist. Bei 100: leeres Array [].\n"
"    Im Zweifel zwischen 100 und 200 wähle 100. 300 hat Vorrang vor 200.\n\n"
```

And the same two lines at the top of its `JSON-Ziel-Schema` block:

```python
"\"returncode\": 100,\n"
"\"returncodeReasons\": [\"string\"],\n"
```

- [ ] **Step 5: Add the vetcostcheck rule**

In `products/vetcostcheck/extract_prompt.py`, insert after rule 17 (the `diagnoses` rule ending `...leeres Array [].\n\n"`). Change that line's trailing `\n\n` to `\n`, then add:

```python
"18. Klassifizierung ('returncode'): Entscheide ZUERST, ob dieses Dokument überhaupt ein Beleg ist.\n"
"    100 = Tierarztrechnung, Quittung oder Verschreibung. Ein Beleg bleibt 100, auch wenn einzelne Felder nicht ermittelbar sind — eine unvollständige oder schwierige Extraktion ist KEIN Grund für 200.\n"
"    200 = lesbar, aber kein Beleg: reines Anschreiben, weiterleitende E-Mail, Impfpass ohne Abrechnung, Foto, Datenschutzhinweis, Versicherungskorrespondenz o. Ä.\n"
"    300 = der Inhalt konnte überhaupt nicht gelesen werden (leere, unlesbare oder vollständig verrauschte Seiten).\n"
"    Bei 200 und 300: begründe in 'returncodeReasons' mit 1-3 kurzen deutschen Sätzen, warum kein Beleg vorliegt bzw. warum das Dokument nicht lesbar ist. Bei 100: leeres Array [].\n"
"    Im Zweifel zwischen 100 und 200 wähle 100. 300 hat Vorrang vor 200.\n\n"
```

And the same two lines at the top of its `JSON-Ziel-Schema` block, before `"\"type\": \"invoice|receipt|prescription|null\",\n"`:

```python
"\"returncode\": 100,\n"
"\"returncodeReasons\": [\"string\"],\n"
```

- [ ] **Step 6: Add the schema properties**

In each of `products/vetcostcheck/extract_schema.json`, `products/bps/extract_schema.json` and `products/sanierer/extract_schema.json`, insert these two entries as the **first** members of the top-level `"properties"` object, immediately before the existing `"type"` entry:

```json
    "returncode": {"type": "integer", "enum": [100, 200, 300], "description": "Klassifizierung des Subdokuments: 100=Beleg (Rechnung/Angebot), 200=kein Beleg, 300=nicht lesbar. Always present."},
    "returncodeReasons": {"type": "array", "items": {"type": "string"}, "description": "German reasons for a 200 or 300. Empty for 100."},
```

- [ ] **Step 7: Run the contract test and the full suite**

Run: `.venv/bin/python -m pytest tests/products/test_returncode_contract.py -q && .venv/bin/python -m pytest tests/ -q`
Expected: PASS. If a prompt builder raises `KeyError` or `IndexError`, a literal brace was added to a `.format()`ed template without doubling it — but note the *extract* prompts are f-strings/concatenation, not `.format()`, so this only bites in Task 4.

- [ ] **Step 8: Commit**

```bash
git add products/*/extract_prompt.py products/*/extract_schema.json tests/products/test_returncode_contract.py
git commit -m "feat: teach all three extraction prompts and schemas the returncode contract"
```

---

### Task 4: Stop the splitter inventing Belege

The actual root cause of the reference case: Frage 2 asks how many Belege the document contains, but never says that zero is a legal answer. The model therefore invented two subdocuments out of one cover email.

**Files:**
- Modify: `products/bps/analyze_overrides.py:43`, `products/sanierer/analyze_overrides.py:45`, `products/vetcostcheck/analyze_overrides.py:30`
- Modify: `tests/products/test_returncode_contract.py` (add the analyze assertions)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

**Critical:** all three `_ANALYZE_PROMPT_TEMPLATE`s are rendered with `str.format()`. The literal `{}` in the new text **must** be written `{{}}` or `build_analyze_prompt` raises `IndexError` at runtime. The existing Frage 3 lines already do this — follow them.

- [ ] **Step 1: Write the failing test**

Append to `tests/products/test_returncode_contract.py`:

```python
def test_analyze_prompt_allows_zero_belege(config):
    # The splitter used to invent Belege out of cover emails because the prompt
    # never said 0 was allowed — the schema permits it, but the model never
    # sees the schema.
    prompt = config.analyze_prompt_builder(markdown_text="--- PAGE 1 --- x")
    assert "'number_of_invoices': 0" in prompt
    assert "Erfinde keine" in prompt
    # Rendered, not template: .format() must have consumed the doubled braces.
    assert "{{" not in prompt


def test_analyze_schema_still_permits_zero(config):
    assert config.analyze_output_schema["properties"]["number_of_invoices"]["minimum"] == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/products/test_returncode_contract.py -q`
Expected: FAIL — 3 failures on `assert "'number_of_invoices': 0" in prompt`. `test_analyze_schema_still_permits_zero` already passes (all three schemas set `minimum: 0` today) — it is there to keep the schema and the prompt from drifting apart again.

- [ ] **Step 3: Update the BPS analyze prompt**

In `products/bps/analyze_overrides.py`, replace:

```python
    "Frage 2: Wie viele unabhängige Belege enthält das Dokument? Output: 'number_of_invoices': <number>.\n"
```

with:

```python
    "Frage 2: Wie viele unabhängige Belege enthält das Dokument? Output: 'number_of_invoices': <number>. "
    "0 ist eine gültige Antwort: Wenn das Dokument gar keinen Beleg enthält — etwa nur ein Anschreiben, "
    "eine weiterleitende E-Mail, eine Schadenmeldung, Fotos oder Datenschutzhinweise — gib "
    "'number_of_invoices': 0 und 'invoice_pages': {{}} zurück. Erfinde keine Belege.\n"
```

- [ ] **Step 4: Update the Sanierer analyze prompt**

In `products/sanierer/analyze_overrides.py`, replace:

```python
    "Frage 2: Wie viele unabhängige Belege enthält das Dokument? Output: 'number_of_invoices': <number>.\n"
```

with:

```python
    "Frage 2: Wie viele unabhängige Belege enthält das Dokument? Output: 'number_of_invoices': <number>. "
    "0 ist eine gültige Antwort: Wenn das Dokument gar keinen Beleg enthält — etwa nur ein Anschreiben, "
    "eine weiterleitende E-Mail, eine Schadenmeldung, Fotos oder Datenschutzhinweise — gib "
    "'number_of_invoices': 0 und 'invoice_pages': {{}} zurück. Erfinde keine Belege.\n"
```

- [ ] **Step 5: Update the vetcostcheck analyze prompt**

In `products/vetcostcheck/analyze_overrides.py`, replace:

```python
    "Frage 2: Wie viele unabhängige Rechnungen enthält das Dokument? Ouput: 'number_of_invoices': <number>.\n"
```

with:

```python
    "Frage 2: Wie viele unabhängige Rechnungen enthält das Dokument? Output: 'number_of_invoices': <number>. "
    "0 ist eine gültige Antwort: Wenn das Dokument gar keine Rechnung, Quittung oder Verschreibung enthält — "
    "etwa nur ein Anschreiben, eine weiterleitende E-Mail, Fotos oder Datenschutzhinweise — gib "
    "'number_of_invoices': 0 und 'invoice_pages': {{}} zurück. Erfinde keine Rechnungen.\n"
```

(This also fixes the `Ouput` typo in the original line.)

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add products/*/analyze_overrides.py tests/products/test_returncode_contract.py
git commit -m "fix: tell all three analyze prompts that zero Belege is a valid answer"
```

---

### Task 5: Regression sweep script

The spec's regression gate — *every real Beleg must come back 100* — is the one that matters, because a false 200 auto-cancels a legitimate claim. It needs live LLM calls, so it cannot live in pytest. This is the script the maintainer runs against the corpora before the branch is promoted.

**Files:**
- Create: `scripts/returncode_sweep.py`

**Interfaces:**
- Consumes: `core.jobs.tasks.process_file` and `core.storage.file_storage.save_upload`, exactly as `scripts/extract_local.py` and `scripts/regression_check.py` already call them.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the script**

Create `scripts/returncode_sweep.py`:

```python
"""Sweep a directory of PDFs and report the returncode of every subdocument.

The regression gate for the returncode feature: every PDF that IS a Beleg must
come back 100. A false 200 auto-cancels a legitimate claim, so this direction
of error is the expensive one — `--expect 100` makes it a hard failure.

Usage:
    PRODUCT_NAME=bps STORAGE_BACKEND=local \\
        .venv/bin/python scripts/returncode_sweep.py bps_sanierer_input/BPS_Input --expect 100

    # Documents that genuinely contain no Beleg:
    PRODUCT_NAME=bps STORAGE_BACKEND=local \\
        .venv/bin/python scripts/returncode_sweep.py bps_sanierer_input --expect 200

Exit code is 0 when every subdocument matched `--expect` (or when `--expect`
was omitted), 1 otherwise. Logs go to stderr; the table goes to stdout.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Must be set before importing anything that calls load_dotenv at import time.
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("LOCAL_STORAGE_BASE_DIR", str(REPO_ROOT / "temp"))
# Keep the artifacts: a surprising returncode is exactly what you want to look at.
os.environ["CLEANUP_ARTIFACTS"] = "false"

sys.path.insert(0, str(REPO_ROOT))

import structlog  # noqa: E402

structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def sweep_one(pdf: Path) -> list[dict]:
    """Run the full pipeline on one PDF; return its subdocument results."""
    from core.storage.file_storage import save_upload
    from core.jobs.tasks import process_file

    file_id = save_upload(pdf.read_bytes(), original_filename=pdf.name)
    result = process_file(file_id)
    return result.get("subdocuments", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="a PDF, or a directory of PDFs (non-recursive)")
    parser.add_argument("--expect", type=int, choices=[100, 200, 300],
                        help="fail the run if any subdocument differs from this code")
    args = parser.parse_args()

    if args.target.is_dir():
        pdfs = sorted(p for p in args.target.glob("*.pdf"))
    elif args.target.is_file():
        pdfs = [args.target]
    else:
        print(f"No such file or directory: {args.target}", file=sys.stderr)
        return 2

    if not pdfs:
        print(f"No PDFs found in {args.target}", file=sys.stderr)
        return 2

    print(f"{'PDF':<44} {'#':>3} {'code':>5}  reasons")
    print("-" * 100)

    failures = []
    for pdf in pdfs:
        try:
            subdocs = sweep_one(pdf)
        except Exception as exc:  # a crashed PDF is a sweep result, not a stop
            print(f"{pdf.name:<44} {'-':>3} {'ERR':>5}  {exc}")
            failures.append((pdf.name, None, f"pipeline raised: {exc}"))
            continue

        if not subdocs:
            # Task 2 makes this impossible for a PDF with any readable page.
            print(f"{pdf.name:<44} {'-':>3} {'none':>5}  no subdocuments returned")
            failures.append((pdf.name, None, "no subdocuments"))
            continue

        for idx, sd in enumerate(subdocs, start=1):
            code = sd.get("returncode")
            reasons = "; ".join(sd.get("returncodeReasons") or [])
            print(f"{pdf.name:<44} {idx:>3} {str(code):>5}  {reasons}")
            if args.expect is not None and code != args.expect:
                failures.append((pdf.name, idx, f"expected {args.expect}, got {code}"))

    print("-" * 100)
    if not failures:
        print(f"OK — {len(pdfs)} PDF(s) swept" + (f", all subdocuments {args.expect}" if args.expect else ""))
        return 0

    print(f"FAILED — {len(failures)} mismatch(es):")
    for name, idx, why in failures:
        where = f"{name} subdoc {idx}" if idx is not None else name
        print(f"  {where}: {why}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify the script's plumbing without spending LLM calls**

Run: `.venv/bin/python scripts/returncode_sweep.py --help`
Expected: the usage text prints, exit 0.

Run: `.venv/bin/python scripts/returncode_sweep.py /nonexistent`
Expected: `No such file or directory: /nonexistent`, exit 2.

- [ ] **Step 3: Commit**

```bash
git add scripts/returncode_sweep.py
git commit -m "feat: add returncode corpus sweep for the regression gate"
```

- [ ] **Step 4: Hand the sweep commands to the maintainer**

These need API keys and cost real LLM calls, so they are **not** run by the implementer. Report them in the task report for the maintainer to run before promotion:

```bash
# Must all come back 100 — this is the gate that matters.
PRODUCT_NAME=bps STORAGE_BACKEND=local .venv/bin/python \
    scripts/returncode_sweep.py bps_sanierer_input/BPS_Input --expect 100
PRODUCT_NAME=sanierer STORAGE_BACKEND=local .venv/bin/python \
    scripts/returncode_sweep.py bps_sanierer_input/Sanierer_Input --expect 100
PRODUCT_NAME=vetcostcheck STORAGE_BACKEND=local .venv/bin/python \
    scripts/returncode_sweep.py 3C_testdaten_pdf --expect 100

# Acceptance: the reference document that started this. Expect exactly one
# subdocument, returncode 200, with non-empty reasons.
PRODUCT_NAME=bps STORAGE_BACKEND=local .venv/bin/python \
    scripts/returncode_sweep.py bps_sanierer_input/null_example_pdf.pdf --expect 200
```

> `bps_sanierer_input/` is gitignored — it exists only on the maintainer's machine.

---

### Task 6: Document the contract for 3C's developers

**Files:**
- Modify: `vetcostcheck_api_doc.md`

**Interfaces:**
- Consumes: the final field semantics from Tasks 1 and 3.
- Produces: nothing.

BPS and Sanierer have no equivalent consumer document — they are covered by Swagger via their `extract_schema.json`, updated in Task 3.

- [ ] **Step 1: Add both fields to the example result**

In `vetcostcheck_api_doc.md`, in the `### When finished:` block, change:

```json
    "subdocuments": [
      {
        "type": "invoice",
```

to:

```json
    "subdocuments": [
      {
        "returncode": 100,
        "returncodeReasons": [],
        "type": "invoice",
```

- [ ] **Step 2: Document the codes**

Insert a new section immediately after the `## Job Status Values` section and before `## Health Check`:

```markdown
---

## Subdocument Return Codes

Every entry in `subdocuments` carries a `returncode`. It is **always present**
and is **always** one of the three values below — branch on it rather than on
whether individual fields came back null.

| Code | Meaning | What to do |
|------|---------|------------|
| `100` | The subdocument is an invoice, receipt or prescription | Process it normally |
| `200` | Readable, but not an invoice — a cover letter, e-mail, photo, privacy notice, … | Do not open a Vorgang for it |
| `300` | The content could not be read at all | Needs a human |

`returncodeReasons` is a list of short German sentences explaining a `200` or
`300`. It is empty for `100`, and non-empty whenever the code is `200` or
`300`, so it can be pasted straight into a Storno note.

Two things worth knowing:

- **A `100` does not promise complete extraction.** A genuine invoice stays
  `100` even when individual fields could not be read — those caveats go in
  `warnings`, which keeps its existing meaning and is separate from
  `returncodeReasons`.
- **`subdocuments` is never empty.** A PDF that contains no invoice at all
  returns exactly one subdocument with `returncode: 200` rather than an empty
  array, so there is always a code to read.
```

- [ ] **Step 3: Commit**

```bash
git add vetcostcheck_api_doc.md
git commit -m "docs: document subdocument return codes for API consumers"
```

---

## Deferred to the maintainer (not tasks)

- **Regression sweep + acceptance run** — the commands in Task 5 Step 4. Needs API keys and costs real LLM calls.
- **Deploy to the test tier only**: `./deploy.sh all <tag> test` with a unique tag. Do **not** run `scripts/promote.sh` — the PO's team adapts against test first.
- Tell 3C's developers that VCC's behaviour changes visibly: a result that is today `{"number_of_subdocuments": 0, "subdocuments": []}` becomes one subdocument with `returncode: 200`.
