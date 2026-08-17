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
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            if error.get("code") == _CONTENT_POLICY_CODE:
                return True
            message = error.get("message") or ""
    # `code` has not always been populated; the message is the fallback signal.
    return _CONTENT_POLICY_TEXT in f"{message} {exc}".lower()


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
