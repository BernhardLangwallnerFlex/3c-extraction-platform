from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
import os
import json
import logging
import tempfile
import fitz
from PIL import Image
import pytesseract
from dotenv import load_dotenv
from openai import AzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from core.utils import log_retry, sampling_params
import shutil
import sentry_sdk
from core.ocr.base_ocr import BaseOCREngine
from core.utils import extract_json_from_response, strip_ocr_element_ids

log = logging.getLogger(__name__)
import structlog
# structlog logger for LLM-call telemetry (token counts), matching the "llm_call"
# events emitted by processors.azure_processor so cost/observability tooling sees
# the analyze step too.
_telemetry = structlog.get_logger()
from core.prompt_building.prompt_building import build_prompt_for_analyze_document
from core.product import ProductConfig
from core.returncode import apply_returncode_floor

from core.storage.storage import StorageBackend, LocalStorage, StorageKey

load_dotenv()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30),
       before_sleep=log_retry, reraise=True)
def _call_analyze_llm(client, model, content_blocks):
    """Call Azure OpenAI for document analysis with retry on transient failures."""
    return client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content_blocks}],
        **sampling_params(model),
    )


@dataclass
class SubdocumentArtifact:
    document_number: int
    page_numbers: list[int]
    markdown: str

    # storage keys (could be local paths or s3://...)
    md_key: StorageKey
    pdf_key: StorageKey
    image_key: StorageKey


