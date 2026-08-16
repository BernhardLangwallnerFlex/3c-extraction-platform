# Content-Policy Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Azure OpenAI's content safety filter rejects a document's images, extract it from OCR text instead of failing it — and stop retrying errors that can never succeed.

**Architecture:** A new `core/llm_errors.py` holds the error classification and a vision-fallback wrapper. Both LLM call sites swap their blanket tenacity retry for a predicate that treats 4xx (except 408/429) as permanent, and route their call through the fallback. Degradation is reported to the consumer in a new `qualityFlags` array and in German prose in `warnings`.

**Tech Stack:** Python 3.11, pytest 9.x, tenacity, structlog, sentry-sdk, openai (Azure client).

**Spec:** `docs/superpowers/specs/2026-08-14-content-policy-fallback-design.md` — read before Task 1.

## Global Constraints

- **Retry means "the same request might succeed".** Any 4xx is permanent **except 408 and 429**. 5xx, connection errors, and anything without a status code stay retryable — fail toward today's behaviour when the error is unrecognised.
- **The vision fallback sits OUTSIDE the tenacity decorator.** It sends a *different* request; nesting it inside the retry would re-attempt the stripped call three times and conflate the two concepts.
- **`qualityFlags` is always present**, an array of ASCII tokens from the closed set `VISION_DROPPED`, `SINGLE_ENGINE_OCR`, empty when nothing degraded, with **no duplicates**.
- **Key order on every subdocument is `returncode`, `returncodeReasons`, `qualityFlags`, then everything else.**
- **Never fold degradation into `returncode`.** `returncode` answers "is this a Beleg?"; `qualityFlags` answers "how well did we read it?". Conflating them lets a badly-read invoice look like a non-invoice, which auto-cancels a real claim.
- **Extraction is multi-threaded.** `extract_data_from_subdocuments` runs subdocuments in a `ThreadPoolExecutor` sharing **one** processor instance. A degradation signal must never be stored on the processor or any other shared object — it rides on the per-call result dict.
- `warnings` keeps its existing meaning and is not otherwise modified.
- Test command: `.venv/bin/python -m pytest tests/ -q`. **167 passing** at the start of this plan; green at the end of every task.
- No linter. Match surrounding style: `from __future__ import annotations`, double quotes, structlog via module-level `log` / `_telemetry`.
- Deploy is **not** part of this plan. This ships with the returncode release once 3C approves; do not deploy or promote.

---

## File Structure

**Created:**
- `core/llm_errors.py` — error classification and the vision-fallback wrapper. Pure logic, no product knowledge, no I/O of its own.
- `tests/core/test_llm_errors.py` — unit tests for the above.
- `tests/core/test_vision_fallback_pipeline.py` — pipeline-level tests for flag and warning propagation.

**Modified:**
- `core/pipeline.py` — retry predicate on `_call_analyze_llm`; fallback in `analyze_document`; `analyze_vision_dropped` class attribute; flags and warnings in `_extract_single_subdocument`.
- `core/processors/azure_processor.py` — retry predicate on `_call_openai`; fallback in `extract`; `_vision_dropped` marker on the returned dict.
- `core/ocr/ocr_dual.py` — `single_engine_fallback` attribute.
- `core/jobs/tasks.py` — suppress RQ retries for permanent errors.
- `products/{vetcostcheck,bps,sanierer}/extract_schema.json` — `qualityFlags`.
- `tests/products/test_returncode_contract.py` — the first-two-keys assertion becomes first-three.
- `vetcostcheck_api_doc.md` — document `qualityFlags`.

---

### Task 1: Error classification and the vision fallback

The whole feature's logic, in one file with no dependencies on the pipeline.

**Files:**
- Create: `core/llm_errors.py`
- Create: `tests/core/test_llm_errors.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `is_retryable(exc: Exception) -> bool`
  - `is_content_policy_rejection(exc: Exception) -> bool`
  - `strip_image_blocks(blocks: list) -> list`
  - `call_with_vision_fallback(call_fn, client, model, blocks) -> tuple[Any, bool]` — returns `(response, vision_dropped)`
  - `RETRYABLE_STATUS_CODES: frozenset[int]`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_llm_errors.py`:

