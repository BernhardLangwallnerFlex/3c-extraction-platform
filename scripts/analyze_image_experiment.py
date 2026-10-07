"""A/B harness for skipping low-text page images in analyze.

Spec: docs/superpowers/specs/2026-10-06-analyze-low-text-images-design.md

Modes (all local; STORAGE_BACKEND forced to local):
  calibrate  OCR every PDF (cached), print a chars-per-page histogram and list
             pages with 50–300 chars for visual review.
  ab         Analyze only, A = threshold None (today) vs B = --threshold,
             --runs each. Compares how TEXT pages are grouped (must match) and
             where LOW-TEXT pages land (reported), plus analyze tokens.
  large      B only, full pipeline (analyze, split, extract) — the >50-page
             documents A cannot process.

OCR runs once per PDF and is cached in temp/analyze_image_experiment/ocr/, so
A and B see identical input and OCR is paid once. Delete the cache to re-OCR.

Usage:
    .venv/bin/python scripts/analyze_image_experiment.py calibrate --product bps bps_sanierer_input/BPS_Input/*.pdf
    .venv/bin/python scripts/analyze_image_experiment.py ab --product bps --runs 3 bps_sanierer_input/BPS_Input/*.pdf
    .venv/bin/python scripts/analyze_image_experiment.py large --product bps test_uploads/BPS_Documents_Large/*.pdf
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "temp" / "analyze_image_experiment"
os.environ["STORAGE_BACKEND"] = "local"
os.environ.setdefault("CLEANUP_ARTIFACTS", "false")
sys.path.insert(0, str(REPO_ROOT))

import structlog  # noqa: E402

_EVENTS: list[dict] = []


def _capture(logger, method_name, event_dict):
    if event_dict.get("event") in ("analyze_llm_call", "llm_call", "analyze_images_capped"):
        _EVENTS.append(dict(event_dict))
    return event_dict


structlog.configure(
    processors=[_capture, structlog.processors.add_log_level, structlog.processors.JSONRenderer()],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

from core.analyze_content import build_pages_markdown, count_ocr_chars  # noqa: E402
from core.ocr.ocr_dual import DualOCRProcessor  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.processors.azure_processor import AzureInvoiceProcessor  # noqa: E402
from core.product import load_product_config  # noqa: E402
from core.storage.storage import LocalStorage  # noqa: E402


def _pipeline(pdf: Path, config, tag: str) -> Pipeline:
    """A Pipeline on `pdf` with OCR loaded from cache (or run once and cached)."""
    out_prefix = OUT / "runs" / f"{pdf.stem}__{tag}"
    out_prefix.mkdir(parents=True, exist_ok=True)
    pipe = Pipeline(
        file_key=str(pdf.resolve()),
        ocr_engine=DualOCRProcessor(name="dual_ocr"),
        product_config=config,
        storage=LocalStorage(),
        output_prefix=str(out_prefix),
    )
    cache = OUT / "ocr" / f"{pdf.stem}.json"
    if cache.exists():
        by_page = {int(k): v for k, v in json.loads(cache.read_text()).items()}
        pipe.markdown_by_page = by_page
        pipe.markdown = "\n\n".join(by_page.values())
        pipe.markdown_with_pages_numbers = build_pages_markdown(by_page, {})
    else:
        pipe.extract_markdown()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(pipe.markdown_by_page, ensure_ascii=False))
    return pipe


def _grouping(invoice_pages: dict, low: set[int]) -> tuple[frozenset, dict[int, frozenset]]:
    """Split an analyze result into (partition of text pages, low-text page -> its Beleg).

    Belege are identified by their text pages, not by the LLM's keys, which
    vary between runs. A low-text page maps to the text pages of its Beleg
    (empty frozenset = a Beleg of only low-text pages); unassigned pages are absent.
    """
    groups = [frozenset(int(p) for p in pages) for pages in (invoice_pages or {}).values()]
    partition = frozenset(g - low for g in groups if g - low)
    placement = {p: g - low for g in groups for p in g if p in low}
    return partition, placement


def cmd_calibrate(args, config):
    hist = Counter()
    review = []
    for pdf in args.pdfs:
        pipe = _pipeline(pdf, config, "calibrate")
        for page, md in pipe.markdown_by_page.items():
            n = count_ocr_chars(md)
            hist[min(n // 50 * 50, 1000)] += 1
            if 50 <= n <= 300:
                review.append({"pdf": pdf.name, "page": page, "chars": n, "text": md[:300]})
    print("chars/page (bucket start: pages)")
    for bucket in sorted(hist):
        label = f"{bucket}+" if bucket == 1000 else f"{bucket}-{bucket + 49}"
        print(f"  {label:>9}: {hist[bucket]}")
    (OUT / "calibration_review.json").write_text(json.dumps(review, indent=2, ensure_ascii=False))
    print(f"{len(review)} pages with 50–300 chars -> {OUT / 'calibration_review.json'}")


def cmd_ab(args, config):
    variants = {"A": dataclasses.replace(config, analyze_low_text_threshold=None),
                "B": dataclasses.replace(config, analyze_low_text_threshold=args.threshold)}
    report = []
    for pdf in args.pdfs:
        ocr = _pipeline(pdf, config, "ocr").markdown_by_page
        low = {p for p, md in ocr.items() if count_ocr_chars(md) < args.threshold}
        runs: dict[str, list] = {"A": [], "B": []}
        tokens: dict[str, list] = {"A": [], "B": []}
        for name, cfg in variants.items():
            for i in range(args.runs):
                _EVENTS.clear()
                pipe = _pipeline(pdf, cfg, f"{name}{i}")
                pipe.analyze_document()
                runs[name].append(_grouping(pipe.analysis_dict.get("invoice_pages"), low))
                tokens[name].append(sum(e.get("prompt_tokens", 0) for e in _EVENTS))
        a_parts = {part for part, _ in runs["A"]}
        b_parts = {part for part, _ in runs["B"]}
        text_ok = b_parts <= a_parts
        a_place = Counter(json.dumps({p: sorted(g) for p, g in sorted(pl.items())}) for _, pl in runs["A"])
        b_place = Counter(json.dumps({p: sorted(g) for p, g in sorted(pl.items())}) for _, pl in runs["B"])
        rec = {
            "pdf": pdf.name, "pages": len(ocr), "low_text_pages": sorted(low),
            "text_grouping_ok": text_ok,
            "a_distinct_text_groupings": len(a_parts), "b_distinct_text_groupings": len(b_parts),
            "a_low_text_placements": a_place, "b_low_text_placements": b_place,
            "a_prompt_tokens": tokens["A"], "b_prompt_tokens": tokens["B"],
            "a_groupings": [sorted(sorted(g) for g in part) for part in a_parts],
            "b_groupings": [sorted(sorted(g) for g in part) for part in b_parts],
        }
        report.append(rec)
        flag = "OK " if text_ok else "DIFF"
        print(f"[{flag}] {pdf.name}: pages={len(ocr)} low={len(low)} "
              f"A-groupings={len(a_parts)} B-groupings={len(b_parts)} "
              f"tokens A={tokens['A']} B={tokens['B']} "
              f"photo-placement same={a_place == b_place}", file=sys.stderr)
    (OUT / f"ab_{args.product}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=list))
    ok = sum(r["text_grouping_ok"] for r in report)
    print(f"text grouping matches A: {ok}/{len(report)} -> {OUT / f'ab_{args.product}.json'}")


def cmd_large(args, config):
    cfg = dataclasses.replace(config, analyze_low_text_threshold=args.threshold)
    processor = AzureInvoiceProcessor(
        name="azure_processor",
        api_key=os.getenv("AZURE_OPENAI_KEY"),
        model=os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4"),
        vision_model=os.getenv("OPENAI_VISION_MODEL", "gpt-5.4"),
        azure_endpoint=os.getenv("AZURE_ENDPOINT"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
    )
    report = []
    for pdf in args.pdfs:
        _EVENTS.clear()
        t0 = time.monotonic()
        rec = {"pdf": pdf.name}
        try:
            pipe = _pipeline(pdf, cfg, "large")
            pipe.analyze_document()
            pipe.split_document_into_invoices()
            pipe.extract_data_from_subdocuments(processor)
            result = pipe.extraction_result_json
            rec.update(
                completed=True, pages=len(pipe.markdown_by_page),
                invoice_pages=pipe.analysis_dict.get("invoice_pages"),
                subdocuments=result["number_of_subdocuments"],
                items=[len((s or {}).get("items") or []) for s in result["subdocuments"]],
                returncodes=[(s or {}).get("returncode") for s in result["subdocuments"]],
            )
        except Exception as exc:  # report and continue with the next document
            rec.update(completed=False, error=f"{type(exc).__name__}: {str(exc)[:300]}")
        analyze = [e for e in _EVENTS if e["event"] == "analyze_llm_call"]
        rec.update(
            seconds=round(time.monotonic() - t0, 1),
            capped=any(e["event"] == "analyze_images_capped" for e in _EVENTS),
            images_sent=analyze[0].get("images_sent") if analyze else None,
            pages_low_text=analyze[0].get("pages_low_text") if analyze else None,
            prompt_tokens=sum(e.get("prompt_tokens", 0) for e in _EVENTS),
            completion_tokens=sum(e.get("completion_tokens", 0) for e in _EVENTS),
        )
        report.append(rec)
        print(f"{pdf.name}: completed={rec['completed']} subdocs={rec.get('subdocuments')} "
              f"images={rec['images_sent']} low={rec['pages_low_text']} capped={rec['capped']} "
              f"{rec['seconds']}s", file=sys.stderr)
    (OUT / f"large_{args.product}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"completed {sum(r['completed'] for r in report)}/{len(report)} -> {OUT / f'large_{args.product}.json'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["calibrate", "ab", "large"])
    ap.add_argument("--product", required=True, choices=["bps", "sanierer"])
    ap.add_argument("--threshold", type=int, default=150)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("pdfs", nargs="+", type=Path)
    args = ap.parse_args()
    missing = [str(p) for p in args.pdfs if not p.exists()]
    if missing:
        print(f"Missing files: {missing}", file=sys.stderr)
        return 2
    os.environ["PRODUCT_NAME"] = args.product
    OUT.mkdir(parents=True, exist_ok=True)
    config = load_product_config(args.product)
    {"calibrate": cmd_calibrate, "ab": cmd_ab, "large": cmd_large}[args.mode](args, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
