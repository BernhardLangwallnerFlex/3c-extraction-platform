import os
import sentry_sdk
import structlog

from fastapi import FastAPI, Depends
from fastapi.openapi.utils import get_openapi
from core.api.routes import upload, process, job, health
from core.api.dependencies import verify_api_key

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


def custom_openapi():
    """Inject the active product's extraction schema into the OpenAPI docs.

    At runtime `JobStatusResponse.result` is a pass-through dict (so no product
    field is ever stripped — see core/api/models.py). For developer-facing docs,
    though, we want the real per-product shape. Each product ships a JSON Schema
    (products/<name>/extract_schema.json, surfaced via ProductConfig); we splice
    it into the `result` field's schema, wrapped in the pipeline's
    {number_of_subdocuments, subdocuments[]} envelope. Falls back to the generic
    dict schema if no product is configured (e.g. local dev without PRODUCT_NAME).
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)

    try:
        from core.product import load_product_config

        config = load_product_config()
        # Drop the JSON Schema dialect key; OpenAPI carries its own.
        item_schema = {k: v for k, v in config.extract_output_schema.items() if k != "$schema"}
        product_result = {
            "type": "object",
            "title": f"{config.name} extraction result",
            "properties": {
                "number_of_subdocuments": {"type": "integer"},
                "subdocuments": {"type": "array", "items": item_schema},
            },
        }
        schema["components"]["schemas"]["JobStatusResponse"]["properties"]["result"] = {
            "anyOf": [product_result, {"type": "null"}],
            "default": None,
            "title": "Result",
            "description": (
                f"Per-product extraction output for '{config.name}'. "
                f"Authoritative schema: products/{config.name}/extract_schema.json"
            ),
        }
        schema["info"]["title"] = f"Extraction API — {config.name}"
    except Exception as exc:  # noqa: BLE001 — docs are best-effort; never break /openapi.json
        structlog.get_logger().warning("openapi_product_schema_skipped", error=str(exc))

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
