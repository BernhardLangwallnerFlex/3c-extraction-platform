"""Sweep a directory of PDFs and report the returncode of every subdocument.

The regression gate for the returncode feature: every PDF that IS a Beleg must
come back 100. A false 200 auto-cancels a legitimate claim, so this direction
of error is the expensive one — `--expect 100` makes it a hard failure.

Usage:
    PRODUCT_NAME=bps STORAGE_BACKEND=local \\
        .venv/bin/python scripts/returncode_sweep.py bps_sanierer_input/BPS_Input --expect 100

    # Documents that genuinely contain no Beleg:
    PRODUCT_NAME=bps STORAGE_BACKEND=local \\
        .venv/bin/python scripts/returncode_sweep.py bps_sanierer_input --expect 200

Exit code is 0 when every subdocument matched `--expect` (or when `--expect`
was omitted), 1 otherwise. Logs go to stderr; the table goes to stdout.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Must be set before importing anything that calls load_dotenv at import time.
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("LOCAL_STORAGE_BASE_DIR", str(REPO_ROOT / "temp"))
# Keep the artifacts: a surprising returncode is exactly what you want to look at.
os.environ["CLEANUP_ARTIFACTS"] = "false"

sys.path.insert(0, str(REPO_ROOT))

import structlog  # noqa: E402

structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def sweep_one(pdf: Path) -> list[dict]:
    """Run the full pipeline on one PDF; return its subdocument results."""
    from core.storage.file_storage import save_upload
    from core.jobs.tasks import process_file

    file_id = save_upload(pdf.read_bytes(), original_filename=pdf.name)
    result = process_file(file_id)
    return result.get("subdocuments", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="a PDF, or a directory of PDFs (non-recursive)")
    parser.add_argument("--expect", type=int, choices=[100, 200, 300],
                        help="fail the run if any subdocument differs from this code")
    args = parser.parse_args()

    if args.target.is_dir():
        pdfs = sorted(p for p in args.target.glob("*.pdf"))
    elif args.target.is_file():
        pdfs = [args.target]
    else:
        print(f"No such file or directory: {args.target}", file=sys.stderr)
        return 2

    if not pdfs:
        print(f"No PDFs found in {args.target}", file=sys.stderr)
        return 2

    print(f"{'PDF':<44} {'#':>3} {'code':>5}  reasons")
    print("-" * 100)

    failures = []
    for pdf in pdfs:
        try:
            subdocs = sweep_one(pdf)
        except Exception as exc:  # a crashed PDF is a sweep result, not a stop
            print(f"{pdf.name:<44} {'-':>3} {'ERR':>5}  {exc}")
            failures.append((pdf.name, None, f"pipeline raised: {exc}"))
            continue

        if not subdocs:
            # Task 2 makes this impossible for a PDF with any readable page.
            print(f"{pdf.name:<44} {'-':>3} {'none':>5}  no subdocuments returned")
            failures.append((pdf.name, None, "no subdocuments"))
            continue

        for idx, sd in enumerate(subdocs, start=1):
            code = sd.get("returncode")
            reasons = "; ".join(sd.get("returncodeReasons") or [])
            print(f"{pdf.name:<44} {idx:>3} {str(code):>5}  {reasons}")
            if args.expect is not None and code != args.expect:
                failures.append((pdf.name, idx, f"expected {args.expect}, got {code}"))

    print("-" * 100)
    if not failures:
        print(f"OK — {len(pdfs)} PDF(s) swept" + (f", all subdocuments {args.expect}" if args.expect else ""))
        return 0

    print(f"FAILED — {len(failures)} mismatch(es):")
    for name, idx, why in failures:
        where = f"{name} subdoc {idx}" if idx is not None else name
        print(f"  {where}: {why}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
