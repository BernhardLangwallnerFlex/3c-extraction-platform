"""A/B harness: OCR quality vs. render resolution cap.

Today every render step picks its dpi from the PDF page's *physical* size. Phone
scanning apps routinely write an A4 photo as a 72 or 144 dpi image, so the page
claims to be ~1.6 x 2.3 m and we rasterise up to ~200 Mpx for an ordinary letter
— 7x more pixels than the photo has, and far over Mistral's own pixel limit.

This harness renders each page under several conditions and runs Mistral OCR on
each render (the only OCR input our rendering controls — Document Intelligence
reads the original PDF bytes):

  current      — exactly what ocr_mistral_v2._process_pdf does today
  cap<L>       — dpi chosen so the long side is <= L px, and never above the
                 native resolution of a page-filling embedded image

Reference text is Document Intelligence on the original single-page PDF (the
other engine production runs). Per page and condition we report: Mistral
success, px, PNG bytes, latency, whole-text similarity to the reference, and
recall of "key tokens" (amounts, dates, IBANs, long numbers) found in the
reference — the tokens extraction actually depends on.

Usage:
    .venv/bin/python scripts/render_cap_experiment.py [pdf ...]
Env:
    CAPS="2500,3500,5000"     # long-side caps in px
Artifacts land in temp/render_cap/; JSON summary on stdout.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import fitz  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from azure.ai.documentintelligence import DocumentIntelligenceClient  # noqa: E402
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat  # noqa: E402
from azure.core.credentials import AzureKeyCredential  # noqa: E402
from mistralai import Mistral  # noqa: E402

from core.rendering import render_dpi_for  # noqa: E402
from core.utils import encode_image_to_base64  # noqa: E402

OUT = REPO_ROOT / "temp" / "render_cap"
CAPS = [int(c) for c in os.getenv("CAPS", "2500,3500,5000").split(",")]

mistral = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
docintel = DocumentIntelligenceClient(
    endpoint=os.environ["AZURE_DOCINTEL_ENDPOINT"],
    credential=AzureKeyCredential(os.environ["AZURE_DOCINTEL_KEY"]),
)

KEY_PATTERNS = [
    r"\d{1,3}(?:\.\d{3})*,\d{2}",          # amounts 1.234,56
    r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b",      # dates
    r"\bDE\d{2}(?:\s?\d{4}){4}\s?\d{2}\b", # IBAN
    r"\b\d{5,}\b",                         # long numbers (invoice no., PLZ, accounts)
]


def key_tokens(text: str) -> set[str]:
    toks = set()
    for pat in KEY_PATTERNS:
        toks.update(re.sub(r"\s", "", m) for m in re.findall(pat, text))
    return toks


def norm(text: str) -> str:
    text = re.sub(r"[|#*_`>\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def native_dpi(page) -> float | None:
    """Effective dpi of an image covering >=90% of the page, if any."""
    area = page.rect.width * page.rect.height
    best = None
    for info in page.get_image_info():
        bx = fitz.Rect(info["bbox"]) & page.rect
        if bx.is_empty or bx.width * bx.height < 0.9 * area:
            continue
        dpi = info["width"] / (bx.width / 72)
        best = max(best or 0, dpi)
    return best


def dpi_for(page, cond: str) -> int:
    w, h = page.rect.width, page.rect.height
    if cond == "current":
        return render_dpi_for([(w, h)], 200)
    cap = int(cond[3:])
    dpi = min(200.0, cap / (max(w, h) / 72))
    nd = native_dpi(page)
    if nd:
        dpi = min(dpi, nd)
    return max(1, int(dpi))


def run_mistral(png: Path) -> tuple[str | None, str | None, float]:
    t0 = time.time()
    try:
        r = mistral.ocr.process(
            model="mistral-ocr-latest",
            document={"type": "image_url", "image_url": f"data:image/png;base64,{encode_image_to_base64(str(png))}"},
            include_image_base64=False,
        )
        return r.pages[0].markdown, None, time.time() - t0
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:200], time.time() - t0


def run_docintel(pdf: Path) -> str:
    poller = docintel.begin_analyze_document(
        "prebuilt-layout",
        body=AnalyzeDocumentRequest(bytes_source=pdf.read_bytes()),
        output_content_format=DocumentContentFormat.MARKDOWN,
    )
    return poller.result().content


def collect_pages(pdfs: list[Path]) -> list[dict]:
    """One entry per visually distinct page (duplicate pages are common)."""
    seen, pages = set(), []
    for pdf in pdfs:
        with fitz.open(pdf) as doc:
            for i, page in enumerate(doc):
                sig = hashlib.md5(page.get_pixmap(dpi=10).samples).hexdigest()
                if sig in seen:
                    continue
                seen.add(sig)
                one = OUT / f"{pdf.stem[:8]}_p{i + 1}.pdf"
                single = fitz.open()
                single.insert_pdf(doc, from_page=i, to_page=i)
                single.save(one)
                single.close()
                pages.append(dict(src=str(pdf), page=i + 1, pdf=one))
    return pages


def process_page(entry: dict) -> dict:
    pdf = entry["pdf"]
    ref = run_docintel(pdf)
    ref_n, ref_keys = norm(ref), key_tokens(ref)
    res = dict(src=entry["src"], page=entry["page"], ref_chars=len(ref_n), ref_keys=len(ref_keys), conds={})
    with fitz.open(pdf) as doc:
        page = doc[0]
        res["mm"] = (round(page.rect.width / 72 * 25.4), round(page.rect.height / 72 * 25.4))
        res["native_dpi"] = round(native_dpi(page) or 0)
        rendered = {}
        for cond in ["current"] + [f"cap{c}" for c in CAPS]:
            dpi = dpi_for(page, cond)
            if dpi in rendered:  # identical render to an earlier condition
                res["conds"][cond] = dict(res["conds"][rendered[dpi]], same_as=rendered[dpi])
                continue
            rendered[dpi] = cond
            png = OUT / f"{pdf.stem}_{cond}.png"
            pix = page.get_pixmap(dpi=dpi)
            pix.save(str(png))
            text, err, secs = run_mistral(png)
            c = dict(dpi=dpi, px=(pix.width, pix.height), mpx=round(pix.width * pix.height / 1e6, 1),
                     png_mb=round(png.stat().st_size / 1e6, 2), secs=round(secs, 1), error=err)
            del pix
            if text is not None:
                (OUT / f"{pdf.stem}_{cond}.md").write_text(text)
                found = key_tokens(text)
                c["sim"] = round(SequenceMatcher(None, ref_n, norm(text), autojunk=False).ratio(), 3)
                c["key_recall"] = round(len(ref_keys & found) / len(ref_keys), 3) if ref_keys else None
                c["missed_keys"] = sorted(ref_keys - found)
            res["conds"][cond] = c
    (OUT / f"{pdf.stem}_docintel.md").write_text(ref)
    return res


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    pdfs = [Path(p) for p in sys.argv[1:]]
    pages = collect_pages(pdfs)
    print(f"{len(pages)} distinct pages from {len(pdfs)} files", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(process_page, pages))
    (OUT / "results.json").write_text(json.dumps(results, indent=1))
    json.dump(results, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