```python
"""Error classification and the content-policy vision fallback.

Two concerns that look alike and must stay apart: RETRY asks "might the same
request succeed?", FALLBACK asks "is there a different request worth sending?".
"""
import pytest

from core.llm_errors import (
    call_with_vision_fallback,
    is_content_policy_rejection,
    is_retryable,
    strip_image_blocks,
)


class FakeAPIError(Exception):
    """Shaped like openai.APIStatusError: carries status_code and body."""

    def __init__(self, status_code, body=None, message=""):
        super().__init__(message or f"Error code: {status_code}")
        self.status_code = status_code
        self.body = body


def _content_policy_body():
    """The exact body Azure returned in the 2026-08-14 production failure."""
    return {"error": {
        "message": "Your input image may contain content that is not allowed by our content safety system.",
        "type": "invalid_request_error",
        "param": None,
        "code": "content_policy_violation",
    }}


TEXT = {"type": "text", "text": "prompt"}
IMG1 = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA", "detail": "low"}}
IMG2 = {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB", "detail": "low"}}


# --- is_retryable ---------------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_client_errors_are_permanent(status):
    assert is_retryable(FakeAPIError(status)) is False


@pytest.mark.parametrize("status", [408, 429])
def test_timeout_and_rate_limit_stay_retryable(status):
    # These are the two 4xx that genuinely are transient.
    assert is_retryable(FakeAPIError(status)) is True


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_stay_retryable(status):
    assert is_retryable(FakeAPIError(status)) is True


def test_errors_without_a_status_code_stay_retryable():
    # Connection reset, read timeout, anything unlabelled: fail toward the
    # behaviour we had before this change.
    assert is_retryable(ConnectionError("connection reset by peer")) is True
    assert is_retryable(Exception("something odd")) is True


def test_a_non_integer_status_code_stays_retryable():
    assert is_retryable(FakeAPIError("nonsense")) is True


# --- is_content_policy_rejection ------------------------------------------

def test_recognises_the_production_rejection():
    exc = FakeAPIError(400, body=_content_policy_body())
    assert is_content_policy_rejection(exc) is True


def test_recognises_it_from_the_message_when_code_is_absent():
    # Azure has not always populated `code`; the message is the fallback.
    exc = FakeAPIError(400, body={"error": {"message": "flagged by our content safety system"}})
    assert is_content_policy_rejection(exc) is True


def test_other_400s_are_not_content_policy():
    exc = FakeAPIError(400, body={"error": {"message": "invalid model", "code": "model_not_found"}})
    assert is_content_policy_rejection(exc) is False


def test_non_400_is_never_content_policy():
    assert is_content_policy_rejection(FakeAPIError(500, body=_content_policy_body())) is False


def test_content_policy_rejections_are_not_retryable():
    # The two predicates must agree: re-sending identical content is pointless.
    assert is_retryable(FakeAPIError(400, body=_content_policy_body())) is False


# --- strip_image_blocks ---------------------------------------------------

def test_strip_removes_images_and_keeps_text_in_order():
    assert strip_image_blocks([TEXT, IMG1, IMG2]) == [TEXT]


def test_strip_leaves_a_text_only_list_untouched():
    assert strip_image_blocks([TEXT]) == [TEXT]


def test_strip_does_not_mutate_its_input():
    blocks = [TEXT, IMG1]
    strip_image_blocks(blocks)
    assert blocks == [TEXT, IMG1]


# --- call_with_vision_fallback --------------------------------------------

def test_clean_call_reports_no_degradation_and_calls_once():
    calls = []

    def call_fn(client, model, blocks):
        calls.append(blocks)
        return "response"

    response, dropped = call_with_vision_fallback(call_fn, None, "m", [TEXT, IMG1])

    assert response == "response"
    assert dropped is False
    assert len(calls) == 1


def test_content_policy_triggers_one_text_only_retry():
    calls = []

    def call_fn(client, model, blocks):
        calls.append(blocks)
        if len(calls) == 1:
            raise FakeAPIError(400, body=_content_policy_body())
        return "response"

    response, dropped = call_with_vision_fallback(call_fn, None, "m", [TEXT, IMG1, IMG2])

    assert response == "response"
    assert dropped is True
    assert len(calls) == 2
    assert calls[1] == [TEXT]  # second attempt carried no images


def test_rejection_on_the_text_only_call_propagates():
    def call_fn(client, model, blocks):
        raise FakeAPIError(400, body=_content_policy_body())

    with pytest.raises(FakeAPIError):
        call_with_vision_fallback(call_fn, None, "m", [TEXT, IMG1])


def test_other_errors_propagate_without_a_second_call():
    calls = []

    def call_fn(client, model, blocks):
        calls.append(blocks)
        raise FakeAPIError(500)

    with pytest.raises(FakeAPIError):
        call_with_vision_fallback(call_fn, None, "m", [TEXT, IMG1])
    assert len(calls) == 1


def test_a_text_only_request_is_not_retried():
    # Nothing to strip means the second attempt would be byte-identical.
    calls = []

    def call_fn(client, model, blocks):
        calls.append(blocks)
        raise FakeAPIError(400, body=_content_policy_body())

    with pytest.raises(FakeAPIError):
        call_with_vision_fallback(call_fn, None, "m", [TEXT])
    assert len(calls) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_llm_errors.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'core.llm_errors'`

- [ ] **Step 3: Write the module**

Create `core/llm_errors.py`:

