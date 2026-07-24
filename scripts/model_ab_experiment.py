"""A/B model comparison harness for the VetCostCheck extraction pipeline.

Runs the full pipeline (OCR is model-independent; analyze + extract both use the
configured OpenAI model) over a set of PDFs under several candidate models and
compares, per model:
  - extraction quality: subdocument count, total items, and GOT-in-name
    preservation (the behavior shipped 2026-07-24 — GOT refs must stay in `name`)
  - cost: prompt/completion tokens across ALL LLM calls (analyze + every
    subdoc extraction), priced per-model from the GlobalStandard short-context
    list rates below
  - speed: wall-clock seconds per document

Both analyze_document() and extract read OPENAI_VISION_MODEL / OPENAI_TEXT_MODEL,
so setting them per candidate swaps the whole pipeline to that model.

Usage:
    PRODUCT_NAME=vetcostcheck STORAGE_BACKEND=local \
        .venv/bin/python scripts/model_ab_experiment.py [pdf ...]

With no pdf args, uses the default 3C_testdaten_pdf set below.
Env overrides:
    AB_MODELS="gpt-5.4,gpt-5.6-terra,gpt-5.6-sol"   # comma-separated
    AB_RUNS=1                                        # runs per (model, pdf)

All artifacts (full JSON per model/pdf/run) land in temp/model_ab/.
Logs go to stderr; the final JSON summary is printed to stdout.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import structlog  # noqa: E402

# GlobalStandard list price (USD per 1M tokens), short-context standard tier,
# from the Azure Retail Prices API (2026-07-24). (input, output).
RATES = {
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.6-terra": (2.5, 15.0),
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.6-luna": (1.0, 6.0),
}

DEFAULT_FILES = [
    "3C_testdaten_pdf/230075012T2_Splitt.pdf",  # original task doc (inline GOT)
    "3C_testdaten_pdf/230041495V_Splitt.pdf",   # 18 inline-GOT items
    "3C_testdaten_pdf/230068612T_Splitt.pdf",   # 1 inline GOT
    "3C_testdaten_pdf/230074801Z_Splitt.pdf",   # GOT in separate column
    "3C_testdaten_pdf/230074893V_Splitt.pdf",   # "- 16" style GOT
]

# Capture every LLM-call telemetry event (analyze + per-subdoc extraction).
_LLM_EVENTS: list[dict] = []


def _capturing_processor(logger, method_name, event_dict):
    if "completion_tokens" in event_dict:
        _LLM_EVENTS.append(dict(event_dict))
    return event_dict


structlog.configure(
    processors=[
        _capturing_processor,
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)


def _cost(model: str, prompt_tok: int, completion_tok: int) -> float:
    in_rate, out_rate = RATES.get(model, (0.0, 0.0))
    return prompt_tok / 1e6 * in_rate + completion_tok / 1e6 * out_rate


def _items(result: dict) -> list[dict]:
    items: list[dict] = []
    for s in result.get("subdocuments", []) or []:
        items.extend((s or {}).get("items", []) or [])
    return items


def _got_in_name(name: str | None, code) -> bool:
    """Heuristic: does the line-item name retain a GOT reference?

    Matches the observed inline forms: "(GOT 643)", a bare "(627)", and "- 16".
    """
    n = name or ""
    if "got" in n.lower():
        return True
    if code:
        c = str(code)
        if f"({c})" in n or f"- {c}" in n or f"-{c}" in n:
            return True
    return False


def _got_stats(result: dict) -> dict:
    items = _items(result)
    detected = 0  # items where a GOT code was extracted
    preserved = 0  # ...of those, name still carries a GOT reference
    for it in items:
        got = (it or {}).get("got") or {}
        if got.get("code"):
            detected += 1
            if _got_in_name(it.get("name"), got.get("code")):
                preserved += 1
    return {"got_detected": detected, "got_in_name": preserved}


def run_model(model: str, files: list[Path], runs: int) -> dict:
    os.environ["OPENAI_TEXT_MODEL"] = model
    os.environ["OPENAI_VISION_MODEL"] = model

    from core.storage.file_storage import save_upload
    from core.jobs.tasks import process_file

    out_dir = REPO_ROOT / "temp" / "model_ab"
    out_dir.mkdir(parents=True, exist_ok=True)

    per_doc = []
    for pdf in files:
        for i in range(1, runs + 1):
            _LLM_EVENTS.clear()
            t0 = time.monotonic()
            file_id = save_upload(pdf.read_bytes(), original_filename=pdf.name)
            result = process_file(file_id)
            elapsed = time.monotonic() - t0

            prompt_tok = sum(e.get("prompt_tokens", 0) for e in _LLM_EVENTS)
            completion_tok = sum(e.get("completion_tokens", 0) for e in _LLM_EVENTS)
            models_used = sorted({e.get("model") for e in _LLM_EVENTS if e.get("model")})
            items = _items(result)
            gstats = _got_stats(result)
            cost = _cost(model, prompt_tok, completion_tok)

            (out_dir / f"{model}__{pdf.stem}__run{i}.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False))

            rec = {
                "pdf": pdf.name,
                "run": i,
                "n_subdocuments": result.get("number_of_subdocuments"),
                "total_items": len(items),
                **gstats,
                "llm_calls": len(_LLM_EVENTS),
                "prompt_tokens": prompt_tok,
                "completion_tokens": completion_tok,
                "cost_usd": round(cost, 5),
                "seconds": round(elapsed, 2),
                "models_used": models_used,
            }
            per_doc.append(rec)
            warn = "" if models_used == [model] else f"  !! models_used={models_used}"
            print(f"[{model}] {pdf.name} run{i}: subs={rec['n_subdocuments']} "
                  f"items={rec['total_items']} got={gstats['got_in_name']}/{gstats['got_detected']} "
                  f"compl_tok={completion_tok} cost=${cost:.4f} {elapsed:.1f}s{warn}",
                  file=sys.stderr)

    n = len(per_doc)
    agg = {
        "model": model,
        "docs": n,
        "total_items": sum(r["total_items"] for r in per_doc),
        "got_detected": sum(r["got_detected"] for r in per_doc),
        "got_in_name": sum(r["got_in_name"] for r in per_doc),
        "total_cost_usd": round(sum(r["cost_usd"] for r in per_doc), 5),
        "avg_cost_per_doc": round(sum(r["cost_usd"] for r in per_doc) / n, 5) if n else None,
        "avg_seconds_per_doc": round(sum(r["seconds"] for r in per_doc) / n, 2) if n else None,
        "completion_tokens": sum(r["completion_tokens"] for r in per_doc),
    }
    return {"aggregate": agg, "per_doc": per_doc}


def main() -> int:
    args = [a for a in sys.argv[1:]]
    files = [Path(a) for a in args] if args else [REPO_ROOT / f for f in DEFAULT_FILES]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        print(f"Missing files: {missing}", file=sys.stderr)
        return 2

    models = os.getenv("AB_MODELS", "gpt-5.4,gpt-5.6-terra,gpt-5.6-sol").split(",")
    models = [m.strip() for m in models if m.strip()]
    runs = int(os.getenv("AB_RUNS", "1"))

    print(f"Models: {models}  |  files: {[f.name for f in files]}  |  runs/pair: {runs}",
          file=sys.stderr)

    results = [run_model(m, files, runs) for m in models]

    # Comparison table -> stderr for quick reading
    print("\n=== A/B SUMMARY (GlobalStandard, short-context list pricing) ===", file=sys.stderr)
    hdr = f"{'model':16s} | {'docs':>4s} | {'items':>5s} | {'GOT keep':>10s} | {'tot $':>8s} | {'$/doc':>7s} | {'s/doc':>6s}"
    print(hdr, file=sys.stderr)
    print("-" * len(hdr), file=sys.stderr)
    base_cost = None
    for r in results:
        a = r["aggregate"]
        keep = f"{a['got_in_name']}/{a['got_detected']}"
        if base_cost is None:
            base_cost = a["total_cost_usd"]
        delta = "" if not base_cost else f" ({(a['total_cost_usd']/base_cost-1)*100:+.0f}%)"
        print(f"{a['model']:16s} | {a['docs']:>4d} | {a['total_items']:>5d} | {keep:>10s} | "
              f"${a['total_cost_usd']:>6.3f} | ${a['avg_cost_per_doc']:>5.4f} | {a['avg_seconds_per_doc']:>5.1f}{delta}",
              file=sys.stderr)

    print(json.dumps({"models": models, "runs_per_pair": runs, "results": results},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
