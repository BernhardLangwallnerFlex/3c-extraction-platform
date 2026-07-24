"""Ad-hoc, local single-file run of the CURRENT pipeline (dual OCR + product config).

Mirrors production wiring in core/jobs/tasks.py: DualOCRProcessor (Mistral +
Azure Doc Intel), the product's extract prompt/schema via ProductConfig, and
gpt-5.4 for text+vision. Runs against a local PDF with LocalStorage so no blob
upload is needed; artifacts land in ./temp/.

Edit PRODUCT / FILE below to change what runs.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from core.ocr.ocr_dual import DualOCRProcessor
from core.pipeline import Pipeline
from core.processors.azure_processor import AzureInvoiceProcessor
from core.product import load_product_config
from core.storage.storage import LocalStorage

load_dotenv()

PRODUCT = "vetcostcheck"
FILES = [
    "3C_testdaten_pdf/230041495V_Splitt.pdf",
    "3C_testdaten_pdf/230068612T_Splitt.pdf",
    "3C_testdaten_pdf/230072869L_Splitt.pdf",
    "3C_testdaten_pdf/230074801Z_Splitt.pdf",
    "3C_testdaten_pdf/230074893V_Splitt.pdf",
]

product_config = load_product_config(PRODUCT)
storage = LocalStorage(base_dir=Path.cwd())
dual_ocr_engine = DualOCRProcessor(name="dual_ocr")
azure_processor = AzureInvoiceProcessor(
    name="azure_processor",
    api_key=os.getenv("AZURE_OPENAI_KEY"),
    model=os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4"),
    vision_model=os.getenv("OPENAI_VISION_MODEL", "gpt-5.4"),
    azure_endpoint=os.getenv("AZURE_ENDPOINT"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
)


def run_one(file: str) -> None:
    print(f"\n{'='*80}\nFILE: {file}\n{'='*80}")
    inv = Pipeline(
        file_key=file,
        ocr_engine=dual_ocr_engine,
        product_config=product_config,
        storage=storage,
        output_prefix="temp",
    )
    inv.extract_markdown()
    inv.analyze_document()
    inv.split_document_into_invoices()
    inv.extract_data_from_subdocuments(azure_processor)

    result = inv.extraction_result_json
    if isinstance(result, str):
        result = json.loads(result)
    print("  items (name | got.raw):")
    got_seen = 0
    for doc in result.get("subdocuments", []):
        sub = json.loads(doc) if isinstance(doc, str) else doc
        for item in (sub.get("items") or []):
            got = item.get("got") or {}
            raw = got.get("raw")
            if raw:
                got_seen += 1
            print(f"    {item.get('name')!r}  ||  got.raw={raw}")
    print(f"  -> {got_seen} item(s) carry a GOT reference")


for f in FILES:
    try:
        run_one(f)
    except Exception as e:  # noqa: BLE001
        print(f"  !! FAILED on {f}: {type(e).__name__}: {e}")
