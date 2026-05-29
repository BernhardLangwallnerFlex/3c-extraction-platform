"""Run the full extraction pipeline on one local PDF for any product.

Usage:
    PRODUCT_NAME=bps STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 \
        python scripts/extract_local.py path/to/file.pdf
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Route the pipeline's structlog output to stderr so stdout carries only the
# extraction JSON (usable as `... | tee out.json`). core defaults to a
# PrintLoggerFactory on stdout, which would otherwise corrupt the JSON.
import structlog  # noqa: E402

structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/extract_local.py <pdf>", file=sys.stderr)
        return 2
    pdf = Path(sys.argv[1])
    if not pdf.exists():
        print(f"No such file: {pdf}", file=sys.stderr)
        return 2

    from core.storage.file_storage import save_upload
    from core.jobs.tasks import process_file

    with pdf.open("rb") as fh:
        file_id = save_upload(fh.read(), original_filename=pdf.name)
    result = process_file(file_id)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
