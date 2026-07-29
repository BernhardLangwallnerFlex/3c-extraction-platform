# TODO

## Pipeline — Accuracy
- [ ] **Fix VAT hallucination on receipts/prescriptions**: LLM assumes 19% VAT on pharmacy receipts where the stated price is the final price. Needs prompt refinement or document-type-aware extraction rules.
- [ ] **Build ground truth set**: Manually verify extraction JSON for 5-10 invoices to measure accuracy quantitatively and catch regressions when changing models or OCR engines.
- [ ] **Use config-driven extraction prompt**: `get_full_prompt()` is hardcoded; `build_prompt_from_config()` exists but isn't used in the main pipeline.

## Pipeline — Performance
- [ ] **Parallelize Mistral OCR page processing** (high impact): Pages are OCR'd one at a time in `ocr/ocr_mistral_v2.py:45-58`. Each API call is I/O-bound (~0.5s), so a 20-page doc takes ~10s sequentially. Wrap in `ThreadPoolExecutor(max_workers=4)`. Azure Doc Intelligence already processes whole documents in one call.
- [ ] **Remove unused PDF download in extraction** (medium): `invoice.py:261` downloads each subdocument PDF from blob storage but never uses it. Remove the line — saves ~100-500ms per subdocument.
- [ ] **Keep sub-PDF in memory instead of save-close-reopen** (medium): `invoice.py:216-232` creates a fitz subdoc, saves to disk, closes, then reopens to render images. Keep the document open and render directly.
- [ ] **Skip temp files in Mistral OCR — encode in memory** (medium): `ocr/ocr_mistral_v2.py:50-57` saves pixmap to temp PNG then reads it back. Use `pix.tobytes("png")` → `base64.b64encode()` directly.
- [ ] **Cap ThreadPoolExecutor for subdocument extraction** (low): `invoice.py:312` sets `max_workers=len(self.subdocuments)` — cap at 5 to prevent rate limiting.
- [ ] **Reuse AzureOpenAI client in analyze_document()** (low): `invoice.py:176-180` creates a new client every call. Create once in `__init__()`.

## Pipeline — Redis

See [redis_resilience_plan.md](redis_resilience_plan.md) for context (Sentry `PYTHON-FASTAPI-9`, 2026-05-06) and full proposal. Deferred — system has been stable, pick up if the issue recurs.

- [ ] **Reuse a single Redis client via FastAPI lifespan + `Depends`**: Today every request to `/job/{job_id}`, `/process`, `/ready` builds a fresh `Redis.from_url(...)`. Move to one app-scoped client (`api/routes/job.py:15`, `api/routes/process.py:17`, `api/routes/health.py:23`).
- [ ] **Add redis-py–level retry on transient errors**: `Retry(ExponentialBackoff(), 3)` with `retry_on_timeout=True` and `retry_on_error=[ConnectionError, TimeoutError]`. Distinct from RQ's job-level `Retry`; would have absorbed the 2026-05-06 blip.
- [ ] **Worker keepalive + health check**: Add `socket_keepalive=True` and `health_check_interval=30` to the long-lived worker connection (`jobs/worker.py:34`) so reaped idle connections are detected before the next dequeue.
- [ ] **Collapse Redis client construction into one helper**: After the above, fold kwargs (timeouts, retry, keepalive) into a single factory shared by API lifespan and worker.
- [ ] **Consider Standard C0 if 503s recur**: Basic C0 is single-node, no SLA. Upgrade only if Sentry shows Redis-driven 503s in a 30-day window after the items above ship.

## Cleanup
- [x] **Remove LandingAI dependency**: Removed `landingai`/`landingai-ade` from requirements. OCR processor code kept for reference.
- [x] **Fix cleanup inconsistency**: Replaced `cleanup_temporary_files()` with `Pipeline.cleanup_storage_artifacts()`, called from `tasks.py` after a successful extraction.
- [ ] **Add caching / deduplication**: Reprocessing the same file reruns the full pipeline with no intermediate result caching.

## Done

- [x] **Worker scale-to-zero**: Set `--min-replicas 0` on `ca-invoice-worker` with KEDA Redis scaling (1200s cooldown). Saves ~€60/mo during idle periods.
- [x] **Redis downgrade to C0**: Migrated from Basic C1 (1 GB, ~€44/mo) to Basic C0 (250 MB, ~€15/mo). RQ metadata is tiny, C0 is plenty.
- [x] **Reduce image size/DPI sent to LLM**: Phase 3 renders at 300 DPI into a concatenated PNG sent as base64 to GPT-4o vision — large payload, more tokens, higher cost. Could reduce DPI or resize before sending.
- [x] **Improve invoice splitting accuracy**: Rewrote analysis prompt with explicit boundary rules (different invoice numbers = separate invoices, same sender doesn't mean same invoice, handle mixed pages). Tested on multi-invoice PDFs with same vet/client/animal.
- [x] **Add visual input to analysis step**: `analyze_document()` now sends 150 DPI page images alongside OCR text for visual boundary detection.
- [x] **Per-invoice animal association**: Added `invoice_animals` to analysis output. Animals are now routed only to the subdocument they belong to, no longer bleeding across invoices.
- [x] **Add `seed` to LLM calls**: All LLM calls (`invoice.py`, `azure_processor.py`, `gpt_processor.py`) now use `seed=42` + `temperature=0` for more deterministic output.
- [x] **Strip OCR element IDs**: Removed random LandingAI UUIDs (`<a id>` anchors, table cell IDs) from OCR markdown. ~23% token reduction and eliminates run-to-run variation from the OCR layer.
- [x] **Parallelize subdocument extraction**: Done — uses `ThreadPoolExecutor` to run all subdocument LLM calls concurrently. Total time = slowest single call instead of sum of all.
- [x] **Switch to gpt-5.4-mini**: Tested gpt-4o, gpt-5.4, gpt-5.4-mini, gpt-5.4-nano across 10 docs. gpt-5.4-mini is 3.2x faster, 3.3x cheaper ($0.75 vs $2.50/1M input), and extracts more items than gpt-4o. Azure deployment capacity set to 50.
- [x] **OCR engine benchmarking**: Compared LandingAI, Docling (EasyOCR), Mistral OCR 3, Azure Doc Intelligence across 10 docs with LLM-as-judge scoring. Mistral and Azure tied on quality, both 12-30x cheaper than LandingAI. Docling not production-ready (table/text accuracy too low).
- [x] **Built dual OCR engine**: `ocr/ocr_dual.py` runs Mistral + Azure Doc Intel in parallel, merges outputs per page. Drop-in replacement, ~$0.0025/page.
- [x] **Automatic page orientation fix**: Tesseract OSD detects rotated pages before OCR. Fixed upside-down receipt in VCC_Viele_Dokumente.pdf.
- [x] **Dual OCR end-to-end tested**: VCC_Viele_Dokumente.pdf — fixed truncated table (6→20 items), detected missing animal (Luci, Zwergpudel), captured pharmacy receipt as 4th subdocument.
- [x] **Hook dual OCR into production**: Swapped `OCRAgenticProcessor` for `DualOCRProcessor` in `tasks.py`. Added Mistral, Azure Doc Intel, pytesseract, tesseract-ocr to prod deps/Dockerfile.
