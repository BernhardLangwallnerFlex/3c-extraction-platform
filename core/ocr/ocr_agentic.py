import os
from landingai_ade import LandingAIADE
from dotenv import load_dotenv
import mimetypes
from invoice import Invoice
from pathlib import Path
from typing import Union
load_dotenv()


class OCRAgenticProcessor:
    def __init__(self, model_id="dpt-2-latest", name="agentic_ocr"):
        self.client = LandingAIADE(
            apikey=os.environ["VISION_AGENT_API_KEY"],
            environment="eu"
        )
        self.model_id = model_id
        self.name = name

    def extract_text(self, invoice: "Invoice"):
        p = Path(invoice.local_input_path)  # ensure Path

        data = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or "application/pdf"

        parse_res = self.client.parse(
            document=(p.name, data, mime),   # ✅ unambiguous file upload
            model=self.model_id,
            split="page",
        )

        markdown = parse_res.markdown
        n_pages = len(parse_res.splits)
        markdown_by_page = {i + 1: parse_res.splits[i].markdown for i in range(n_pages)}
        return markdown, markdown_by_page
    

class AgenticPDFOCRExtractor:
    """
    Pragmatic OCR runner for a single local PDF (or image) file path.

    This mirrors `OCRAgenticProcessor.extract_text()` but avoids the `Invoice`
    dependency, which is handy for quick testing.
    """

    def __init__(self, model_id: str = "dpt-2-latest", name: str = "agentic_ocr_local"):
        self.client = LandingAIADE(
            apikey=os.environ["VISION_AGENT_API_KEY"],
            environment="eu",
        )
        self.model_id = model_id
        self.name = name

    def extract_text(self, pdf_path: Union[str, Path]):
        p = Path(pdf_path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"Input file not found: {p}")

        data = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or "application/pdf"

        parse_res = self.client.parse(
            document=(p.name, data, mime),  # explicit file upload triple
            model=self.model_id,
            split="page",
        )

        markdown = parse_res.markdown
        n_pages = len(parse_res.splits)
        markdown_by_page = {i + 1: parse_res.splits[i].markdown for i in range(n_pages)}
        return markdown, markdown_by_page

