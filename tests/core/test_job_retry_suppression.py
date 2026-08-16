"""A permanent failure must not be re-run by RQ.

Each RQ retry re-runs the entire pipeline including both OCR engines. For an
error that can never succeed — a rotated key, a rejected request — that is
minutes of compute and three OCR passes spent to reach the same conclusion.
"""
import types

import pytest

import core.jobs.tasks as tasks


class FakeAPIError(Exception):
    def __init__(self, status_code):
        super().__init__(f"Error code: {status_code}")
        self.status_code = status_code


class _FakeJob:
    def __init__(self, retries_left=2):
        self.retries_left = retries_left
        self.saved = False

    def save(self):
        self.saved = True


def _run_with(monkeypatch, exc, job):
    """Drive process_file to its failure path with everything else stubbed out.

    `load_product_config()` runs before `get_file_key` and raises RuntimeError
    when PRODUCT_NAME is unset, so it is stubbed too — otherwise the test would
    be asserting on the wrong exception entirely.
    """
    monkeypatch.setattr(tasks, "load_product_config",
                        lambda *a, **kw: types.SimpleNamespace(name="test"))
    monkeypatch.setattr(tasks, "get_file_key", lambda file_id: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(tasks, "get_current_job", lambda: job)
    with pytest.raises(type(exc)):
        tasks.process_file("some-file-id.pdf")


def test_permanent_error_suppresses_the_remaining_retries(monkeypatch):
    job = _FakeJob(retries_left=2)
    _run_with(monkeypatch, FakeAPIError(401), job)

    assert job.retries_left == 0
    assert job.saved is True


def test_transient_error_keeps_its_retries(monkeypatch):
    job = _FakeJob(retries_left=2)
    _run_with(monkeypatch, FakeAPIError(503), job)

    assert job.retries_left == 2
    assert job.saved is False


def test_no_rq_job_context_does_not_break_the_failure_path(monkeypatch):
    # process_file is also called directly by scripts/extract_local.py and the
    # sweep, where there is no RQ job at all.
    _run_with(monkeypatch, FakeAPIError(401), None)


def test_a_failure_while_suppressing_does_not_mask_the_original_error(monkeypatch):
    class _ExplodingJob(_FakeJob):
        def save(self):
            raise RuntimeError("redis is down")

    # The original FakeAPIError must still be what propagates.
    _run_with(monkeypatch, FakeAPIError(401), _ExplodingJob())


class _PathologicalError(Exception):
    """An exception whose `status_code` access itself blows up.

    `is_retryable` probes `status_code` via `getattr(exc, "status_code", None)`.
    That default only swallows `AttributeError` — if `__getattr__` raises
    something else for that one name, it propagates straight out of
    `is_retryable`. The suppression helper must still let the *original*
    exception (this one) be what `process_file` re-raises, not whatever
    `is_retryable` raised internally.

    Only `status_code` is special-cased; every other missing attribute still
    raises a plain `AttributeError` so that unrelated introspection (pytest's
    own exception handling, `__notes__`, etc.) is unaffected.
    """

    def __getattr__(self, name):
        if name == "status_code":
            raise RuntimeError(f"boom while probing {name!r}")
        raise AttributeError(name)


def test_is_retryable_blowing_up_does_not_mask_the_original_error(monkeypatch):
    job = _FakeJob(retries_left=2)

    # The original _PathologicalError must still be what propagates, not the
    # RuntimeError raised internally by is_retryable's attribute probe.
    _run_with(monkeypatch, _PathologicalError("weird failure"), job)

    # Nothing should have been mutated: the helper never got far enough to
    # decide the error was permanent.
    assert job.retries_left == 2
    assert job.saved is False
