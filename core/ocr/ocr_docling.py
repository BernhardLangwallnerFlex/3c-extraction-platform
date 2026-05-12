from docling.document_converter import DocumentConverter
from pathlib import Path
import fitz  # PyMuPDF
import tempfile
import os
from invoice import Invoice
from typing import Union


class DoclingOCR:
    def __init__(self, name="docling_ocr"):
        """
        Initialize Docling OCR processor
        
        Args:
            name: Name identifier for this OCR processor
        """
        self.converter = DocumentConverter()
        self.name = name
    
    def extract_text(self, invoice: Invoice):
        """
        Extract text from document using Docling.
        
        Args:
            invoice: Invoice object with local_input_path attribute
            
        Returns:
            Tuple (markdown, markdown_by_page) where markdown_by_page is a dict
            mapping page numbers (1-indexed) to markdown strings
        """
        p = Path(invoice.local_input_path)  # ensure Path
        
        # Get full markdown
        result = self.converter.convert(str(p))
        markdown = result.document.export_to_markdown()
        
        # Get page-by-page markdown
        markdown_by_page = self._extract_by_page(str(p))
        
        return markdown, markdown_by_page
    
    def _extract_by_page(self, file_path: str) -> dict[int, str]:
        """
        Extract markdown for each page separately
        
        Args:
            file_path: Path to PDF or image file
            
        Returns:
            Dictionary mapping page number (1-indexed) to markdown content
        """
        markdown_by_page = {}
        
        # Check if it's a PDF
        if file_path.lower().endswith('.pdf'):
            try:
                with fitz.open(file_path) as doc:
                    num_pages = len(doc)
                    
                    # Process each page separately
                    for page_num in range(num_pages):
                        # Create a temporary PDF with just this page
                        temp_pdf = tempfile.mktemp(suffix=".pdf")
                        temp_doc = fitz.open()
                        temp_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)
                        temp_doc.save(temp_pdf)
                        temp_doc.close()
                        
                        try:
                            # Convert this single page
                            result = self.converter.convert(temp_pdf)
                            page_markdown = result.document.export_to_markdown()
                            markdown_by_page[page_num + 1] = page_markdown
                        finally:
                            # Clean up temporary file
                            if os.path.exists(temp_pdf):
                                os.remove(temp_pdf)
            except Exception as e:
                raise Exception(f"Error processing PDF pages {file_path}: {str(e)}")
        else:
            # For images, there's only one page
            try:
                result = self.converter.convert(file_path)
                markdown = result.document.export_to_markdown()
                markdown_by_page[1] = markdown
            except Exception as e:
                raise Exception(f"Error processing image {file_path}: {str(e)}")
        
        return markdown_by_page


class DoclingPDFOCRExtractor:
    """
    Pragmatic OCR runner for a single local PDF (or image) file path.

    This mirrors `DoclingOCR.extract_text()` but avoids the `Invoice` dependency,
    which is handy for quick testing.
    """

    def __init__(self, name: str = "docling_ocr_local"):
        self.converter = DocumentConverter()
        self.name = name

    def extract_text(self, pdf_path: Union[str, Path]):
        p = Path(pdf_path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"Input file not found: {p}")

        # Get full markdown
        result = self.converter.convert(str(p))
        markdown = result.document.export_to_markdown()

        # Get page-by-page markdown
        markdown_by_page = self._extract_by_page(str(p))

        return markdown, markdown_by_page

    def _extract_by_page(self, file_path: str) -> dict[int, str]:
        """
        Extract markdown for each page separately

        Args:
            file_path: Path to PDF or image file

        Returns:
            Dictionary mapping page number (1-indexed) to markdown content
        """
        markdown_by_page: dict[int, str] = {}

        # Check if it's a PDF
        if file_path.lower().endswith(".pdf"):
            try:
                with fitz.open(file_path) as doc:
                    num_pages = len(doc)

                    # Process each page separately
                    for page_num in range(num_pages):
                        # Create a temporary PDF with just this page
                        temp_pdf = tempfile.mktemp(suffix=".pdf")
                        temp_doc = fitz.open()
                        temp_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)
                        temp_doc.save(temp_pdf)
                        temp_doc.close()

                        try:
                            # Convert this single page
                            result = self.converter.convert(temp_pdf)
                            page_markdown = result.document.export_to_markdown()
                            markdown_by_page[page_num + 1] = page_markdown
                        finally:
                            # Clean up temporary file
                            if os.path.exists(temp_pdf):
                                os.remove(temp_pdf)
            except Exception as e:
                raise Exception(f"Error processing PDF pages {file_path}: {str(e)}")
        else:
            # For images, there's only one page
            try:
                result = self.converter.convert(file_path)
                markdown = result.document.export_to_markdown()
                markdown_by_page[1] = markdown
            except Exception as e:
                raise Exception(f"Error processing image {file_path}: {str(e)}")

        return markdown_by_page

