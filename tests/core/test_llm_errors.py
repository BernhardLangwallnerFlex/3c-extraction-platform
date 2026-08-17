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


@pytest.fixture
def no_backoff(monkeypatch):
    # The retry tests assert attempt COUNTS, not timing. Without this they
    # pay the real wait_exponential backoff (~9s) on every suite run.
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _seconds: None)


def test_analyze_call_still_retries_a_server_error(no_backoff):
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


def test_extraction_call_still_retries_a_server_error(no_backoff):
    from core.processors.azure_processor import _call_openai

    client = _RaisingClient(FakeAPIError(500))
    with pytest.raises(FakeAPIError):
        _call_openai(client, "gpt-5.4", [TEXT])
    assert client.calls == 3
