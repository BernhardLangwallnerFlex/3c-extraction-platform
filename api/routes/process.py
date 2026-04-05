import os
import structlog
from fastapi import APIRouter, HTTPException
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue

from jobs.tasks import process_file
from api.models import ProcessRequest

logger = structlog.get_logger()
router = APIRouter()

QUEUE_NAME = os.getenv("RQ_QUEUE_NAME", "invoice-jobs")


def get_queue() -> Queue:
    redis_url = os.environ["REDIS_URL"]  # fail fast if missing
    redis_conn = Redis.from_url(
        redis_url,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    return Queue(QUEUE_NAME, connection=redis_conn)


@router.post("/process")
async def process_document(req: ProcessRequest):
    try:
        queue = get_queue()
        job = queue.enqueue(process_file, req.file_id, job_timeout=3600,result_ttl=3600,failure_ttl=3600)  # 1 hour timeout
    except RedisError as e:
        logger.error("redis_connection_failed", endpoint="process", error=str(e))
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

    return {
        "job_id": job.get_id(),
        "status": "queued",
        "queue": QUEUE_NAME,
    }