class Pipeline:
    def __init__(
        self,
        file_key: StorageKey,
        ocr_engine: BaseOCREngine,
        product_config: ProductConfig,
        storage: StorageBackend | None = None,
        work_dir: Path | None = None,
        output_prefix: str = "temp",  # where to put subdocs + outputs within the storage
    ):
        self.file_key = file_key
        self.ocr_engine = ocr_engine
        self.product_config = product_config
        self.storage = storage or LocalStorage()
        self.output_prefix = output_prefix

        self.work_dir = work_dir or Path(tempfile.mkdtemp(prefix="invoice_work_"))
        self.work_dir.mkdir(parents=True, exist_ok=True)

        # Materialize the source document locally for fitz/PIL/OCR tooling
        self.local_input_path = self.storage.materialize_to_local(file_key)

        self.markdown = ""
        self.markdown_by_page: dict[int, str] = {}
        self.markdown_with_pages_numbers = ""
        self.extraction_dict = {}
        self.analysis_dict = {}
        self.subdocuments: list[SubdocumentArtifact] = []

        self.file_type = "pdf" if self.local_input_path.suffix.lower() == ".pdf" else "image"

        # Fix page orientation before any OCR or image rendering
        self._fix_page_orientation()

        if self.file_type == "pdf":
            with fitz.open(self.local_input_path) as doc:
                self.page_number = len(doc)
        else:
            self.page_number = 1

        # A nice stable stem for output naming
        self.stem = self.local_input_path.stem

    def _fix_page_orientation(self):
        """Detect and correct rotated pages using Tesseract OSD.

        Overwrites self.local_input_path with a corrected version if any
        pages need rotation. Handles both PDFs (per-page) and images.
        """
        if self.file_type == "pdf":
            self._fix_pdf_orientation()
        else:
            self._fix_image_orientation()

    def _fix_pdf_orientation(self):
        """Check each page of a PDF for rotation and correct in-place."""
        needs_fix = False
        rotations = {}

        with fitz.open(self.local_input_path) as doc:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=150)
                mode = "RGB" if pix.alpha == 0 else "RGBA"
                img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
                rotation = self._detect_rotation(img)
                if rotation != 0:
                    needs_fix = True
                    rotations[i] = rotation
                    log.info("page_rotation_detected page=%d rotation=%d", i + 1, rotation)

        if not needs_fix:
            return

        # Re-open and apply rotations
        doc = fitz.open(self.local_input_path)
        for page_idx, rotation in rotations.items():
            page = doc[page_idx]
            page.set_rotation((page.rotation + rotation) % 360)
        corrected_path = self.work_dir / f"corrected_{self.local_input_path.name}"
        doc.save(str(corrected_path))
        doc.close()
        self.local_input_path = corrected_path

    def _fix_image_orientation(self):
        """Check a single image for rotation and correct in-place."""
        img = Image.open(self.local_input_path)
        rotation = self._detect_rotation(img)
        if rotation != 0:
            log.info("image_rotation_detected rotation=%d", rotation)
            # PIL rotate is counter-clockwise, Tesseract reports clockwise correction needed
            corrected = img.rotate(rotation, expand=True)
            corrected_path = self.work_dir / f"corrected_{self.local_input_path.name}"
            corrected.save(str(corrected_path))
            self.local_input_path = corrected_path

    @staticmethod
    def _detect_rotation(img: Image.Image) -> int:
        """Use Tesseract OSD to detect rotation angle. Returns 0, 90, 180, or 270."""
        try:
            osd = pytesseract.image_to_osd(img)
            for line in osd.split("\n"):
                if "Rotate:" in line:
                    return int(line.split(":")[1].strip())
        except Exception:
            # OSD fails on pages with too little text — assume no rotation
            pass
        return 0

    def extract_markdown(self):
        markdown, markdown_by_page = self.ocr_engine.extract_text(self)
        self.markdown = strip_ocr_element_ids(markdown)
        self.markdown_by_page = {p: strip_ocr_element_ids(txt) for p, txt in markdown_by_page.items()}
        self.markdown_with_pages_numbers = "\n\n---\n\n".join(
            [f"--- PAGE {page} ---\n: {txt}" for page, txt in markdown_by_page.items()]
        )

    def analyze_document(self):
        if self.product_config.analyze_prompt_builder is not None:
            prompt = self.product_config.analyze_prompt_builder(
                markdown_text=self.markdown_with_pages_numbers,
            )
        else:
            prompt = build_prompt_for_analyze_document(
                markdown_text=self.markdown_with_pages_numbers,
            )

        # Build multimodal content: text prompt + one low-res image per page
        content_blocks = [{"type": "text", "text": prompt}]

        if self.file_type == "pdf":
            with fitz.open(self.local_input_path) as doc:
                for page in doc:
                    pix = page.get_pixmap(dpi=150)
                    img_bytes = pix.tobytes("png")
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    content_blocks.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "low",
                        },
                    })

        client = AzureOpenAI(
            api_key=os.getenv("AZURE_OPENAI_KEY"),
            azure_endpoint=os.getenv("AZURE_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
        )
        analyze_model = os.getenv("OPENAI_VISION_MODEL", "gpt-5.4")
        response = _call_analyze_llm(client, analyze_model, content_blocks)
        _usage = getattr(response, "usage", None)
        if _usage is not None:
            _telemetry.info(
                "analyze_llm_call",
                model=analyze_model,
                prompt_tokens=_usage.prompt_tokens,
                completion_tokens=_usage.completion_tokens,
            )
        self.analysis_dict = extract_json_from_response(response.choices[0].message.content)

    def _subdoc_key(self, ext: str, document_number: int) -> str:
        base = self.output_prefix.rstrip("/")
        return f"{base}/{self.stem}_subdocument_{document_number}{ext}"

    def split_document_into_invoices(self):
        if self.file_type != "pdf":
            raise ValueError("split_document_into_invoices currently expects a PDF input.")

        # Map LLM invoice keys (may be invoice numbers like "73980/25-024544BP")
        # to sequential document numbers and preserve the mapping for subdocument_context lookup
        self._invoice_key_to_doc_number = {}

        with fitz.open(self.local_input_path) as doc:
            for seq_idx, (invoice_key, page_numbers) in enumerate(self.analysis_dict["invoice_pages"].items(), start=1):
                document_number = seq_idx
                self._invoice_key_to_doc_number[invoice_key] = document_number

                sub_md = "\n\n".join([self.markdown_by_page[p] for p in page_numbers])

                md_key = self._subdoc_key(".md", document_number)
                pdf_key = self._subdoc_key(".pdf", document_number)
                img_key = self._subdoc_key(".png", document_number)

                # 1) write markdown to storage
                self.storage.write_text(md_key, sub_md)

                # 2) create sub-pdf locally, then upload/store
                subdoc_pdf_local = self.work_dir / Path(pdf_key).name
                subdoc = fitz.open()
                for page_num in page_numbers:
                    subdoc.insert_pdf(doc, from_page=page_num - 1, to_page=page_num - 1)
                subdoc.save(subdoc_pdf_local)
                subdoc.close()

                self.storage.write_bytes(pdf_key, subdoc_pdf_local.read_bytes(), content_type="application/pdf")

                # 3) render pages into one concatenated image locally, then upload/store
                page_images: list[Image.Image] = []
                with fitz.open(subdoc_pdf_local) as subpdf:
                    for page in subpdf:
                        pix = page.get_pixmap(dpi=200)
                        mode = "RGB" if pix.alpha == 0 else "RGBA"
                        pil_image = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
                        page_images.append(pil_image)

                total_height = sum(img.height for img in page_images)
                max_width = max(img.width for img in page_images)
                concatenated = Image.new("RGB", (max_width, total_height), color=(255, 255, 255))
                y = 0
                for img in page_images:
                    concatenated.paste(img, (0, y))
                    y += img.height

                subdoc_img_local = self.work_dir / Path(img_key).name
                concatenated.save(subdoc_img_local)

                self.storage.write_bytes(img_key, subdoc_img_local.read_bytes(), content_type="image/png")

                self.subdocuments.append(
                    SubdocumentArtifact(
                        document_number=document_number,
                        page_numbers=page_numbers,
                        markdown=sub_md,
                        md_key=md_key,
                        pdf_key=pdf_key,
                        image_key=img_key,
                    )
                )

    def _extract_single_subdocument(self, subdoc, processor):
        """Extract data from a single subdocument. Thread-safe."""
        local_image = self.storage.materialize_to_local(subdoc.image_key)
        self.storage.materialize_to_local(subdoc.pdf_key)

        ocr_text = subdoc.markdown

        # Resolve the original invoice key for this subdocument
        doc_num_to_key = {v: k for k, v in getattr(self, '_invoice_key_to_doc_number', {}).items()}
        invoice_key = doc_num_to_key.get(subdoc.document_number)

        # Per-subdocument context produced by the product's analyze override
        # (e.g. vetcostcheck fills it with the animals on each sub-invoice; BPS
        # leaves it absent). Core is domain-agnostic — it only slices the map by
        # document key and falls back to a global blob.
        subdocument_context_map = self.analysis_dict.get("subdocument_context", {})
        subdocument_context = None
        if subdocument_context_map:
            if invoice_key and invoice_key in subdocument_context_map:
                subdocument_context = subdocument_context_map[invoice_key]
            elif str(subdoc.document_number) in subdocument_context_map:
                subdocument_context = subdocument_context_map[str(subdoc.document_number)]
        if subdocument_context is None:
            subdocument_context = self.analysis_dict.get("subdocument_context_global")

        # Look up expected item count from analysis step
        item_counts = self.analysis_dict.get("invoice_number_of_items", {})
        expected_items = None
        if invoice_key and invoice_key in item_counts:
            expected_items = item_counts[invoice_key]
        elif str(subdoc.document_number) in item_counts:
            expected_items = item_counts[str(subdoc.document_number)]

        result = processor.extract(
            str(local_image),
            use_ocr=True,
            use_vision=True,
            markdown_text=subdoc.markdown,
            prompt=self.product_config.extract_prompt_builder(
                ocr_text=ocr_text,
                subdocument_context=subdocument_context,
                expected_items=expected_items,
            ),
            subdocument_context=subdocument_context,
        )
        if self.product_config.postprocess_extraction is not None:
            result = self.product_config.postprocess_extraction(result)
        # Unconditional and last: all three products need the identical
        # guarantee, and VCC already occupies the postprocess hook. Running
        # after the hook means the floor judges the values the consumer will
        # actually receive.
        return apply_returncode_floor(result)

    def extract_data_from_subdocuments(self, processor):
        from concurrent.futures import ThreadPoolExecutor

        extraction_result_json = {"number_of_subdocuments": len(self.subdocuments)}

        if len(self.subdocuments) <= 1:
            extraction_dicts = [
                self._extract_single_subdocument(sd, processor)
                for sd in self.subdocuments
            ]
        else:
            with ThreadPoolExecutor(max_workers=len(self.subdocuments)) as pool:
                futures = [
                    pool.submit(self._extract_single_subdocument, sd, processor)
                    for sd in self.subdocuments
                ]
                extraction_dicts = [f.result() for f in futures]

        extraction_result_json["subdocuments"] = extraction_dicts
        self.extraction_result_json = extraction_result_json

        # store output JSON via storage too
        out_key = f"{self.output_prefix}/extracted_data_{self.stem}.json"
        self.storage.write_text(out_key, json.dumps(extraction_result_json, indent=4))

    def cleanup_storage_artifacts(self) -> int:
        """Delete every stored artifact for this job except the result JSON.

        Removes the upload blob and each subdocument's markdown, sub-PDF and page
        image. `extracted_data_<stem>.json` is deliberately kept — the 14-day
        lifecycle rule on the container expires it.

        Never raises. Extraction has already succeeded by the time this runs, so a
        storage hiccup must not turn a good job into a failed one: every key is
        deleted independently and failures are logged. Returns the number of keys
        actually deleted.
        """
        # Known-false set (not an allowlist of true-spellings): an unrecognized
        # value — a typo, a stray quote, an inline .env comment — must fail toward
        # the documented default (cleanup enabled), not silently disable it.
        if os.getenv("CLEANUP_ARTIFACTS", "true").strip().lower() in {"0", "false", "no", "off", "n", "f", ""}:
            _telemetry.info("artifact_cleanup_skipped", reason="CLEANUP_ARTIFACTS disabled")
            return 0

        # A vacuous extraction (analyze_document found no invoice pages) is exactly
        # the outcome a human most needs to reproduce. Don't destroy the only
        # evidence of it — bail out before touching the upload blob. Distinct event
        # name from the flag-disabled case above so alert rules don't have to
        # parse the `reason` string to tell "routine" from "worth investigating".
        if not self.subdocuments:
            _telemetry.warning(
                "artifact_cleanup_skipped_no_subdocuments",
                reason="no subdocuments — vacuous extraction, keeping upload for investigation",
                file_key=self.file_key,
            )
            return 0

        # Intermediates (subdoc md/pdf/image) are cheap to regenerate from the
        # upload on a retry. The upload itself is not recoverable once deleted, so
        # it must be deleted last: if the worker is killed mid-cleanup (job_timeout,
        # SIGTERM on scale-in/rollout), RQ retries the job, and the retry needs the
        # upload blob to still exist. Do not reorder this back to upload-first.
        keys = []
        for subdoc in self.subdocuments:
            keys.extend([subdoc.md_key, subdoc.pdf_key, subdoc.image_key])
        keys.append(self.file_key)

        deleted = 0
        failed = 0
        for key in keys:
            try:
                self.storage.delete(key)
                deleted += 1
            except Exception as exc:
                failed += 1
                _telemetry.warning("artifact_delete_failed", key=key, error=str(exc))

        _telemetry.info(
            "artifact_cleanup_completed", deleted=deleted, failed=failed, total=len(keys)
        )

        # Every delete failing points at credentials or permissions rather than a
        # stray missing blob — surface it the way DualOCR degradation is surfaced.
        if failed and not deleted:
            sentry_sdk.capture_message(
                f"Artifact cleanup deleted nothing: all {failed} deletes failed",
                level="warning",
            )

        return deleted
    
    def cleanup_local(self):
        try:
            shutil.rmtree(self.work_dir, ignore_errors=True)
        except Exception:
            pass