```python
"""Classifying Azure OpenAI errors, and recovering from content-policy rejections.

Two concerns that look alike and must not be conflated:

  * RETRY answers "might the same request succeed if I send it again?" — true
    for 5xx, connection errors and rate limits; false for a 400, which
    describes something wrong with the request itself.
  * FALLBACK answers "is there a different request worth sending?" — used when
    the content safety filter rejects a document's images, where re-sending
    identical content can only be rejected identically.

Background: on 2026-08-14 a BPS document failed in production because Azure's
content filter false-positived one page of water-damage photos. The blanket
`stop_after_attempt(3)` retried that permanent 400 three times, RQ then retried
the whole job twice more, and each attempt re-ran both OCR engines.
"""
from __future__ import annotations

import sentry_sdk
import structlog

log = structlog.get_logger()

# The only 4xx that are actually transient. Every other client error describes
# the request, so re-sending it unchanged can only fail the same way.
RETRYABLE_STATUS_CODES = frozenset({408, 429})

_CONTENT_POLICY_CODE = "content_policy_violation"
_CONTENT_POLICY_TEXT = "content safety"


def _status_code(exc: Exception):
    return getattr(exc, "status_code", None)


def is_retryable(exc: Exception) -> bool:
    """Tenacity predicate. Unrecognised failures stay retryable on purpose."""
    status = _status_code(exc)
    if not isinstance(status, int):
        return True
    if 400 <= status < 500:
        return status in RETRYABLE_STATUS_CODES
    return True


def is_content_policy_rejection(exc: Exception) -> bool:
    """True when Azure's content safety filter refused the request."""
    if _status_code(exc) != 400:
        return False
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") == _CONTENT_POLICY_CODE:
            return True
    # `code` has not always been populated; the message is the fallback signal.
    return _CONTENT_POLICY_TEXT in str(exc).lower()


def strip_image_blocks(blocks: list) -> list:
    """A new list with the image_url blocks removed, order preserved."""
    return [b for b in blocks
            if not (isinstance(b, dict) and b.get("type") == "image_url")]


def call_with_vision_fallback(call_fn, client, model, blocks):
    """Call `call_fn(client, model, blocks)`; drop images on a content-policy 400.

    Returns `(response, vision_dropped)`. Raises if the text-only attempt also
    fails, or if there were no images to drop in the first place.

    Deliberately outside the tenacity decorator on `call_fn`: this sends a
    DIFFERENT request, which is not what retry means.
    """
    try:
        return call_fn(client, model, blocks), False
    except Exception as exc:
        if not is_content_policy_rejection(exc):
            raise
        text_only = strip_image_blocks(blocks)
        dropped = len(blocks) - len(text_only)
        if dropped == 0:
            # Nothing to strip: the retry would be byte-identical.
            raise
        log.warning(
            "content_policy_vision_fallback",
            images_dropped=dropped,
            model=model,
            error=str(exc)[:200],
        )
        sentry_sdk.capture_message(
            f"Content filter rejected {dropped} image(s); retrying without vision",
            level="warning",
        )
        return call_fn(client, model, text_only), True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/core/test_llm_errors.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (167 + the new tests)

- [ ] **Step 6: Commit**

```bash
git add core/llm_errors.py tests/core/test_llm_errors.py
git commit -m "feat: classify LLM errors and add a content-policy vision fallback"
```

---

### Task 2: Stop retrying permanent errors at both call sites

**Files:**
- Modify: `core/pipeline.py` (the `@retry` decorator above `_call_analyze_llm`, around line 37)
- Modify: `core/processors/azure_processor.py` (the `@retry` decorator above `_call_openai`, line 14)
- Modify: `tests/core/test_llm_errors.py` (append the two decorator tests below)

**Interfaces:**
- Consumes: `core.llm_errors.is_retryable` (Task 1).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_llm_errors.py`:

```python
# --- the decorators actually use the predicate ----------------------------

class _RaisingClient:
    """Counts how many times the SDK entry point was reached."""

    def __init__(self, exc):
        self.calls = 0
        self._exc = exc
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls += 1
                raise outer._exc

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def test_analyze_call_does_not_retry_a_permanent_error():
    from core.pipeline import _call_analyze_llm

    client = _RaisingClient(FakeAPIError(400, body=_content_policy_body()))
    with pytest.raises(FakeAPIError):
        _call_analyze_llm(client, "gpt-5.4", [TEXT])
    assert client.calls == 1


def test_analyze_call_still_retries_a_server_error():
    from core.pipeline import _call_analyze_llm

    client = _RaisingClient(FakeAPIError(503))
    with pytest.raises(FakeAPIError):
        _call_analyze_llm(client, "gpt-5.4", [TEXT])
    assert client.calls == 3


def test_extraction_call_does_not_retry_a_permanent_error():
    from core.processors.azure_processor import _call_openai

    client = _RaisingClient(FakeAPIError(401))
    with pytest.raises(FakeAPIError):
        _call_openai(client, "gpt-5.4", [TEXT])
    assert client.calls == 1


def test_extraction_call_still_retries_a_server_error():
    from core.processors.azure_processor import _call_openai

    client = _RaisingClient(FakeAPIError(500))
    with pytest.raises(FakeAPIError):
        _call_openai(client, "gpt-5.4", [TEXT])
    assert client.calls == 3
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_llm_errors.py -q -k "permanent_error"`
Expected: FAIL — `assert 3 == 1`, because the current decorator retries everything.

The two `server_error` tests already pass; they are there to prove the change does not break the transient path.

- [ ] **Step 3: Change the analyze decorator**

In `core/pipeline.py`, change the import line:

```python
from tenacity import retry, stop_after_attempt, wait_exponential
```

to:

```python
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
```

Add next to the other `core` imports (only `is_retryable` for now — Task 3 adds the fallback import):

```python
from core.llm_errors import is_retryable
```

Then change the decorator on `_call_analyze_llm` from:

```python
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30),
       before_sleep=log_retry, reraise=True)
```

to:

```python
# A 4xx other than 408/429 describes the request, so re-sending it unchanged
# can only fail the same way. Retrying one cost ~5 minutes and three OCR
# passes in production on 2026-08-14.
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30),
       retry=retry_if_exception(is_retryable), before_sleep=log_retry, reraise=True)
```

- [ ] **Step 4: Change the extraction decorator**

In `core/processors/azure_processor.py`, change:

```python
from tenacity import retry, stop_after_attempt, wait_exponential
```

to:

```python
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from core.llm_errors import is_retryable
```

Then change the decorator on `_call_openai` from:

```python
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30),
       before_sleep=log_retry, reraise=True)
```

to:

