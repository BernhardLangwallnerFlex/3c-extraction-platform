"""One-off purge of the blob backlog in the `invoices` container.

Dry-run by default; --apply actually deletes. Blob soft delete is enabled on the
account (7 days, allowPermanentDelete=false), so an --apply run is recoverable
within that window.

    python scripts/purge_blob_backlog.py              # report only
    python scripts/purge_blob_backlog.py --apply      # delete
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

from core.retention import select_for_deletion


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: dry run)")
    parser.add_argument("--cutoff-days", type=int, default=14)
    parser.add_argument("--container", default="invoices")
    args = parser.parse_args()

    load_dotenv()
    account = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
    key = os.environ["AZURE_STORAGE_ACCOUNT_KEY"]
    container = BlobServiceClient(
        f"https://{account}.blob.core.windows.net", credential=key
    ).get_container_client(args.container)

    blobs = [(b.name, b.last_modified, b.size or 0) for b in container.list_blobs()]
    doomed = set(select_for_deletion(
        [(name, modified) for name, modified, _ in blobs],
        now=datetime.now(timezone.utc),
        cutoff_days=args.cutoff_days,
    ))

    counts: dict[str, int] = defaultdict(int)
    sizes: dict[str, int] = defaultdict(int)
    oldest: dict[str, datetime] = {}
    newest: dict[str, datetime] = {}
    for name, modified, size in blobs:
        if name in doomed:
            bucket = name.split("/")[0] if "/" in name else "(root)"
            counts[bucket] += 1
            sizes[bucket] += size
            if bucket not in oldest or modified < oldest[bucket]:
                oldest[bucket] = modified
            if bucket not in newest or modified > newest[bucket]:
                newest[bucket] = modified

    print(f"account={account} container={args.container} "
          f"cutoff_days={args.cutoff_days}")
    print(f"total={len(blobs)} selected={len(doomed)} keeping={len(blobs) - len(doomed)}")
    for bucket in sorted(counts):
        date_range = f"{oldest[bucket]:%Y-%m-%d} .. {newest[bucket]:%Y-%m-%d}"
        print(f"  {bucket:26} {counts[bucket]:5d} blobs  {sizes[bucket] / 1048576:8.1f} MB  {date_range}")

    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply to delete.")
        return

    deleted = failed = 0
    for name in sorted(doomed):
        try:
            container.delete_blob(name)
            deleted += 1
        except Exception as exc:  # keep going; report at the end
            failed += 1
            print(f"FAILED {name}: {exc}")
    print(f"\ndeleted={deleted} failed={failed}")


if __name__ == "__main__":
    main()
