"""Cleanup of stored artifacts after a successful extraction.

Follows the construction trick in test_pipeline_postprocess.py: build a Pipeline
via object.__new__ so __init__ (OCR, PDF I/O, storage materialization) never runs.
"""
import pytest

from core.pipeline import Pipeline, SubdocumentArtifact
from core.storage.storage import LocalStorage


class _RecordingStorage:
    """Records deletes; raises for any key listed in fail_keys."""

    def __init__(self, fail_keys=()):
        self.deleted = []
        self.fail_keys = set(fail_keys)

    def delete(self, key):
        if key in self.fail_keys:
            raise RuntimeError(f"delete refused: {key}")
        self.deleted.append(key)


def _subdoc(n):
    return SubdocumentArtifact(
        document_number=n,
        page_numbers=[n],
        markdown="dummy",
        md_key=f"az://invoices/processed-bps/abc_subdocument_{n}.md",
        pdf_key=f"az://invoices/processed-bps/abc_subdocument_{n}.pdf",
        image_key=f"az://invoices/processed-bps/abc_subdocument_{n}.png",
    )


def _make_pipeline(storage, subdoc_count=2):
    pipe = object.__new__(Pipeline)
    pipe.storage = storage
    pipe.file_key = "az://invoices/uploads-bps/abc.pdf"
    pipe.subdocuments = [_subdoc(n) for n in range(1, subdoc_count + 1)]
    pipe.output_prefix = "az://invoices/processed-bps/"
    pipe.stem = "abc"
    return pipe


@pytest.fixture(autouse=True)
def _cleanup_enabled(monkeypatch):
    # load_dotenv() at import time may have set this from a local .env; pin it.
    monkeypatch.setenv("CLEANUP_ARTIFACTS", "true")


def test_deletes_upload_and_every_subdoc_artifact():
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage, subdoc_count=2)

    deleted = pipe.cleanup_storage_artifacts()

    assert deleted == 7  # 1 upload + 2 subdocs x 3 artifacts
    assert set(storage.deleted) == {
        "az://invoices/uploads-bps/abc.pdf",
        "az://invoices/processed-bps/abc_subdocument_1.md",
        "az://invoices/processed-bps/abc_subdocument_1.pdf",
        "az://invoices/processed-bps/abc_subdocument_1.png",
        "az://invoices/processed-bps/abc_subdocument_2.md",
        "az://invoices/processed-bps/abc_subdocument_2.pdf",
        "az://invoices/processed-bps/abc_subdocument_2.png",
    }


def test_never_deletes_the_result_json():
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage)

    pipe.cleanup_storage_artifacts()

    # The key exactly as extract_data_from_subdocuments writes it (core/pipeline.py:358).
    result_key = f"{pipe.output_prefix}/extracted_data_{pipe.stem}.json"
    assert result_key not in storage.deleted
    assert not [k for k in storage.deleted if "extracted_data" in k]


def test_one_failing_delete_does_not_stop_the_others():
    doomed = "az://invoices/processed-bps/abc_subdocument_1.pdf"
    storage = _RecordingStorage(fail_keys=[doomed])
    pipe = _make_pipeline(storage, subdoc_count=2)

    deleted = pipe.cleanup_storage_artifacts()

    assert deleted == 6
    assert doomed not in storage.deleted
    assert "az://invoices/uploads-bps/abc.pdf" in storage.deleted
    assert "az://invoices/processed-bps/abc_subdocument_2.png" in storage.deleted


def test_disabled_by_env_flag(monkeypatch):
    monkeypatch.setenv("CLEANUP_ARTIFACTS", "false")
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage)

    assert pipe.cleanup_storage_artifacts() == 0
    assert storage.deleted == []


def test_total_failure_reports_to_sentry(monkeypatch):
    all_keys = [
        "az://invoices/uploads-bps/abc.pdf",
        "az://invoices/processed-bps/abc_subdocument_1.md",
        "az://invoices/processed-bps/abc_subdocument_1.pdf",
        "az://invoices/processed-bps/abc_subdocument_1.png",
    ]
    storage = _RecordingStorage(fail_keys=all_keys)
    pipe = _make_pipeline(storage, subdoc_count=1)

    captured = []
    monkeypatch.setattr(
        "core.pipeline.sentry_sdk.capture_message",
        lambda msg, level=None: captured.append((msg, level)),
    )

    assert pipe.cleanup_storage_artifacts() == 0  # does not raise
    assert len(captured) == 1
    assert captured[0][1] == "warning"


def test_partial_failure_does_not_report_to_sentry(monkeypatch):
    storage = _RecordingStorage(fail_keys=["az://invoices/uploads-bps/abc.pdf"])
    pipe = _make_pipeline(storage, subdoc_count=1)

    captured = []
    monkeypatch.setattr(
        "core.pipeline.sentry_sdk.capture_message",
        lambda msg, level=None: captured.append((msg, level)),
    )

    assert pipe.cleanup_storage_artifacts() == 3
    assert captured == []