```python
# See core/llm_errors.is_retryable — permanent client errors must not be retried.
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30),
       retry=retry_if_exception(is_retryable), before_sleep=log_retry, reraise=True)
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/pipeline.py core/processors/azure_processor.py tests/core/test_llm_errors.py
git commit -m "fix: stop retrying permanent client errors on both LLM call sites"
```

---

### Task 3: Route both call sites through the fallback

**Files:**
- Modify: `core/pipeline.py` (`analyze_document`, the `_call_analyze_llm(...)` call around line 208; add a class attribute on `Pipeline`)
- Modify: `core/processors/azure_processor.py` (`extract`, the `_call_openai(...)` call around line 109)
- Create: `tests/core/test_vision_fallback_pipeline.py`

**Interfaces:**
- Consumes: `call_with_vision_fallback(call_fn, client, model, blocks) -> (response, vision_dropped)` (Task 1).
- Produces:
  - `Pipeline.analyze_vision_dropped: bool` — class-level default `False`, set per instance in `analyze_document()`. Task 4 reads it.
  - `AzureInvoiceProcessor.extract()` sets `result["_vision_dropped"] = True` on the returned dict when its own call degraded. Task 4 pops that key.

**Critical — thread safety:** `extract_data_from_subdocuments` runs subdocuments in a `ThreadPoolExecutor` sharing **one** processor instance. The extraction signal therefore rides on the per-call result dict, never on `self`. Do not "simplify" it to an attribute.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_vision_fallback_pipeline.py`:

```python
"""The vision fallback, wired into the two call sites.

Both tests drive the real functions with a stubbed Azure client, so a refactor
that bypassed `call_with_vision_fallback` on either path would fail here.
"""
import pytest

from core.processors.azure_processor import AzureInvoiceProcessor


class FakeAPIError(Exception):
    def __init__(self, status_code, body=None):
        super().__init__(f"Error code: {status_code}")
        self.status_code = status_code
        self.body = body


CONTENT_POLICY_BODY = {"error": {
    "message": "Your input image may contain content that is not allowed by our content safety system.",
    "code": "content_policy_violation",
}}


class _RejectImagesClient:
    """Rejects any request carrying an image block; accepts text-only ones."""

    def __init__(self, payload='{"type": "invoice"}'):
        self.blocks_seen = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                blocks = kwargs["messages"][0]["content"]
                outer.blocks_seen.append(blocks)
                if any(b.get("type") == "image_url" for b in blocks):
                    raise FakeAPIError(400, body=CONTENT_POLICY_BODY)

                class _Msg:
                    content = payload

                class _Choice:
                    message = _Msg()

                class _Usage:
                    prompt_tokens = 10
                    completion_tokens = 5

                class _Resp:
                    choices = [_Choice()]
                    usage = _Usage()

                return _Resp()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _processor_with(client):
    proc = object.__new__(AzureInvoiceProcessor)
    proc.client = client
    proc.model = "gpt-5.4"
    proc.vision_model = "gpt-5.4"
    proc.name = "test"
    return proc


def test_extract_falls_back_to_text_and_marks_the_result(monkeypatch, tmp_path):
    img = tmp_path / "page.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    monkeypatch.setattr("core.processors.azure_processor.convert_file_to_images",
                        lambda p: [str(img)])

    client = _RejectImagesClient()
    result = _processor_with(client).extract(str(img), prompt="extract this")

    assert result["_vision_dropped"] is True
    assert len(client.blocks_seen) == 2
    assert any(b.get("type") == "image_url" for b in client.blocks_seen[0])
    assert not any(b.get("type") == "image_url" for b in client.blocks_seen[1])


def test_extract_leaves_a_clean_result_unmarked(monkeypatch, tmp_path):
    img = tmp_path / "page.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    monkeypatch.setattr("core.processors.azure_processor.convert_file_to_images",
                        lambda p: [str(img)])

    class _AcceptAll(_RejectImagesClient):
        def __init__(self):
            super().__init__()
            outer = self

            class _Completions:
                def create(self, **kwargs):
                    outer.blocks_seen.append(kwargs["messages"][0]["content"])

                    class _Msg:
                        content = '{"type": "invoice"}'

                    class _Choice:
                        message = _Msg()

                    class _Usage:
                        prompt_tokens = 10
                        completion_tokens = 5

                    class _Resp:
                        choices = [_Choice()]
                        usage = _Usage()

                    return _Resp()

            class _Chat:
                completions = _Completions()

            self.chat = _Chat()

    client = _AcceptAll()
    result = _processor_with(client).extract(str(img), prompt="extract this")

    assert "_vision_dropped" not in result
    assert len(client.blocks_seen) == 1


def test_pipeline_defaults_analyze_vision_dropped_to_false():
    # Several existing tests build Pipeline via object.__new__ and never call
    # analyze_document(); the attribute must still read False.
    from core.pipeline import Pipeline

    assert Pipeline.analyze_vision_dropped is False
    assert object.__new__(Pipeline).analyze_vision_dropped is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_vision_fallback_pipeline.py -q`
Expected: FAIL — the extract test raises `FakeAPIError` (no fallback yet), and the Pipeline attribute test raises `AttributeError`.

- [ ] **Step 3: Wire the analyze call site**

In `core/pipeline.py`, extend the Task 2 import to bring in the fallback:

```python
from core.llm_errors import call_with_vision_fallback, is_retryable
```

Do the same in `core/processors/azure_processor.py`.

Then add a class-level attribute at the top of the `Pipeline` class body, immediately after its docstring (before `__init__`):

```python
    # Set per instance by analyze_document(). Class-level default because
    # several tests build Pipeline via object.__new__ and never analyze.
    analyze_vision_dropped: bool = False
