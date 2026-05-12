import os
import structlog
from fastapi import APIRouter, HTTPException
from redis import Redis
from redis.exceptions import RedisError
from rq.job import Job
from rq.exceptions import NoSuchJobError

from core.api.models import JobStatusResponse

logger = structlog.get_logger()
router = APIRouter()


def get_redis_conn() -> Redis:
    return Redis.from_url(
        os.environ["REDIS_URL"],
        socket_connect_timeout=5,
        socket_timeout=5,
    )


@router.get("/job/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str):
    try:
        redis_conn = get_redis_conn()
        job = Job.fetch(job_id, connection=redis_conn)
    except NoSuchJobError:
        return {"job_id": job_id, "status": "not_found", "result": None}
    except RedisError as e:
        logger.error("redis_connection_failed", job_id=job_id, error=str(e))
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

    status = job.get_status()  # 'queued', 'started', 'finished', 'failed', etc.

    if status == "finished":
        return {"job_id": job_id, "status": "finished", "result": job.result}

    if status == "failed":
        # keep it short; full traceback is in Redis and logs
        err = None
        if job.exc_info:
            # first line usually contains the exception type/message
            err = job.exc_info.splitlines()[-1]
        return {"job_id": job_id, "status": "failed", "error": err}

    # queued / started / deferred / scheduled
    return {"job_id": job_id, "status": status, "result": None}