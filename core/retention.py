"""Selection rules for the one-off blob backlog purge.

Kept separate from scripts/purge_blob_backlog.py so the rules can be unit-tested
without an Azure connection.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

# Prefixes belonging to the decommissioned pre-multi-product deployment. Nothing
# should write here any more, so everything under them goes regardless of age.
# The trailing slash matters: without it "uploads/" would also match
# "uploads-bps/" and friends.
LEGACY_PREFIXES = ("processed/", "uploads/")


def select_for_deletion(
    blobs: Iterable[tuple[str, datetime]],
    now: datetime,
    cutoff_days: int = 14,
) -> list[str]:
    """Return the names of the blobs that should be deleted.

    `blobs` yields (name, last_modified) pairs; last_modified must be
    timezone-aware, as the Azure SDK returns it.

    Rules:
      - under a legacy prefix: delete, whatever its age
      - anything else (per-product prefixes, root-level strays, prefixes added
        later): delete only if strictly older than `cutoff_days`
    """
    cutoff = now - timedelta(days=cutoff_days)
    doomed = []
    for name, last_modified in blobs:
        if name.startswith(LEGACY_PREFIXES) or last_modified < cutoff:
            doomed.append(name)
    return doomed
