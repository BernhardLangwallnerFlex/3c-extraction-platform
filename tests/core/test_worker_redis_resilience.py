"""A brief Redis outage must not crash-loop the worker.

On 2026-08-24 03:38-03:42 UTC the shared Basic C0 cache restarted for ~2.5
minutes. All six worker apps died with `TimeoutError: Timeout connecting to
server` and were restarted by Container Apps in a loop — ca-worker-bps-test
went round 24 times, producing 36 Sentry events out of one infra blip.

Two facts combine to cause that:

1. redis-py's default connection retry is `Retry(NoBackoff(), retries=0)`, so
   the very first timeout propagates to the caller.
2. RQ retries `redis.exceptions.ConnectionError` with exponential backoff, but
   treats `redis.exceptions.TimeoutError` as fatal ("Redis connection timeout,
   quitting..."). The two are siblings, not parent/child — a TimeoutError is
   not caught by RQ's ConnectionError handler.

So the outage was survivable right up to the moment the dying node stopped
resetting connections (ECONNRESET -> ConnectionError, handled) and started
black-holing them (socket timeout -> TimeoutError, fatal).

These tests pin the connection-layer retry that keeps TimeoutError away from
RQ in the first place.
"""
import redis.exceptions
from redis.backoff import ExponentialBackoff

from core.jobs.worker import build_redis_connection, connect_with_retry


def _connection_for(**kwargs):
    """The Connection object the pool would hand to RQ."""
    client = build_redis_connection("redis://localhost:6379/0", **kwargs)
    return client.connection_pool.make_connection()


def test_timeouts_are_retried_not_raised_on_the_first_failure():
    """redis-py's default is retries=0, which is what let one blip through."""
    retry = _connection_for().retry

    assert retry._retries > 0, "retries=0 propagates the first timeout to RQ, which then quits"
    assert redis.exceptions.TimeoutError in retry._supported_errors
    assert redis.exceptions.ConnectionError in retry._supported_errors


def test_retries_back_off_instead_of_hammering_a_restarting_node():
    retry = _connection_for().retry

    assert isinstance(retry._backoff, ExponentialBackoff), (
        "NoBackoff retries all fire within milliseconds, so they all fail together"
    )


def test_the_retry_budget_outlasts_a_basic_tier_reboot():
    """The 2026-08-24 outage lasted ~188s; a patch reboot is the failure to survive."""
    retry = _connection_for().retry
    backoff = retry._backoff

    total_wait = sum(backoff.compute(n) for n in range(retry._retries))

    assert total_wait >= 240, f"budget is only {total_wait:.0f}s, too short for a node restart"


def test_a_transient_timeout_is_absorbed_and_the_call_succeeds():
    """Behavioural check: the policy actually retries a failing call."""
    retry = _connection_for().retry
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise redis.exceptions.TimeoutError("Timeout connecting to server")
        return "ok"

    # Zero out the sleeps so the test does not actually wait the backoff.
    retry.update_supported_errors([redis.exceptions.TimeoutError])
    retry._backoff = ExponentialBackoff(base=0, cap=0)

    assert retry.call_with_retry(flaky, lambda _: None) == "ok"
    assert len(attempts) == 3


def test_health_checks_are_enabled_so_stale_connections_are_recycled():
    """After an outage the pool holds sockets to a node that no longer exists."""
    assert _connection_for().health_check_interval > 0


def test_startup_waits_out_an_outage_instead_of_exiting():
    """A cold start during the outage must not exit(1) into a crash loop."""
    calls = []

    def failing_twice():
        calls.append(1)
        if len(calls) < 3:
            raise redis.exceptions.TimeoutError("Timeout connecting to server")
        return "worker"

    assert connect_with_retry(failing_twice, _sleep=lambda _: None) == "worker"
    assert len(calls) == 3


def test_startup_gives_up_eventually_rather_than_hanging_forever():
    """A permanently dead Redis should surface, not be retried in silence."""
    def always_failing():
        raise redis.exceptions.TimeoutError("Timeout connecting to server")

    try:
        connect_with_retry(always_failing, _sleep=lambda _: None)
    except redis.exceptions.TimeoutError:
        pass
    else:
        raise AssertionError("expected the error to surface after the attempts are spent")


def test_startup_also_waits_out_a_refused_connection():
    """A refused connect surfaces as ConnectionError from outside the retry wrapper.

    redis-py converts the underlying OSError into `redis.exceptions.ConnectionError`
    *after* `Retry.call_with_retry` has already given up, so the connection-level
    policy never sees it. During `work()` RQ's own ConnectionError handler covers
    that case; at startup this loop has to.
    """
    calls = []

    def failing_twice():
        calls.append(1)
        if len(calls) < 3:
            raise redis.exceptions.ConnectionError("Error 61 connecting to host. Connection refused.")
        return "worker"

    assert connect_with_retry(failing_twice, _sleep=lambda _: None) == "worker"
    assert len(calls) == 3