```

Then in `analyze_document`, change:

```python
        response = _call_analyze_llm(client, analyze_model, content_blocks)
```

to:

```python
        response, self.analyze_vision_dropped = call_with_vision_fallback(
            _call_analyze_llm, client, analyze_model, content_blocks,
        )
        if self.analyze_vision_dropped:
            _telemetry.warning(
                "analyze_vision_dropped",
                reason="content filter rejected the page images — splitting from OCR text only",
                pages=len(self.markdown_by_page),
            )
```

- [ ] **Step 4: Wire the extraction call site**

In `core/processors/azure_processor.py`, change:

```python
        model = self.vision_model if use_vision else self.model
        response = _call_openai(self.client, model, content_blocks)
```

to:

```python
        model = self.vision_model if use_vision else self.model
        # The model is deliberately NOT switched on fallback: dropping to
        # self.model would change extraction behaviour on top of the
        # degradation, and the vision model handles a text-only request fine.
        response, vision_dropped = call_with_vision_fallback(
            _call_openai, self.client, model, content_blocks,
        )
```

Then change the return at the end of `extract` from:

```python
        json_result = extract_json_from_response(response.choices[0].message.content)
        return json_result
```

to:

```python
        json_result = extract_json_from_response(response.choices[0].message.content)
        if vision_dropped and isinstance(json_result, dict):
            # Rides on the result, not on self: subdocuments are extracted in
            # parallel threads sharing one processor instance. The pipeline
            # pops this key, so it never reaches the consumer.
            json_result["_vision_dropped"] = True
        return json_result
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/core/test_vision_fallback_pipeline.py -q`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add core/pipeline.py core/processors/azure_processor.py tests/core/test_vision_fallback_pipeline.py
git commit -m "feat: fall back to text-only extraction when the content filter rejects images"
```

---

### Task 4: Report degradation as qualityFlags and warnings

**Files:**
- Modify: `core/ocr/ocr_dual.py` (`extract_text`, lines 24-57)
- Modify: `core/pipeline.py` (`_extract_single_subdocument`, the tail after `apply_returncode_floor`)
- Modify: `tests/core/test_vision_fallback_pipeline.py` (append)
- Modify: `tests/products/test_returncode_contract.py` (the first-two-keys assertion)

**Interfaces:**
- Consumes: `Pipeline.analyze_vision_dropped` and `result["_vision_dropped"]` (Task 3); `apply_returncode_floor` (already in `core/returncode.py`).
- Produces: `qualityFlags` on every subdocument — the consumer-visible contract.

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_vision_fallback_pipeline.py`:

```python
# --- qualityFlags and warnings -------------------------------------------

from core.pipeline import Pipeline, SubdocumentArtifact  # noqa: E402
from core.product import ProductConfig  # noqa: E402

ANALYZE_WARNING_FRAGMENT = "Aufteilung des Dokuments erfolgte ohne Bildanalyse"
EXTRACT_WARNING_FRAGMENT = "nur anhand des OCR-Textes"


class _FakeStorage:
    def materialize_to_local(self, key):
        return "/tmp/does-not-need-to-exist.png"


class _FixedProcessor:
    def __init__(self, result):
        self._result = result

    def extract(self, *args, **kwargs):
        return dict(self._result)


class _FakeOCR:
    def __init__(self, single_engine_fallback=False):
        self.single_engine_fallback = single_engine_fallback


_SUBDOC = SubdocumentArtifact(
    document_number=1, page_numbers=[1], markdown="text",
    md_key="k.md", pdf_key="k.pdf", image_key="k.png",
)


def _pipeline(*, analyze_dropped=False, ocr_degraded=False):
    pipe = object.__new__(Pipeline)
    pipe.storage = _FakeStorage()
    pipe.analysis_dict = {}
    pipe.product_config = ProductConfig(
        name="t", extract_prompt_builder=lambda **kw: "p", extract_output_schema={},
    )
    pipe.analyze_vision_dropped = analyze_dropped
    pipe.ocr_engine = _FakeOCR(ocr_degraded)
    return pipe


def test_clean_job_has_an_empty_quality_flags_list():
    pipe = _pipeline()
    result = pipe._extract_single_subdocument(_SUBDOC, _FixedProcessor({"number": "R-1"}))

    assert result["qualityFlags"] == []
    assert list(result)[:3] == ["returncode", "returncodeReasons", "qualityFlags"]


def test_analyze_degradation_flags_and_warns():
    pipe = _pipeline(analyze_dropped=True)
    result = pipe._extract_single_subdocument(_SUBDOC, _FixedProcessor({"number": "R-1"}))

    assert result["qualityFlags"] == ["VISION_DROPPED"]
    assert any(ANALYZE_WARNING_FRAGMENT in w for w in result["warnings"])


def test_extraction_degradation_flags_and_warns():
    pipe = _pipeline()
    processor = _FixedProcessor({"number": "R-1", "_vision_dropped": True})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert result["qualityFlags"] == ["VISION_DROPPED"]
    assert any(EXTRACT_WARNING_FRAGMENT in w for w in result["warnings"])


def test_the_internal_marker_never_reaches_the_consumer():
    pipe = _pipeline()
    processor = _FixedProcessor({"number": "R-1", "_vision_dropped": True})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert "_vision_dropped" not in result


