from processors.gpt_processor import GPTInvoiceProcessor
from ocr.ocr_tesseract import TesseractOCR
from ocr.ocr_mistral import MistralOCR
from ocr.ocr_googlevision import GoogleOCR
from ocr.ocr_agentic import OCRAgenticProcessor
from invoice import Invoice
from dotenv import load_dotenv
import os
from pathlib import Path
from processors.azure_processor import AzureInvoiceProcessor
from storage.storage import S3Storage, AzureBlobStorage

# Load API key from .env
load_dotenv()
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")

input_folder = "3C_testdaten_pdf/"
output_folder = "3C_testdaten_json/"

# get list of image files (.jpg, .jpeg, .png) in input_folder
files = [f for f in os.listdir(input_folder) if f.lower().endswith(('.pdf'))]
files.sort()
file = "230041495V_Splitt.pdf"
file_string = file.split(".")[0]
file_path = input_folder + file

# initialize OCR engines
agentic_ocr_engine = OCRAgenticProcessor(name = "agentic_ocr")

#storage = S3Storage(region_name="eu-central-1")
storage = AzureBlobStorage(account_name=os.getenv("AZURE_STORAGE_ACCOUNT_NAME"), account_key=os.getenv("AZURE_STORAGE_ACCOUNT_KEY"))

inv = Invoice(
    file_key="az://invoices/230075012T_Splitt.pdf",
    ocr_engine=agentic_ocr_engine,
    storage=storage,
    output_prefix="az://invoices/processed/"  # see note below
)

inv.extract_markdown()
inv.analyze_document()
print(inv.analysis_dict)
inv.split_document_into_invoices()

# initialize GPT processor
#processor = GPTInvoiceProcessor(
#    name="gpt_processor",
#    api_key=os.getenv("OPENAI_API_KEY"),
#    model="gpt-4",
#    vision_model="gpt-4o"  # or "gpt-4.1", or whatever OpenAI supports for vision in your account
#)

azure_processor = AzureInvoiceProcessor(
    name="azure_processor",
    api_key=AZURE_OPENAI_API_KEY,
    model="gpt-4.1",
    vision_model="gpt-4o",
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_OPENAI_API_VERSION
)

inv.extract_data_from_subdocuments(azure_processor)
print(inv.extraction_result_json)
print(type(inv.extraction_result_json))