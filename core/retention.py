"""Selection rules for the one-off blob backlog purge.

Kept separate from scripts/purge_blob_backlog.py so the rules can be unit-tested
without an Azure connection.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable


def select_for_deletion(
    blobs: Iterable[tuple[str, datetime]],
    now: datetime,
    cutoff_days: int = 14,
) -> list[str]:
    """Return the names of the blobs that should be deleted.

    `blobs` yields (name, last_modified) pairs; last_modified must be
    timezone-aware, as the Azure SDK returns it.

    Rule: delete a blob only if it is strictly older than `cutoff_days`,
    regardless of prefix (per-product prefixes, legacy `processed/`/`uploads/`
    prefixes, root-level strays, or prefixes added later all follow this same
    cutoff).
    """
    cutoff = now - timedelta(days=cutoff_days)
    doomed = []
    for name, last_modified in blobs:
        if last_modified < cutoff:
            doomed.append(name)
    return doomed
