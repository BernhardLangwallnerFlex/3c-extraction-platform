import os
import sentry_sdk
import structlog

from fastapi import FastAPI, Depends
from api.routes import upload, process, job, health
from api.dependencies import verify_api_key

# --- Sentry ---
sentry_dsn = os.getenv("SENTRY_DSN")
if sentry_dsn:
    sentry_sdk.init(
        dsn=sentry_dsn,
        traces_sample_rate=1.0,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        release=os.getenv("SENTRY_RELEASE", "unknown"),
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

app = FastAPI(title="Invoice Extraction API")

# Apply to all routers
app.include_router(upload.router, dependencies=[Depends(verify_api_key)])
app.include_router(process.router, dependencies=[Depends(verify_api_key)])
app.include_router(job.router, dependencies=[Depends(verify_api_key)])
app.include_router(health.router)
