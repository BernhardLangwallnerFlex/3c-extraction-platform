"""Mistral OCR engine returning (markdown, markdown_by_page) tuple."""
import os
import fitz
import tempfile
from pathlib import Path
from typing import Union
from mistralai import Mistral
from utils import encode_image_to_base64


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
                pix = page.get_pixmap(dpi=200)
                tmp_img = tempfile.mktemp(suffix=".png")
                pix.save(tmp_img)
                try:
                    markdown_by_page[page_num + 1] = self._process_image(tmp_img)
                finally:
                    if os.path.exists(tmp_img):
                        os.remove(tmp_img)
        return markdown_by_page
