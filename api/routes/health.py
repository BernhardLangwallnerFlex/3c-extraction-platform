import os
from fastapi import APIRouter, Response
from redis import Redis

router = APIRouter()


@router.get("/healthz")
def healthz():
    """Liveness probe — lightweight, always returns 200 if the process is alive."""
    return {"status": "ok"}


@router.get("/ready")
def ready(response: Response):
    """Readiness probe — checks critical dependencies."""
    checks = {}

    # Redis
    try:
        redis_url = os.environ.get("REDIS_URL")
        if redis_url:
            conn = Redis.from_url(redis_url, socket_connect_timeout=5, socket_timeout=5)
            conn.ping()
            checks["redis"] = "ok"
        else:
            checks["redis"] = "not_configured"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    # Storage backend
    storage_backend = os.getenv("STORAGE_BACKEND", "local").lower()
    if storage_backend == "azure":
        try:
            from storage.storage import AzureBlobStorage
            account_name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
            account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
            if account_name and account_key:
                storage = AzureBlobStorage(account_name=account_name, account_key=account_key)
                storage._service.get_account_information()
                checks["storage"] = "ok"
            else:
                checks["storage"] = "not_configured"
        except Exception as e:
            checks["storage"] = f"error: {e}"
    else:
        checks["storage"] = "ok"

    all_ok = all(v == "ok" for v in checks.values())
    if not all_ok:
        response.status_code = 503

    return {"status": "ok" if all_ok else "degraded", "checks": checks}
