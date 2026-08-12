"""Repeatability + cost harness for the Sanierer layout-reasoning experiment.

Runs the full extraction pipeline on one PDF N times and reports, per run:
  - number_of_subdocuments
  - items extracted per subdocument  (the determinism metric)
  - total prompt/completion tokens + USD cost across all LLM calls in that run
    (the output-token cost metric the article's preamble would move)

Usage:
    PRODUCT_NAME=sanierer STORAGE_BACKEND=local \
        .venv/bin/python scripts/layout_experiment.py <pdf> [runs]

Everything is logged to stderr; the final summary table is printed to stdout.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import structlog  # noqa: E402

# Capture every LLM-call telemetry event (analyze + per-subdoc extraction) into
# this list. _capturing_processor appends, then lets the event flow on to stderr.
_LLM_EVENTS: list[dict] = []


def _capturing_processor(logger, method_name, event_dict):
    if "completion_tokens" in event_dict:
        _LLM_EVENTS.append(dict(event_dict))
    return event_dict


# Configure BEFORE process_file runs so this chain wins. Logs go to stderr,
# keeping stdout clean for the JSON summary.
structlog.configure(
    processors=[
        _capturing_processor,
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)


def _items_per_subdoc(result: dict) -> list[int]:
    subs = result.get("subdocuments", []) or []
    return [len((s or {}).get("items", []) or []) for s in subs]


def _norm(v) -> str:
    """Normalize a position/lvPosition for comparison (strip, drop trailing dot)."""
    return str(v or "").strip().rstrip(".")


def _field_stats(result: dict) -> dict:
    """Aggregate position/lvPosition fill quality across all items in the result.

    The position/lvPosition bug manifests as: a number missing when one is
    visible, or the two columns collapsed to the same value. Both are tracked.
    """
    items = []
    for s in result.get("subdocuments", []) or []:
        items.extend((s or {}).get("items", []) or [])
    n = len(items)
    with_pos = sum(1 for it in items if _norm(it.get("position")))
    with_lv = sum(1 for it in items if _norm(it.get("lvPosition")))
    with_both = sum(1 for it in items if _norm(it.get("position")) and _norm(it.get("lvPosition")))
    pos_eq_lv = sum(1 for it in items
                    if _norm(it.get("position")) and _norm(it.get("position")) == _norm(it.get("lvPosition")))
    return {
        "n_items": n,
        "with_position": with_pos,
        "with_lvPosition": with_lv,
        "with_both": with_both,
        "pos_eq_lv": pos_eq_lv,  # both present but identical → likely confusion
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/layout_experiment.py <pdf> [runs]", file=sys.stderr)
        return 2
    pdf = Path(sys.argv[1])
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if not pdf.exists():
        print(f"No such file: {pdf}", file=sys.stderr)
        return 2

    from core.storage.file_storage import save_upload
    from core.jobs.tasks import process_file

    summary = []
    for i in range(1, runs + 1):
        _LLM_EVENTS.clear()
        file_id = save_upload(pdf.read_bytes(), original_filename=pdf.name)
        result = process_file(file_id)

        prompt_tok = sum(e.get("prompt_tokens", 0) for e in _LLM_EVENTS)
        completion_tok = sum(e.get("completion_tokens", 0) for e in _LLM_EVENTS)
        cost = sum(e.get("cost_usd", 0.0) for e in _LLM_EVENTS)
        per_sub = _items_per_subdoc(result)
        fstats = _field_stats(result)
        # Dump full result for manual spot-checking of field assignment.
        out_dir = REPO_ROOT / "temp" / "layout_experiment"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{pdf.stem}_run{i}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False))
        summary.append({
            "run": i,
            "n_subdocuments": result.get("number_of_subdocuments"),
            "items_per_subdoc": per_sub,
            "total_items": sum(per_sub),
            "field_stats": fstats,
            "llm_calls": len(_LLM_EVENTS),
            "prompt_tokens": prompt_tok,
            "completion_tokens": completion_tok,
            "cost_usd": round(cost, 5),
        })
        print(f"[run {i}/{runs}] subs={result.get('number_of_subdocuments')} "
              f"items={per_sub} fields={fstats} compl_tok={completion_tok} cost=${cost:.4f}",
              file=sys.stderr)

    print(json.dumps({"pdf": pdf.name, "runs": summary}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