def test_vision_dropped_is_not_duplicated_when_both_stages_degrade():
    pipe = _pipeline(analyze_dropped=True)
    processor = _FixedProcessor({"number": "R-1", "_vision_dropped": True})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert result["qualityFlags"] == ["VISION_DROPPED"]


def test_single_engine_ocr_is_flagged():
    pipe = _pipeline(ocr_degraded=True)
    result = pipe._extract_single_subdocument(_SUBDOC, _FixedProcessor({"number": "R-1"}))

    assert result["qualityFlags"] == ["SINGLE_ENGINE_OCR"]


def test_both_flags_can_appear_together():
    pipe = _pipeline(analyze_dropped=True, ocr_degraded=True)
    result = pipe._extract_single_subdocument(_SUBDOC, _FixedProcessor({"number": "R-1"}))

    assert result["qualityFlags"] == ["VISION_DROPPED", "SINGLE_ENGINE_OCR"]


def test_existing_warnings_are_preserved():
    pipe = _pipeline(analyze_dropped=True)
    processor = _FixedProcessor({"number": "R-1", "warnings": ["Steuersumme weicht ab"]})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert "Steuersumme weicht ab" in result["warnings"]
    assert len(result["warnings"]) == 2


def test_a_non_list_warnings_value_is_replaced():
    pipe = _pipeline(analyze_dropped=True)
    processor = _FixedProcessor({"number": "R-1", "warnings": "nope"})
    result = pipe._extract_single_subdocument(_SUBDOC, processor)

    assert isinstance(result["warnings"], list)
    assert any(ANALYZE_WARNING_FRAGMENT in w for w in result["warnings"])


def test_dual_ocr_sets_the_flag_when_one_engine_fails(monkeypatch):
    from core.ocr.ocr_dual import DualOCRProcessor

    dual = object.__new__(DualOCRProcessor)

    class _Good:
        def extract_text(self, invoice):
            return "md", {1: "page one"}

    class _Bad:
        def extract_text(self, invoice):
            raise RuntimeError("engine down")

    dual.mistral, dual.azure, dual.name = _Good(), _Bad(), "dual"
    monkeypatch.setattr("core.ocr.ocr_dual.sentry_sdk.capture_message", lambda *a, **k: None)

    dual.extract_text(invoice=None)

    assert dual.single_engine_fallback is True


def test_dual_ocr_leaves_the_flag_false_when_both_engines_work(monkeypatch):
    from core.ocr.ocr_dual import DualOCRProcessor

    dual = object.__new__(DualOCRProcessor)

    class _Good:
        def extract_text(self, invoice):
            return "md", {1: "page one"}

    dual.mistral, dual.azure, dual.name = _Good(), _Good(), "dual"

    dual.extract_text(invoice=None)

    assert dual.single_engine_fallback is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_vision_fallback_pipeline.py -q -k "quality or warn or dual_ocr or marker"`
Expected: FAIL — `KeyError: 'qualityFlags'` and `AttributeError: single_engine_fallback`.

- [ ] **Step 3: Add the DualOCR flag**

In `core/ocr/ocr_dual.py`, add a class-level default immediately after the class docstring line `class DualOCRProcessor:` and before `def __init__`:

```python
    # Set by extract_text(). Class-level default so a caller that inspects it
    # before OCR has run reads False rather than raising.
    single_engine_fallback: bool = False
```

Then inside `extract_text`, set it to `False` at the top of the method (immediately after the docstring):

```python
        self.single_engine_fallback = False
```

And set it to `True` in both `except` blocks, immediately after each `sentry_sdk.capture_message(...)` call:

```python
                self.single_engine_fallback = True
```

- [ ] **Step 4: Add the flags and warnings in the pipeline**

In `core/pipeline.py`, add these two module-level constants immediately after the `_telemetry = structlog.get_logger()` line:

```python
# Reported to the consumer verbatim; the wording is reviewed German.
_ANALYZE_VISION_WARNING = (
    "Die Aufteilung des Dokuments erfolgte ohne Bildanalyse, da der Inhaltsfilter des "
    "KI-Dienstes mindestens eine Seite abgelehnt hat. Die Zuordnung von Seiten zu Belegen "
    "kann ungenauer sein."
)
_EXTRACT_VISION_WARNING = (
    "Die Extraktion dieses Belegs erfolgte nur anhand des OCR-Textes, da der Inhaltsfilter "
    "des KI-Dienstes das Seitenbild abgelehnt hat. Einzelne Werte können ungenauer sein."
)
```

Then change the tail of `_extract_single_subdocument` from:

```python
        if self.product_config.postprocess_extraction is not None:
            result = self.product_config.postprocess_extraction(result)
        # Unconditional and last: all three products need the identical
        # guarantee, and VCC already occupies the postprocess hook. Running
        # after the hook means the floor judges the values the consumer will
        # actually receive.
        return apply_returncode_floor(result)
