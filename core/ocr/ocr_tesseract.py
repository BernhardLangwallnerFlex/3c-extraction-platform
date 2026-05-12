from PIL import Image
import pytesseract
import fitz  # PyMuPDF
import tempfile
import os
from core.ocr.base_ocr import BaseOCREngine

class TesseractOCR(BaseOCREngine):
    def extract_text(self, file_path: str) -> str:
        if file_path.lower().endswith((".jpg", ".jpeg", ".png")):
            return pytesseract.image_to_string(Image.open(file_path))

        elif file_path.lower().endswith(".pdf"):
            text = ""
            with fitz.open(file_path) as doc:
                for page in doc:
                    pix = page.get_pixmap(dpi=300)
                    tmp_img = tempfile.mktemp(suffix=".png")
                    pix.save(tmp_img)
                    page_text = pytesseract.image_to_string(Image.open(tmp_img))
                    text += f"\n\n--- PAGE {page.number + 1} ---\n\n" + page_text
                    os.remove(tmp_img)
            return text

        else:
            raise ValueError("Unsupported file format for TesseractOCR")