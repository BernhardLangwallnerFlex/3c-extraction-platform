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
