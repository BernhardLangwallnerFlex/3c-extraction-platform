"""Mistral OCR engine returning (markdown, markdown_by_page) tuple."""
import os
import fitz
import tempfile
from pathlib import Path
from typing import Union
from mistralai import Mistral
from tenacity import retry, stop_after_attempt, wait_exponential
from core.rendering import render_dpi_for
from core.utils import encode_image_to_base64, log_retry


class MistralOCRProcessor:
    def __init__(self, model="mistral-ocr-latest", name="mistral_ocr"):
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY environment variable required")
        self.client = Mistral(api_key=api_key)
        self.model = model
        self.name = name

    def extract_text(self, invoice):
        """Extract text from document, returning (markdown, markdown_by_page)."""
        p = Path(invoice.local_input_path)

        if p.suffix.lower() == ".pdf":
            markdown_by_page = self._process_pdf(str(p))
        else:
            md = self._process_image(str(p))
            markdown_by_page = {1: md}

        markdown = "\n\n".join(markdown_by_page.values())
        return markdown, markdown_by_page

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30),
           before_sleep=log_retry, reraise=True)
    def _process_image(self, image_path: str) -> str:
        b64 = encode_image_to_base64(image_path)
        ocr_response = self.client.ocr.process(
            model=self.model,
            document={
                "type": "image_url",
                "image_url": f"data:image/png;base64,{b64}",
            },
            include_image_base64=False,
        )
        return ocr_response.pages[0].markdown

    def _process_pdf(self, pdf_path: str) -> dict[int, str]:
        markdown_by_page = {}
        with fitz.open(pdf_path) as doc:
            for page_num in range(len(doc)):
                page = doc[page_num]
                # One page at a time, but a large-format page at a fixed 200 dpi
                # is still hundreds of megabytes.
                dpi = render_dpi_for([(page.rect.width, page.rect.height)], 200)
                pix = page.get_pixmap(dpi=dpi)
                tmp_img = tempfile.mktemp(suffix=".png")
                pix.save(tmp_img)
                del pix
                try:
                    markdown_by_page[page_num + 1] = self._process_image(tmp_img)
                finally:
                    if os.path.exists(tmp_img):
                        os.remove(tmp_img)
        return markdown_by_page
