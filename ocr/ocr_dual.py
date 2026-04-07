"""Dual OCR engine combining Mistral OCR and Azure Document Intelligence.

Runs both engines in parallel and merges their outputs per page,
giving the downstream LLM two independent OCR sources to cross-reference.
Combined cost ~$0.0025/page (12x cheaper than LandingAI).
"""
import os
from concurrent.futures import ThreadPoolExecutor
from ocr.ocr_mistral_v2 import MistralOCRProcessor
from ocr.ocr_azure_docintel import AzureDocIntelOCR
from utils import strip_ocr_element_ids


class DualOCRProcessor:
    def __init__(self, name="dual_ocr"):
        self.mistral = MistralOCRProcessor(name="mistral_ocr")
        self.azure = AzureDocIntelOCR(name="azure_docintel_ocr")
        self.name = name

    def extract_text(self, invoice):
        """Run both OCR engines in parallel and merge results per page.

        Returns (markdown, markdown_by_page) with both outputs labelled.
        """
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_mistral = pool.submit(self.mistral.extract_text, invoice)
            future_azure = pool.submit(self.azure.extract_text, invoice)

            _, mistral_by_page = future_mistral.result()
            _, azure_by_page = future_azure.result()

        # Merge per page — union of all page numbers from both engines
        all_pages = sorted(set(mistral_by_page.keys()) | set(azure_by_page.keys()))

        markdown_by_page = {}
        for page_num in all_pages:
            parts = []
            mistral_text = strip_ocr_element_ids(mistral_by_page.get(page_num, ""))
            azure_text = strip_ocr_element_ids(azure_by_page.get(page_num, ""))

            if mistral_text:
                parts.append(f"--- OCR Source A (Mistral) ---\n{mistral_text}")
            if azure_text:
                parts.append(f"--- OCR Source B (Azure Document Intelligence) ---\n{azure_text}")

            markdown_by_page[page_num] = "\n\n".join(parts)

        markdown = "\n\n".join(markdown_by_page.values())
        return markdown, markdown_by_page
