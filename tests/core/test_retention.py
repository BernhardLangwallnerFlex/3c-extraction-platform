"""Selection rules for the one-off blob backlog purge."""
from datetime import datetime, timedelta, timezone

from core.retention import select_for_deletion

NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def test_legacy_prefixes_go_regardless_of_age():
    blobs = [
        ("uploads/abc.pdf", NOW),                                    # uploaded seconds ago
        ("processed/abc_subdocument_1.png", NOW - timedelta(days=1)),
        ("processed//extracted_data_abc.json", NOW),
    ]
    assert set(select_for_deletion(blobs, now=NOW)) == {b[0] for b in blobs}


def test_recent_product_blobs_are_kept():
    blobs = [
        ("uploads-bps/abc.pdf", NOW - timedelta(days=2)),
        ("processed-vetcostcheck//extracted_data_abc.json", NOW - timedelta(days=13)),
        ("processed-sanierer/abc_subdocument_1.png", NOW),
    ]
    assert select_for_deletion(blobs, now=NOW) == []


def test_old_product_blobs_go():
    blobs = [
        ("uploads-bps/old.pdf", NOW - timedelta(days=15)),
        ("processed-vetcostcheck/old_subdocument_1.md", NOW - timedelta(days=90)),
    ]
    assert set(select_for_deletion(blobs, now=NOW)) == {b[0] for b in blobs}


def test_root_level_strays_follow_the_cutoff():
    old = ("230075012T_Splitt.pdf", NOW - timedelta(days=180))
    new = ("something_just_uploaded.pdf", NOW - timedelta(hours=1))
    assert select_for_deletion([old, new], now=NOW) == [old[0]]


def test_unknown_prefix_follows_the_cutoff():
    blobs = [
        ("uploads-garagenhub/new.pdf", NOW - timedelta(days=3)),
        ("uploads-garagenhub/old.pdf", NOW - timedelta(days=20)),
    ]
    assert select_for_deletion(blobs, now=NOW) == ["uploads-garagenhub/old.pdf"]


def test_cutoff_boundary_is_strict():
    exactly = ("uploads-bps/a.pdf", NOW - timedelta(days=14))
    just_over = ("uploads-bps/b.pdf", NOW - timedelta(days=14, seconds=1))
    assert select_for_deletion([exactly, just_over], now=NOW) == [just_over[0]]


def test_cutoff_days_is_configurable():
    blobs = [("uploads-bps/a.pdf", NOW - timedelta(days=5))]
    assert select_for_deletion(blobs, now=NOW, cutoff_days=3) == ["uploads-bps/a.pdf"]
    assert select_for_deletion(blobs, now=NOW, cutoff_days=30) == []
