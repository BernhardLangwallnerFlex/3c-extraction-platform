import os
import time
import sentry_sdk
import structlog
from redis import Redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry
import redis.exceptions
from rq import Worker, Queue

# --- Sentry ---
sentry_dsn = os.getenv("SENTRY_DSN")
if sentry_dsn:
    sentry_sdk.init(
        dsn=sentry_dsn,
        traces_sample_rate=1.0,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
    )

# --- Structured logging ---
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()

queue_name = os.getenv("RQ_QUEUE_NAME", "invoice-jobs")

# A Basic-tier cache has no replica, so a patch reboot is a hard outage rather
# than a failover. The 2026-08-24 one lasted ~188s; this budget is 331s
# (1+2+4+8+16 then 30s a go), which covers a reboot with room to spare.
CONNECT_RETRIES = 15
BACKOFF_BASE_S = 1
BACKOFF_CAP_S = 30

# Outer attempts, for an outage that outlasts even the budget above.
STARTUP_ATTEMPTS = 4
STARTUP_WAIT_S = 30

RETRYABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def build_redis_connection(url: str | None = None, **kwargs) -> Redis:
    """A Redis client that rides out a short outage instead of dying.

    redis-py defaults to `Retry(NoBackoff(), retries=0)`, so the first timeout
    reaches the caller. That matters here because RQ retries
    `redis.exceptions.ConnectionError` with backoff but treats
    `redis.exceptions.TimeoutError` as fatal — see rq/worker.py, "Redis
    connection timeout, quitting...". The two are siblings, not parent/child,
    so RQ's handler never sees a timeout.

    That is exactly how the 2026-08-24 outage played out: while the dying node
    reset connections we got ConnectionError and RQ retried, and the moment it
    started black-holing them we got TimeoutError and every worker exited 1 into
    a Container Apps restart loop. Retrying down here keeps the timeout away
    from RQ entirely.

    `socket_timeout` stays at 5s only until RQ raises it to `dequeue_timeout +
    10` (415s) for the blocking pop, so it does not shorten a waiting worker.
    """
    return Redis.from_url(
        url or os.environ["REDIS_URL"],
        socket_connect_timeout=5,
        socket_timeout=5,
        retry=Retry(
            ExponentialBackoff(base=BACKOFF_BASE_S, cap=BACKOFF_CAP_S),
            retries=CONNECT_RETRIES,
        ),
        retry_on_error=list(RETRYABLE),
        # After an outage the pool still holds sockets to a node that is gone;
        # health checks retire them instead of failing a job on first use.
        health_check_interval=30,
        **kwargs,
    )


def connect_with_retry(do, attempts: int = STARTUP_ATTEMPTS, _sleep=time.sleep):
    """Run `do`, retrying Redis connection failures, then re-raise.

    The connection-level policy above covers a reboot. This covers a cold start
    during one that runs longer, so the container waits rather than exiting into
    a crash loop. If Redis is genuinely gone the error still surfaces — a worker
    that can never reach its queue should not look healthy.
    """
    for attempt in range(1, attempts + 1):
        try:
            return do()
        except RETRYABLE as exc:
            if attempt == attempts:
                log.error("redis_unreachable", attempts=attempts, error=str(exc))
                raise
            log.warning(
                "redis_unreachable_retrying",
                attempt=attempt,
                wait_s=STARTUP_WAIT_S,
                error=str(exc),
            )
            _sleep(STARTUP_WAIT_S)


if __name__ == "__main__":
    log.info("worker_starting", queue=queue_name)
    redis_conn = build_redis_connection()
    queues = [Queue(queue_name, connection=redis_conn)]
    worker = connect_with_retry(lambda: Worker(queues, connection=redis_conn))
    worker.work()
