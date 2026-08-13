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
