import os
from pathlib import Path
from dotenv import load_dotenv

from storage.file_storage import get_file_key  # <-- NEW (was get_file_path)

from storage.storage import LocalStorage, S3Storage, AzureBlobStorage  # adjust import to your actual module names
from invoice import Invoice

from ocr.ocr_agentic import OCRAgenticProcessor
from ocr.ocr_docling import DoclingOCR
from processors.gpt_processor import GPTInvoiceProcessor
from utils import ensure_json_serializable

load_dotenv()


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _build_storage():
    """
    Decide storage backend from env. Keep it dead simple:
    STORAGE_BACKEND=local|s3|azure
    """
    backend = os.getenv("STORAGE_BACKEND", "local").lower()

    if backend == "s3":
        region = os.getenv("AWS_DEFAULT_REGION", "eu-central-1")
        return S3Storage(region_name=region)

    if backend == "azure":
        return AzureBlobStorage(
            account_name=_require_env("AZURE_STORAGE_ACCOUNT_NAME"),
            account_key=_require_env("AZURE_STORAGE_ACCOUNT_KEY"),
        )

    # default: local
    base_dir = Path(os.getenv("LOCAL_STORAGE_BASE_DIR", Path.cwd()))
    return LocalStorage(base_dir=base_dir)


def _validate_output_prefix(output_prefix: str) -> None:
    backend = os.getenv("STORAGE_BACKEND", "local").lower()
    prefix = output_prefix.rstrip("/")

    if backend == "s3" and not prefix.startswith("s3://"):
        raise RuntimeError(
            f"STORAGE_BACKEND is 's3' but OUTPUT_PREFIX is '{output_prefix}'. "
            "Set OUTPUT_PREFIX like 's3://<bucket>/processed'."
        )
    if backend == "azure" and not prefix.startswith("az://"):
        raise RuntimeError(
            f"STORAGE_BACKEND is 'azure' but OUTPUT_PREFIX is '{output_prefix}'. "
            "Set OUTPUT_PREFIX like 'az://<container>/processed'."
        )
    if backend not in {"s3", "azure"} and (prefix.startswith("s3://") or prefix.startswith("az://")):
        raise RuntimeError(
            f"OUTPUT_PREFIX is '{output_prefix}' but STORAGE_BACKEND is '{backend}'. "
            "Either set STORAGE_BACKEND to match, or use a local folder like 'outputs'."
        )


def process_file(file_id: str):
    # 1) Resolve file_id -> storage key (local path or s3://...)
    invoice = None
    try:
        file_key = get_file_key(file_id)

        # 2) Build storage backend
        storage = _build_storage()

        # 3) Engines / processors
        agentic_ocr_engine = OCRAgenticProcessor(name="agentic_ocr")
        docling_ocr_engine = DoclingOCR(name="docling_ocr")

        processor = GPTInvoiceProcessor(
            name="gpt_processor",
            api_key=os.getenv("OPENAI_API_KEY"),
            model=os.getenv("OPENAI_TEXT_MODEL", "gpt-4"),
            vision_model=os.getenv("OPENAI_VISION_MODEL", "gpt-4o"),
        )

        # 4) Output prefix (local folder or s3 prefix)
        #    Examples:
        #      local: output_prefix="outputs"
        #      s3:    output_prefix="s3://my-bucket/processed/invoices"
        backend = os.getenv("STORAGE_BACKEND", "local").lower()
        if backend == "s3":
            output_prefix = os.getenv("S3_OUTPUT_PREFIX")
        elif backend == "azure":
            output_prefix = os.getenv("AZURE_OUTPUT_PREFIX")
        else:
            output_prefix = Path(os.getenv("LOCAL_STORAGE_BASE_DIR", Path.cwd()))
        _validate_output_prefix(output_prefix)

        # 5) Run pipeline
        invoice = Invoice(
            file_key=file_key,
            ocr_engine=agentic_ocr_engine,
            storage=storage,
            output_prefix=output_prefix,
        )

        invoice.extract_markdown()
        invoice.analyze_document()
        invoice.split_document_into_invoices()
        invoice.extract_data_from_subdocuments(processor)

    # (optional) keep artifacts in S3 but remove local temps
    # invoice.cleanup_temporary_files()  # enable if desired
    finally:    
        if invoice is not None:
            invoice.cleanup_local()
        else:
            print("Invoice is None")

    return ensure_json_serializable(invoice.extraction_result_json)