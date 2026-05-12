import os
import fitz  # PyMuPDF
import tempfile
from core.ocr.base_ocr import BaseOCREngine
from core.utils import encode_image_to_base64, encode_pdf
from mistralai import Mistral

class MistralOCR(BaseOCREngine):
    def __init__(self, api_key: str = None):
        """Initialize Mistral OCR with API key"""
        if api_key is None:
            api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("Mistral API key is required. Set MISTRAL_API_KEY environment variable or pass it directly.")
        
        self.client = Mistral(api_key=api_key)
    
    def extract_text(self, file_path: str) -> str:
        """Extract text from image or PDF using Mistral OCR"""
        if file_path.lower().endswith((".jpg", ".jpeg", ".png")):
            return self._process_image(file_path)
        elif file_path.lower().endswith(".pdf"):
            return self._process_pdf(file_path)
        else:
            raise ValueError("Unsupported file format for MistralOCR")
    
    def _process_image(self, image_path: str) -> str:
        """Process a single image file"""
        try:
            # Encode image to base64
            base64_image = encode_image_to_base64(image_path)
            
            # Call Mistral OCR API
            ocr_response = self.client.ocr.process(
                model="mistral-ocr-latest",
                document={
                    "type": "image_url",
                    "image_url": f"data:image/jpeg;base64,{base64_image}" 
                },
                include_image_base64=True
            )
            
            return ocr_response.pages[0].markdown
            
        except Exception as e:
            raise Exception(f"Error processing image {image_path}: {str(e)}")
    
    def _process_pdf(self, pdf_path: str) -> str:
        """Process PDF by converting pages to images and processing each"""
        text = ""
        try:
            with fitz.open(pdf_path) as doc:
                for page_num in range(len(doc)):
                    # Convert PDF page to image
                    page = doc[page_num]
                    pix = page.get_pixmap(dpi=300)
                    tmp_img = tempfile.mktemp(suffix=".png")
                    pix.save(tmp_img)
                    
                    try:
                        # Process the page image
                        page_text = self._process_image(tmp_img)
                        text += f"\n\n--- PAGE {page_num + 1} ---\n\n" + page_text
                    finally:
                        # Clean up temporary file
                        if os.path.exists(tmp_img):
                            os.remove(tmp_img)
            
            return text
            
        except Exception as e:
            raise Exception(f"Error processing PDF {pdf_path}: {str(e)}")