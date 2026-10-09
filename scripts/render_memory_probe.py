"""Peak memory of one job's rendering, without any API call.

Answers "how much memory does a worker need?" for worker sizing. Each PDF runs
in its own process — RQ forks one work-horse per job, so the process peak is
the job peak — through the real code paths that allocate images:

  init      Pipeline(): orientation detection (tesseract on rendered pages)
  ocr       MistralOCRProcessor._process_pdf render loop (the API call stubbed)
  analyze   Pipeline.analyze_document page images (the API call stubbed)
  split     Pipeline.split_document_into_invoices: sub-PDF + stacked canvas

Worst case on purpose: analyze is stubbed to put EVERY page into ONE
subdocument, so the canvas is as large as the document can make it, and every
page counts as a text page (no low-text skipping). Document Intelligence reads
the PDF bytes and renders nothing, so it is not exercised.

Peak RSS is ru_maxrss of the child: cumulative, so the value after a phase is
the job peak so far. Measured on the host OS; a Linux container differs
somewhat (allocator, page cache), so leave headroom.

Usage:
    .venv/bin/python scripts/render_memory_probe.py                 # whole local corpus, worst 12
    .venv/bin/python scripts/render_memory_probe.py a.pdf b.pdf     # specific files
Env:
    TOP=12       how many of the corpus' worst candidates to run (by projected pixels)
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CORPUS_GLOBS = [
    "test_uploads/**/*.pdf",
    "bps_sanierer_input/**/*.pdf",
]


def _peak_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kibibytes.
    return peak / 2**20 if sys.platform == "darwin" else peak / 2**10


def _child(pdf: str) -> None:
    """Run one job's rendering in this process and print a JSON line."""
    os.environ["STORAGE_BACKEND"] = "local"
    import fitz
    from PIL import Image

    from core.ocr.ocr_mistral_v2 import MistralOCRProcessor
    from core.pipeline import Pipeline
    from core.product import load_product_config
    from core.storage.storage import LocalStorage
    import core.pipeline as pipeline_mod

    out: dict = {"pdf": pdf, "baseline_mib": round(_peak_mib())}
    with fitz.open(pdf) as doc:
        out["pages"] = len(doc)
        out["max_page_pt2"] = max(p.rect.width * p.rect.height for p in doc)

    work = Path(tempfile.mkdtemp(prefix="memprobe_"))

    class _NoOCR:
        single_engine_fallback = False

    t = time.monotonic()
    pipe = Pipeline(
        file_key=str(Path(pdf).resolve()),
        ocr_engine=_NoOCR(),
        product_config=load_product_config("bps"),
        storage=LocalStorage(),
        work_dir=work,
        output_prefix=str(work / "out"),
    )
    out["init_mib"] = round(_peak_mib())

    mistral = object.__new__(MistralOCRProcessor)
    mistral._process_image = lambda image_path: "x" * 300
    by_page = mistral._process_pdf(str(pipe.local_input_path))
    out["ocr_mib"] = round(_peak_mib())

    pipe.markdown_by_page = by_page
    pipe.markdown_with_pages_numbers = "\n\n---\n\n".join(
        f"--- PAGE {p} ---\n: {t}" for p, t in by_page.items()
    )

    pages = sorted(by_page)

    class _Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    class _Msg:
                        content = json.dumps({"invoice_pages": {"1": pages}})

                    class _Choice:
                        message = _Msg()

                    class _Resp:
                        choices = [_Choice()]
                        usage = None

                    return _Resp()

    pipeline_mod.AzureOpenAI = lambda **kwargs: _Client()
    pipe.analyze_document()
    out["analyze_mib"] = round(_peak_mib())

    pipe.split_document_into_invoices()
    out["split_mib"] = round(_peak_mib())

    canvas = work / "out" / f"{pipe.stem}_subdocument_1.png"
    if canvas.exists():
        with Image.open(canvas) as im:
            out["canvas_mpx"] = round(im.width * im.height / 1e6, 1)
    out["seconds"] = round(time.monotonic() - t, 1)
    print(json.dumps(out))


def _candidates(top: int) -> list[Path]:
    """The corpus' worst cases by projected render pixels (pages x largest page)."""
    import fitz

    scored = []
    seen = set()
    for pattern in CORPUS_GLOBS:
        for p in REPO_ROOT.glob(pattern):
            real = p.resolve()
            if real in seen or not p.is_file():
                continue
            seen.add(real)
            try:
                with fitz.open(p) as doc:
                    n = len(doc)
                    biggest = max(pg.rect.width * pg.rect.height for pg in doc)
            except Exception:
                continue
            scored.append((n * biggest, biggest, p))
    by_total = sorted(scored, key=lambda s: -s[0])[: top // 2]
    by_page = sorted(scored, key=lambda s: -s[1])[: top - len(by_total)]
    picked: dict[Path, None] = {}
    for _, _, p in by_total + by_page:
        picked[p] = None
    return list(picked)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _child(sys.argv[2])
        return 0

    files = [Path(a) for a in sys.argv[1:]] or _candidates(int(os.getenv("TOP", "12")))
    rows = []
    for pdf in files:
        proc = subprocess.run(
            [sys.executable, __file__, "--child", str(pdf)],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        line = next((l for l in reversed(proc.stdout.splitlines()) if l.startswith("{")), None)
        if proc.returncode != 0 or line is None:
            err = (proc.stderr.strip().splitlines() or ["?"])[-1]
            rows.append({"pdf": str(pdf), "error": err[:200]})
            print(f"{pdf.name}: ERROR {err[:200]}", file=sys.stderr)
            continue
        row = json.loads(line)
        rows.append(row)
        a4 = 595 * 842
        print(f"{pdf.name[:40]:40} pages={row['pages']:3} maxpage={row['max_page_pt2'] / a4:5.1f}xA4 "
              f"init={row['init_mib']:5} ocr={row['ocr_mib']:5} analyze={row['analyze_mib']:5} "
              f"split={row['split_mib']:5} MiB  canvas={row.get('canvas_mpx', '?')} Mpx  {row['seconds']}s",
              file=sys.stderr)
    out = REPO_ROOT / "temp" / "render_memory_probe.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    peaks = [r["split_mib"] for r in rows if "split_mib" in r]
    print(f"max job peak: {max(peaks) if peaks else '?'} MiB over {len(peaks)} documents -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
