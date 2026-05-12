"""Run extraction on fixed PDFs and diff JSON against pinned references.

Workflow for the multi-product refactor checkpoint:
    1. Copy your 3 chosen test PDFs into tests/regression/inputs/
       (suggested: VCC_Viele_Dokumente.pdf, testrechnung_03_katze.pdf,
       230074893V_Splitt.pdf — all live in 3C_testdaten_pdf/)
    2. python scripts/regression_check.py --capture   # against pre-refactor code
    3. Do the refactor step.
    4. python scripts/regression_check.py             # confirm PASS

Inputs and captured references are intentionally NOT committed (they contain
customer-shaped data). Each checkpoint re-captures locally.

The diff is intentionally permissive: it compares JSON *structure* (keys, list
lengths) and *numeric* leaf values (counts, prices, totals). String content is
ignored because LLM outputs vary in wording even with seed/temperature pinned.

LLM-noise smoothers applied so the diff stays stable on re-runs:
  1. The entire `.warnings` subtree is skipped (LLM-generated quality notes
     vary wildly in count and wording per run).
  2. None is treated as type-compatible with str (the LLM randomly emits null
     vs. an empty-ish string for the same "missing" field).
  3. Length differences on known-noisy structured arrays are ignored
     (see NOISY_LEN_PATHS). Their *content* is still compared pairwise where
     overlap exists, so a renamed/dropped field still trips the diff.

Per-PDF strictness (see PDF_MODE):
  - Default ("strict"): full structural + numeric diff with the smoothers above.
  - "shape": only verifies number_of_subdocuments and per-subdoc len(items).
    Used for PDFs where extraction is too LLM-noisy for value-level comparison
    (currently: VCC_Viele_Dokumente, 4 subdocuments, persistent intermittent
    nulls and array-length jitter on every run).

Any other structural or numeric drift fails the run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Force local storage BEFORE any imports that load .env (jobs/tasks.py calls
# load_dotenv at import time). load_dotenv does not override pre-set env vars.
os.environ["STORAGE_BACKEND"] = "local"
os.environ["LOCAL_STORAGE_BASE_DIR"] = str(REPO_ROOT / "temp")

sys.path.insert(0, str(REPO_ROOT))

INPUTS = REPO_ROOT / "tests" / "regression" / "inputs"
REFS = REPO_ROOT / "tests" / "regression" / "references"

# LLM-noisy array fields whose length varies between runs. Content is still
# compared pairwise (via zip), so renamed/missing inner keys still surface.
NOISY_LEN_PATHS = (".clinicians", ".diagnoses")

# Per-PDF comparison mode keyed by file stem. Anything not listed is "strict".
PDF_MODE = {
    "VCC_Viele_Dokumente": "shape",
}


def run_extraction(pdf_path: Path) -> dict:
    """Save the PDF via the same code path the API uses, then run process_file."""
    from core.storage.file_storage import save_upload
    from core.jobs.tasks import process_file

    file_bytes = pdf_path.read_bytes()
    file_id = save_upload(file_bytes, original_filename=pdf_path.name)
    return process_file(file_id)


def _diff_walk(actual, expected, path: str, diffs: list) -> None:
    """Walk both trees in parallel. Record structural and numeric mismatches only.

    Smoothers (see module docstring):
      - paths ending in `.warnings` are skipped wholesale
      - None matches str (and vice versa) at any leaf
    """
    if path.endswith(".warnings"):
        return

    if type(actual) is not type(expected):
        # int vs float: compare numerically
        if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
            if actual != expected:
                diffs.append((path, "NUM", f"{actual!r} != {expected!r}"))
            return
        # None ↔ str: treat as same shape (LLM null/empty drift)
        if (actual is None and isinstance(expected, str)) or (expected is None and isinstance(actual, str)):
            return
        diffs.append((path, "TYPE", f"{type(actual).__name__} != {type(expected).__name__}"))
        return

    if isinstance(actual, dict):
        a_keys, e_keys = set(actual), set(expected)
        if a_keys != e_keys:
            missing = e_keys - a_keys
            extra = a_keys - e_keys
            diffs.append((path, "KEYS", f"missing={sorted(missing)} extra={sorted(extra)}"))
        for k in sorted(a_keys & e_keys):
            _diff_walk(actual[k], expected[k], f"{path}.{k}", diffs)
    elif isinstance(actual, list):
        if len(actual) != len(expected) and not path.endswith(NOISY_LEN_PATHS):
            diffs.append((path, "LEN", f"{len(actual)} != {len(expected)}"))
        for i, (a, e) in enumerate(zip(actual, expected)):
            _diff_walk(a, e, f"{path}[{i}]", diffs)
    elif isinstance(actual, bool):
        if actual != expected:
            diffs.append((path, "BOOL", f"{actual} != {expected}"))
    elif isinstance(actual, (int, float)):
        if actual != expected:
            diffs.append((path, "NUM", f"{actual!r} != {expected!r}"))
    # strings, None: structure-only — ignore content


def _shape_signature(payload: dict) -> dict:
    """Reduce a full extraction result to its coarse structural shape.

    Captures only the parts a refactor would realistically perturb:
      - top-level keys present
      - number_of_subdocuments
      - per-subdocument len(items) (the actual extraction target)
    """
    subs = payload.get("subdocuments", []) or []
    return {
        "top_keys": sorted(payload.keys()),
        "number_of_subdocuments": payload.get("number_of_subdocuments"),
        "items_per_subdocument": [len((s or {}).get("items", []) or []) for s in subs],
    }


def diff(pdf_path: Path, capture: bool) -> bool:
    ref_path = REFS / f"{pdf_path.stem}.json"
    actual = run_extraction(pdf_path)

    if capture:
        ref_path.write_text(json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True))
        print(f"[CAPTURED] {pdf_path.name} -> {ref_path.relative_to(REPO_ROOT)}")
        return True

    if not ref_path.exists():
        print(f"[MISSING REF] {pdf_path.name} — run with --capture first", file=sys.stderr)
        return False

    expected = json.loads(ref_path.read_text())
    mode = PDF_MODE.get(pdf_path.stem, "strict")

    if mode == "shape":
        actual_sig = _shape_signature(actual)
        expected_sig = _shape_signature(expected)
        if actual_sig == expected_sig:
            print(f"[PASS] {pdf_path.name} (shape-only)")
            return True
        print(f"[FAIL] {pdf_path.name} (shape-only):", file=sys.stderr)
        print(f"  expected: {expected_sig}", file=sys.stderr)
        print(f"  actual:   {actual_sig}", file=sys.stderr)
        return False

    diffs: list = []
    _diff_walk(actual, expected, "", diffs)
    if not diffs:
        print(f"[PASS] {pdf_path.name}")
        return True

    print(f"[FAIL] {pdf_path.name} — {len(diffs)} structural/numeric diff(s):", file=sys.stderr)
    for p, kind, msg in diffs[:25]:
        print(f"  {p or '<root>'}  [{kind}]  {msg}", file=sys.stderr)
    if len(diffs) > 25:
        print(f"  ... and {len(diffs) - 25} more", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", action="store_true", help="Overwrite references with current output")
    args = parser.parse_args()

    pdfs = sorted(INPUTS.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs in {INPUTS}", file=sys.stderr)
        return 2

    all_ok = True
    for pdf in pdfs:
        ok = diff(pdf, capture=args.capture)
        all_ok = all_ok and ok

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
