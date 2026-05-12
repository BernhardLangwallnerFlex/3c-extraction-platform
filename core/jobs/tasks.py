import os
import time
from pathlib import Path
from dotenv import load_dotenv
import structlog

from core.storage.file_storage import get_file_key

from core.storage.storage import LocalStorage, S3Storage, AzureBlobStorage
from core.pipeline import Pipeline

from core.ocr.ocr_dual import DualOCRProcessor
from core.processors.azure_processor import AzureInvoiceProcessor
from core.utils import ensure_json_serializable

load_dotenv()

log = structlog.get_logger()


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _build_storage():
    backend = os.getenv("STORAGE_BACKEND", "local").lower()

    if backend == "s3":
        region = os.getenv("AWS_DEFAULT_REGION", "eu-central-1")
        return S3Storage(region_name=region)

    if backend == "azure":
        return AzureBlobStorage(
            account_name=_require_env("AZURE_STORAGE_ACCOUNT_NAME"),
            account_key=_require_env("AZURE_STORAGE_ACCOUNT_KEY"),
        )

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
    job_start = time.monotonic()
    invoice = None
    log.info("job_started", file_id=file_id)

    try:
        file_key = get_file_key(file_id)
        storage = _build_storage()

        dual_ocr_engine = DualOCRProcessor(name="dual_ocr")

        processor = AzureInvoiceProcessor(
            name="azure_processor",
            api_key=os.getenv("AZURE_OPENAI_KEY"),
            model=os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4"),
            vision_model=os.getenv("OPENAI_VISION_MODEL", "gpt-5.4"),
            azure_endpoint=os.getenv("AZURE_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
        )

        backend = os.getenv("STORAGE_BACKEND", "local").lower()
        if backend == "s3":
            output_prefix = os.getenv("S3_OUTPUT_PREFIX")
        elif backend == "azure":
            output_prefix = os.getenv("AZURE_OUTPUT_PREFIX")
        else:
            output_prefix = os.getenv("LOCAL_STORAGE_BASE_DIR", str(Path.cwd()))
        _validate_output_prefix(output_prefix)

        invoice = Pipeline(
            file_key=file_key,
            ocr_engine=dual_ocr_engine,
            storage=storage,
            output_prefix=output_prefix,
        )
        log.info("invoice_created", file_id=file_id, pages=invoice.page_number, file_type=invoice.file_type)

        t = time.monotonic()
        invoice.extract_markdown()
        log.info("ocr_completed", file_id=file_id, duration_s=round(time.monotonic() - t, 2), pages=len(invoice.markdown_by_page))

        t = time.monotonic()
        invoice.analyze_document()
        subdoc_count = len(invoice.analysis_dict.get("invoice_pages", {}))
        log.info("analysis_completed", file_id=file_id, duration_s=round(time.monotonic() - t, 2),
                 subdocuments=subdoc_count,
                 invoice_number_of_items=invoice.analysis_dict.get("invoice_number_of_items"),
                 ocr_chars=len(invoice.markdown))

        t = time.monotonic()
        invoice.split_document_into_invoices()
        log.info("split_completed", file_id=file_id, duration_s=round(time.monotonic() - t, 2), subdocuments=len(invoice.subdocuments))

        t = time.monotonic()
        invoice.extract_data_from_subdocuments(processor)
        log.info("extraction_completed", file_id=file_id, duration_s=round(time.monotonic() - t, 2), subdocuments=len(invoice.subdocuments))

        total_duration = round(time.monotonic() - job_start, 2)
        log.info("job_completed", file_id=file_id, duration_s=total_duration, subdocuments=len(invoice.subdocuments))

    except Exception:
        log.exception("job_failed", file_id=file_id, duration_s=round(time.monotonic() - job_start, 2))
        raise
    finally:
        if invoice is not None:
            invoice.cleanup_local()

    return ensure_json_serializable(invoice.extraction_result_json)