def test_upload_blob_is_deleted_last():
    # If the worker dies mid-cleanup, a retry re-reads self.file_key. Intermediates
    # are regenerated from it on the retry, so they must go first; the upload must
    # be the very last delete so a partial cleanup never loses the only copy.
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage, subdoc_count=2)

    pipe.cleanup_storage_artifacts()

    assert storage.deleted[-1] == pipe.file_key
    assert storage.deleted.index(pipe.file_key) == len(storage.deleted) - 1


def test_upload_still_deleted_last_when_a_subdoc_delete_fails():
    doomed = "az://invoices/processed-bps/abc_subdocument_1.md"
    storage = _RecordingStorage(fail_keys=[doomed])
    pipe = _make_pipeline(storage, subdoc_count=2)

    pipe.cleanup_storage_artifacts()

    assert pipe.file_key in storage.deleted
    assert storage.deleted[-1] == pipe.file_key


def test_zero_subdocuments_keeps_everything_and_warns(monkeypatch):
    # A vacuous extraction (analyze_document found no invoice pages) is exactly the
    # case a human most needs to reproduce. The upload must survive it.
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage, subdoc_count=0)

    warnings = []
    monkeypatch.setattr(
        "core.pipeline._telemetry.warning",
        lambda event, **kw: warnings.append((event, kw)),
    )

    deleted = pipe.cleanup_storage_artifacts()

    assert deleted == 0
    assert storage.deleted == []
    assert len(warnings) == 1
    event, kw = warnings[0]
    assert event == "artifact_cleanup_skipped"
    assert "no subdocuments" in kw["reason"]
    assert kw["file_key"] == pipe.file_key


def test_unrecognized_flag_spelling_fails_toward_enabled(monkeypatch):
    # CLEANUP_ARTIFACTS is a known-false set, not an allowlist: a typo, stray
    # quote, or inline .env comment must fail toward the documented default
    # (cleanup enabled), not silently disable cleanup.
    monkeypatch.setenv("CLEANUP_ARTIFACTS", "on")
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage, subdoc_count=1)

    deleted = pipe.cleanup_storage_artifacts()

    assert deleted == 4  # 1 upload + 1 subdoc x 3 artifacts — cleanup ran
    assert storage.deleted != []


def test_empty_string_flag_disables_cleanup(monkeypatch):
    monkeypatch.setenv("CLEANUP_ARTIFACTS", "")
    storage = _RecordingStorage()
    pipe = _make_pipeline(storage)

    assert pipe.cleanup_storage_artifacts() == 0
    assert storage.deleted == []


def test_cleanup_with_real_local_storage_tolerates_missing_file(tmp_path):
    # The guarded-delete design exists because backends disagree on deleting a
    # missing key: AzureBlobStorage raises ResourceNotFoundError, LocalStorage
    # silently no-ops. Exercise the real LocalStorage to prove that difference is
    # actually handled, not just assumed from the fake.
    storage = LocalStorage(base_dir=tmp_path)

    upload_key = "uploads-bps/abc.pdf"
    md_key = "processed-bps/abc_subdocument_1.md"
    pdf_key = "processed-bps/abc_subdocument_1.pdf"
    image_key = "processed-bps/abc_subdocument_1.png"

    storage.write_bytes(upload_key, b"%PDF-upload")
    storage.write_text(md_key, "# subdoc markdown")
    storage.write_bytes(pdf_key, b"%PDF-subdoc")
    storage.write_bytes(image_key, b"fake-png-bytes")

    # Simulate a stray already-missing artifact (e.g. a previous partial cleanup).
    (tmp_path / pdf_key).unlink()

    pipe = object.__new__(Pipeline)
    pipe.storage = storage
    pipe.file_key = upload_key
    pipe.subdocuments = [
        SubdocumentArtifact(
            document_number=1,
            page_numbers=[1],
            markdown="dummy",
            md_key=md_key,
            pdf_key=pdf_key,
            image_key=image_key,
        )
    ]
    pipe.output_prefix = "processed-bps"
    pipe.stem = "abc"

    deleted = pipe.cleanup_storage_artifacts()

    # LocalStorage.delete() never raises on a missing path, so the already-gone
    # pdf_key is counted the same as the others — the whole point being that
    # cleanup does not crash or short-circuit when a key is already absent.
    assert deleted == 4
    assert not (tmp_path / upload_key).exists()
    assert not (tmp_path / md_key).exists()
    assert not (tmp_path / pdf_key).exists()
    assert not (tmp_path / image_key).exists()


def test_zero_subdocuments_with_real_local_storage_keeps_upload(tmp_path):
    storage = LocalStorage(base_dir=tmp_path)
    upload_key = "uploads-bps/abc.pdf"
    storage.write_bytes(upload_key, b"%PDF-upload")

    pipe = object.__new__(Pipeline)
    pipe.storage = storage
    pipe.file_key = upload_key
    pipe.subdocuments = []
    pipe.output_prefix = "processed-bps"
    pipe.stem = "abc"

    deleted = pipe.cleanup_storage_artifacts()

    assert deleted == 0
    assert (tmp_path / upload_key).exists()
