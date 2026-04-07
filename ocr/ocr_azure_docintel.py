"""Azure Document Intelligence OCR engine with markdown output."""
import os
from pathlib import Path
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat
from azure.core.credentials import AzureKeyCredential


class AzureDocIntelOCR:
    def __init__(self, name="azure_docintel_ocr", endpoint=None, key=None):
        endpoint = endpoint or os.getenv("AZURE_DOCINTEL_ENDPOINT")
        key = key or os.getenv("AZURE_DOCINTEL_KEY")
        if not endpoint or not key:
            raise ValueError(
                "Azure Doc Intelligence endpoint and key required. "
                "Set AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY."
            )
        self.client = DocumentIntelligenceClient(
            endpoint=endpoint,
            credential=AzureKeyCredential(key),
        )
        self.name = name

    def extract_text(self, invoice):
        """Extract text from document, returning (markdown, markdown_by_page)."""
        p = Path(invoice.local_input_path)
        data = p.read_bytes()

        poller = self.client.begin_analyze_document(
            "prebuilt-layout",
            body=AnalyzeDocumentRequest(bytes_source=data),
            output_content_format=DocumentContentFormat.MARKDOWN,
        )
        result = poller.result()

        markdown = result.content

        # Split into per-page markdown using page spans
        markdown_by_page = {}
        if result.pages:
            for page in result.pages:
                page_num = page.page_number
                if page.spans:
                    # Each span has offset and length into the full content
                    page_text_parts = []
                    for span in page.spans:
                        start = span.offset
                        end = span.offset + span.length
                        page_text_parts.append(markdown[start:end])
                    markdown_by_page[page_num] = "\n".join(page_text_parts)
                else:
                    markdown_by_page[page_num] = ""
        else:
            # Fallback: single page
            markdown_by_page[1] = markdown

        return markdown, markdown_by_page