```

to:

```python
        if self.product_config.postprocess_extraction is not None:
            result = self.product_config.postprocess_extraction(result)
        # Unconditional and last: all three products need the identical
        # guarantee, and VCC already occupies the postprocess hook. Running
        # after the hook means the floor judges the values the consumer will
        # actually receive.
        result = apply_returncode_floor(result)
        return self._report_quality(result)

    def _report_quality(self, result: dict) -> dict:
        """Attach qualityFlags and the German warnings for any degradation.

        `qualityFlags` answers "how well did we read it?" and is deliberately
        separate from `returncode`, which answers "is this a Beleg?". Folding
        them together would let a badly-read invoice look like a non-invoice,
        which auto-cancels a legitimate claim.
        """
        # The processor marks its own per-subdocument degradation on the result
        # rather than on itself, because subdocuments are extracted in parallel
        # threads sharing one processor. Pop it: it is internal.
        extraction_dropped = bool(result.pop("_vision_dropped", False))
        analyze_dropped = bool(getattr(self, "analyze_vision_dropped", False))
        ocr_degraded = bool(getattr(getattr(self, "ocr_engine", None),
                                    "single_engine_fallback", False))

        flags = []
        if analyze_dropped or extraction_dropped:
            flags.append("VISION_DROPPED")
        if ocr_degraded:
            flags.append("SINGLE_ENGINE_OCR")

        warnings = result.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
        if analyze_dropped:
            warnings.append(_ANALYZE_VISION_WARNING)
        if extraction_dropped:
            warnings.append(_EXTRACT_VISION_WARNING)
        result["warnings"] = warnings

        # Rebuild so the three metadata fields lead, in the documented order.
        ordered = {
            "returncode": result.pop("returncode"),
            "returncodeReasons": result.pop("returncodeReasons"),
            "qualityFlags": flags,
        }
        ordered.update(result)
        return ordered
```

Do **not** touch `tests/products/test_returncode_contract.py` in this task. Its
first-two-keys assertion is about the *schema*, which Task 6 changes; updating it here
would leave the suite red between two tasks.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/pipeline.py core/ocr/ocr_dual.py tests/core/test_vision_fallback_pipeline.py
git commit -m "feat: report vision and OCR degradation via qualityFlags and warnings"
```

---

### Task 5: Stop RQ from re-running a doomed job

**Files:**
- Modify: `core/jobs/tasks.py` (the `except Exception:` block at lines 138-140)
- Create: `tests/core/test_job_retry_suppression.py`

**Interfaces:**
- Consumes: `core.llm_errors.is_retryable` (Task 1).
- Produces: nothing.

Without this, a permanent error still triggers `Retry(max=2)` from `core/api/routes/process.py:31`, re-running the whole pipeline — including both OCR engines, ~75 s — twice more before anyone sees it.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_job_retry_suppression.py`:

```python
"""A permanent failure must not be re-run by RQ.

Each RQ retry re-runs the entire pipeline including both OCR engines. For an
error that can never succeed — a rotated key, a rejected request — that is
minutes of compute and three OCR passes spent to reach the same conclusion.
"""
import types

import pytest

import core.jobs.tasks as tasks


class FakeAPIError(Exception):
    def __init__(self, status_code):
        super().__init__(f"Error code: {status_code}")
        self.status_code = status_code


class _FakeJob:
    def __init__(self, retries_left=2):
        self.retries_left = retries_left
        self.saved = False

    def save(self):
        self.saved = True


