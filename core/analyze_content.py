"""Building the analyze request: which pages get an image, and how pages are labelled.

Pure functions, no I/O, so the pipeline and the offline experiment
(scripts/analyze_image_experiment.py) build byte-identical requests.

Background: gpt-5.4 accepts at most 50 images per request, and analyze sends
one per page — so every document over 50 pages failed (spec
docs/superpowers/specs/2026-10-06-analyze-low-text-images-design.md). Many of
those pages are damage photos with no text, whose images add little to the
page grouping.
"""
from __future__ import annotations

import re
from typing import Iterable

MAX_ANALYZE_IMAGES = 50

# Reviewed German, shown to the model in the page's text section.
LOW_TEXT_MARKER = (
    "[Kaum Text erkannt ({chars} Zeichen) – vermutlich Foto oder Leerseite. "
    "Bild nicht mitgesendet.]"
)

# DualOCRProcessor labels each engine's output with one of these headers.
_SOURCE_HEADER = re.compile(r"^--- OCR Source [^\n]*---$", re.MULTILINE)
# Mistral writes a placeholder like ![img-0.jpeg](img-0.jpeg) for each photo.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_WHITESPACE = re.compile(r"\s+")


def count_ocr_chars(page_markdown: str) -> int:
    """Characters of real text on a page, taking the larger of the two engines.

    The max rather than the sum keeps a page that fell back to one OCR engine
    on the same scale as a page both engines read.
    """
    parts = _SOURCE_HEADER.split(page_markdown or "")
    counts = [len(_WHITESPACE.sub("", _MD_IMAGE.sub("", part))) for part in parts]
    return max(counts, default=0)


def low_text_page_chars(markdown_by_page: dict[int, str], threshold: int | None) -> dict[int, int]:
    """Page -> char count for every page below `threshold`. Empty when disabled."""
    if threshold is None:
        return {}
    counts = {page: count_ocr_chars(md) for page, md in markdown_by_page.items()}
    return {page: n for page, n in counts.items() if n < threshold}


def build_pages_markdown(markdown_by_page: dict[int, str], low_text: dict[int, int]) -> str:
    """The page-numbered OCR text the analyze prompt embeds.

    With `low_text` empty this is exactly the string Pipeline.extract_markdown
    builds, which is what keeps VCC's request unchanged.
    """
    sections = []
    for page, txt in markdown_by_page.items():
        section = f"--- PAGE {page} ---\n: {txt}"
        if page in low_text:
            section += "\n" + LOW_TEXT_MARKER.format(chars=low_text[page])
        sections.append(section)
    return "\n\n---\n\n".join(sections)


def select_image_pages(
    page_numbers: Iterable[int],
    low_text: dict[int, int],
    cap: int = MAX_ANALYZE_IMAGES,
) -> tuple[list[int], bool]:
    """Pages that get an image, and whether the cap removed all of them.

    All-or-nothing on purpose: text-only analyze is a proven path (the
    content-policy fallback), "the best 50" would be a new judgement call.
    """
    pages = [p for p in page_numbers if p not in low_text]
    if len(pages) > cap:
        return [], True
    return pages, False


def build_analyze_blocks(prompt: str, page_images: list[tuple[int, str]], label_images: bool) -> list[dict]:
    """Prompt text block, then one `detail: low` image per entry.

    `label_images` puts a `Seite N:` text block before each image. Needed once
    any page is skipped: position alone no longer says which page an image is.
    """
    blocks: list[dict] = [{"type": "text", "text": prompt}]
    for page, b64 in page_images:
        if label_images:
            blocks.append({"type": "text", "text": f"Seite {page}:"})
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
        })
    return blocks
