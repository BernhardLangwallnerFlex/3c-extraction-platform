"""Full-pipeline A/B: physical-size rendering ("off") vs. the per-page short-side cap.

Runs process_file end to end (OCR -> analyze -> split -> extract) with
RENDER_PAGE_SHORT_SIDE_PX=0 ("off") and set to each candidate cap, and compares the
extracted JSON field by field. LLM output is not deterministic, so each
condition runs AB_RUNS times; disagreement *within* a condition is the noise
floor that disagreement *between* conditions has to be judged against.

Usage:
    PRODUCT_NAME=bps STORAGE_BACKEND=local CLEANUP_ARTIFACTS=false \
        .venv/bin/python scripts/render_cap_pipeline_ab.py pdf [pdf ...]
Env:
    AB_CAPS="2500"    # comma-separated short-side caps; "off" is always included
    AB_RUNS=2
Artifacts land in temp/render_cap_ab/; JSON summary on stdout.
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

_EVENTS: list[dict] = []


def _capture(logger, method_name, event_dict):
    _EVENTS.append(dict(event_dict))
    return event_dict


structlog.configure(
    processors=[_capture, structlog.processors.add_log_level, structlog.processors.JSONRenderer()],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

OUT = REPO_ROOT / "temp" / "render_cap_ab"
IGNORE_KEYS = {"source", "snippet"}  # provenance text, varies with OCR wording


def flatten(obj, prefix="") -> dict[str, object]:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in IGNORE_KEYS:
                continue
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def run_once(pdf: Path, cap: str) -> dict:
    if cap == "off":
        os.environ["RENDER_PAGE_SHORT_SIDE_PX"] = "0"
    else:
        os.environ["RENDER_PAGE_SHORT_SIDE_PX"] = cap

    from core.jobs.tasks import process_file
    from core.storage.file_storage import save_upload

    _EVENTS.clear()
    t0 = time.monotonic()
    file_id = save_upload(pdf.read_bytes(), original_filename=pdf.name)
    error = None
    try:
        result = process_file(file_id)
    except Exception as e:  # noqa: BLE001
        result, error = {}, f"{type(e).__name__}: {e}"[:300]
    secs = time.monotonic() - t0

    pngs = sorted((REPO_ROOT / "temp").glob(f"{Path(file_id).stem}_subdocument_*.png"))
    tokens = [e for e in _EVENTS if "prompt_tokens" in e]
    return dict(
        file_id=file_id, seconds=round(secs, 1), error=error,
        result=result,
        png_mb=round(sum(p.stat().st_size for p in pngs) / 1e6, 2),
        png_px=[_px(p) for p in pngs],
        prompt_tokens=sum(e["prompt_tokens"] for e in tokens),
        mistral_failed=any("Mistral OCR failed" in str(e.get("event", "")) or e.get("event") == "ocr_engine_degraded"
                           for e in _EVENTS),
    )


def _px(path: Path) -> str:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        return f"{im.width}x{im.height}"


def summarise(run: dict) -> dict:
    subs = (run["result"] or {}).get("subdocuments") or []
    return dict(
        seconds=run["seconds"], error=run["error"], png_mb=run["png_mb"], png_px=run["png_px"],
        prompt_tokens=run["prompt_tokens"],
        n_subdocs=len(subs),
        flags=[s.get("qualityFlags") for s in subs],
        returncodes=[s.get("returncode") for s in subs],
        n_items=[len(s.get("items") or []) for s in subs],
    )


def diff(a: dict, b: dict) -> list[str]:
    fa, fb = flatten(a), flatten(b)
    keys = sorted(set(fa) | set(fb))
    return [f"{k}: {fa.get(k)!r} -> {fb.get(k)!r}" for k in keys if fa.get(k) != fb.get(k)]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    pdfs = [Path(a) for a in sys.argv[1:]]
    caps = ["off"] + [c.strip() for c in os.getenv("AB_CAPS", "2500").split(",") if c.strip()]
    runs = int(os.getenv("AB_RUNS", "2"))

    report = []
    for pdf in pdfs:
        by_cap: dict[str, list[dict]] = {}
        for cap in caps:
            for i in range(runs):
                r = run_once(pdf, cap)
                (OUT / f"{pdf.stem}__{cap}__run{i + 1}.json").write_text(
                    json.dumps(r["result"], indent=2, ensure_ascii=False))
                by_cap.setdefault(cap, []).append(r)
                s = summarise(r)
                print(f"[{pdf.stem[:10]}] cap={cap} run{i + 1}: {s}", file=sys.stderr)

        entry = dict(pdf=pdf.name, runs={c: [summarise(r) for r in rs] for c, rs in by_cap.items()}, diffs={})
        base = by_cap["off"][0]["result"]
        # noise floor: off run1 vs off run2
        for c, rs in by_cap.items():
            for i, r in enumerate(rs):
                if c == "off" and i == 0:
                    continue
                entry["diffs"][f"off#1 vs {c}#{i + 1}"] = diff(base, r["result"])
        report.append(entry)

    (OUT / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    json.dump(report, sys.stdout, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