def _run_with(monkeypatch, exc, job):
    """Drive process_file to its failure path with everything else stubbed out.

    `load_product_config()` runs before `get_file_key` and raises RuntimeError
    when PRODUCT_NAME is unset, so it is stubbed too — otherwise the test would
    be asserting on the wrong exception entirely.
    """
    monkeypatch.setattr(tasks, "load_product_config",
                        lambda *a, **kw: types.SimpleNamespace(name="test"))
    monkeypatch.setattr(tasks, "get_file_key", lambda file_id: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(tasks, "get_current_job", lambda: job)
    with pytest.raises(type(exc)):
        tasks.process_file("some-file-id.pdf")


def test_permanent_error_suppresses_the_remaining_retries(monkeypatch):
    job = _FakeJob(retries_left=2)
    _run_with(monkeypatch, FakeAPIError(401), job)

    assert job.retries_left == 0
    assert job.saved is True


def test_transient_error_keeps_its_retries(monkeypatch):
    job = _FakeJob(retries_left=2)
    _run_with(monkeypatch, FakeAPIError(503), job)

    assert job.retries_left == 2
    assert job.saved is False


def test_no_rq_job_context_does_not_break_the_failure_path(monkeypatch):
    # process_file is also called directly by scripts/extract_local.py and the
    # sweep, where there is no RQ job at all.
    _run_with(monkeypatch, FakeAPIError(401), None)


def test_a_failure_while_suppressing_does_not_mask_the_original_error(monkeypatch):
    class _ExplodingJob(_FakeJob):
        def save(self):
            raise RuntimeError("redis is down")

    # The original FakeAPIError must still be what propagates.
    _run_with(monkeypatch, FakeAPIError(401), _ExplodingJob())
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/core/test_job_retry_suppression.py -q`
Expected: FAIL — `AttributeError: module 'core.jobs.tasks' has no attribute 'get_current_job'`.

- [ ] **Step 3: Implement the suppression**

In `core/jobs/tasks.py`, add to the imports at the top:

```python
from rq import get_current_job

from core.llm_errors import is_retryable
```

Then change the exception handler from:

```python
    except Exception:
        log.exception("job_failed", file_id=file_id, duration_s=round(time.monotonic() - job_start, 2))
        raise
```

to:

```python
    except Exception as exc:
        log.exception("job_failed", file_id=file_id, duration_s=round(time.monotonic() - job_start, 2))
        _suppress_retries_if_permanent(exc, file_id)
        raise


def _suppress_retries_if_permanent(exc: Exception, file_id: str) -> None:
    """Zero the RQ retry budget for an error that can never succeed.

    Each retry re-runs the whole pipeline, both OCR engines included. Never
    raises: the caller is about to re-raise the real failure, and losing that
    to a bookkeeping error would be far worse than a wasted retry.
    """
    if is_retryable(exc):
        return
    try:
        job = get_current_job()
        if job is None or not getattr(job, "retries_left", None):
            return
        job.retries_left = 0
        job.save()
        log.warning(
            "rq_retries_suppressed",
            file_id=file_id,
            error_type=type(exc).__name__,
            reason="permanent error — retrying would re-run OCR to reach the same failure",
        )
    except Exception:  # noqa: BLE001
        log.warning("rq_retry_suppression_failed", file_id=file_id)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/core/test_job_retry_suppression.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/jobs/tasks.py tests/core/test_job_retry_suppression.py
git commit -m "fix: do not let RQ re-run a job that failed permanently"
```

---

### Task 6: Publish qualityFlags in the schemas and the API doc

**Files:**
- Modify: `products/vetcostcheck/extract_schema.json`, `products/bps/extract_schema.json`, `products/sanierer/extract_schema.json`
- Modify: `tests/products/test_returncode_contract.py`
- Modify: `vetcostcheck_api_doc.md`

**Interfaces:**
- Consumes: the flag values from Task 4.
- Produces: nothing.

- [ ] **Step 1: Tighten the contract test first**

In `tests/products/test_returncode_contract.py`, change:

```python
def test_schema_publishes_both_fields_first(config):
    props = config.extract_output_schema["properties"]
    assert list(props)[:2] == ["returncode", "returncodeReasons"]
```

to:

```python
def test_schema_publishes_the_metadata_fields_first(config):
    props = config.extract_output_schema["properties"]
    assert list(props)[:3] == ["returncode", "returncodeReasons", "qualityFlags"]
    flags = props["qualityFlags"]
    assert flags["type"] == "array"
    assert flags["items"]["enum"] == ["VISION_DROPPED", "SINGLE_ENGINE_OCR"]
```

Run: `.venv/bin/python -m pytest tests/products/test_returncode_contract.py -q`
Expected: FAIL, 3 failures (one per product) — the schemas have no `qualityFlags` yet.

- [ ] **Step 2: Add the schema property**

In each of the three `extract_schema.json` files, insert this as the **third** entry in the top-level `"properties"` object — immediately after `"returncodeReasons"` and before `"type"`:

```json
    "qualityFlags": {"type": "array", "items": {"type": "string", "enum": ["VISION_DROPPED", "SINGLE_ENGINE_OCR"]}, "description": "Quality caveats for this subdocument. Always present, empty when nothing degraded. VISION_DROPPED = extracted from OCR text only because the content filter rejected the page images; SINGLE_ENGINE_OCR = one OCR engine failed. Separate from returncode by design."},
```

- [ ] **Step 3: Run the contract test**

Run: `.venv/bin/python -m pytest tests/products/test_returncode_contract.py -q`
Expected: PASS — including `test_schema_publishes_the_metadata_fields_first` from Task 4.

- [ ] **Step 4: Add the field to the API doc's example**

In `vetcostcheck_api_doc.md`, in the `### When finished:` block, change:

```json
        "returncode": 100,
        "returncodeReasons": [],
        "type": "invoice",
```

to:

```json
        "returncode": 100,
        "returncodeReasons": [],
        "qualityFlags": [],
        "type": "invoice",
```

- [ ] **Step 5: Document the field**

In `vetcostcheck_api_doc.md`, insert this section immediately after the `## Subdocument Return Codes` section and before `## Health Check`:

```markdown
---

## Quality Flags

Every subdocument carries `qualityFlags`, an array that is **always present** and
empty when extraction ran normally. It tells you how well the document could be
read — which is a separate question from whether it is an invoice, so it is kept
separate from `returncode`.

| Flag | Meaning |
|------|---------|
| `VISION_DROPPED` | The AI service's content filter rejected the page images, so this subdocument was read from OCR text alone. Individual values may be less accurate. |
| `SINGLE_ENGINE_OCR` | One of the two OCR engines was unavailable, so the text comes from one source instead of two. |

A flagged subdocument is still a normal result: `returncode` means exactly what
it always means, and the extracted fields are populated as usual. Treat the flags
as a signal that the result is worth a closer human look, not as an error.

`warnings` carries the same information as a German sentence for a human reader.

**Ignore flag values you do not recognise.** The list may grow, and new values
will always be additive.
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add products/*/extract_schema.json tests/products/test_returncode_contract.py vetcostcheck_api_doc.md
git commit -m "docs: publish qualityFlags in the product schemas and the API doc"
```

---

## Deferred to the maintainer (not tasks)

- **Regression sweep.** `scripts/returncode_sweep.py <dir> --expect 100` over the three corpora. The retry-policy change in Task 2 touches every LLM call in the pipeline, so this is not optional before release.
- **Acceptance run.** `~/Downloads/bps-content-policy-20260814-bf8d300a.pdf` (33 pages, page 20 rejected) through `PRODUCT_NAME=bps STORAGE_BACKEND=local scripts/extract_local.py`. It must now complete, and every subdocument must carry `VISION_DROPPED` plus the analyze warning. This is the document that motivated the work; it is customer data and is not committed.
- **Release.** Ships with the returncode feature once 3C approves. Do **not** deploy or promote from this plan.
- **Tell the PO** that the release adds `qualityFlags` alongside `returncode`, so 3C integrates both in one pass.
