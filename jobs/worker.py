import os
import sentry_sdk
import structlog
from redis import Redis
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

queue_name = os.getenv("RQ_QUEUE_NAME", "invoice-jobs")

if __name__ == "__main__":
    log = structlog.get_logger()
    log.info("worker_starting", queue=queue_name)
    redis_conn = Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=5, socket_timeout=5)
    queues = [Queue(queue_name, connection=redis_conn)]
    worker = Worker(queues, connection=redis_conn)
    worker.work()
