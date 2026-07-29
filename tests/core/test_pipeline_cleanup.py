"""Cleanup of stored artifacts after a successful extraction.

Follows the construction trick in test_pipeline_postprocess.py: build a Pipeline
via object.__new__ so __init__ (OCR, PDF I/O, storage materialization) never runs.
"""
import pytest

from core.pipeline import Pipeline, SubdocumentArtifact


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
