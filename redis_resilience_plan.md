# Redis Resilience Plan

## Context

On 2026-05-06 (17:17–17:36 UTC) Sentry recorded 19 occurrences of `HTTPException: Service temporarily unavailable` (issue `PYTHON-FASTAPI-9`) on `GET /job/{job_id}`. Underlying cause was `redis.exceptions.TimeoutError: Timeout connecting to server` while running `HGETALL rq:job:...` against Azure Cache for Redis (Basic C0, single node, no SLA). The 503 path is intentional — `RedisError` is caught and converted — but the system has no client-side retry, so a single TCP hiccup over the 5s `socket_connect_timeout` immediately surfaces as a 503 to the caller.

The system has otherwise been stable for ~2 weeks. These items are **deferred** — to be picked up if the issue recurs or before scaling load. No change required now.

## Current State

Three API call sites and one worker call site, each constructing a fresh `Redis` client with the same options (`socket_connect_timeout=5`, `socket_timeout=5`) and no retry policy:

- `api/routes/job.py:15-20` — `get_redis_conn()`, called per request inside `GET /job/{job_id}`
- `api/routes/process.py:17-24` — `get_queue()`, called per request inside `POST /process`
- `api/routes/health.py:23` — inline construction inside `GET /ready`
- `jobs/worker.py:34` — single connection at process start, used for the worker's lifetime

API requests therefore pay TCP+auth setup on every poll. RQ's `Retry(max=2)` in `process.py:31` only governs job execution, not the enqueue call or the `/job/{job_id}` lookup.

## Proposed Changes

### 1. Reuse a connection pool via FastAPI lifespan + Depends (high leverage)

Create one `Redis` client at app startup and inject it into routes via `Depends`. `redis-py`'s `Redis.from_url(...)` already wraps an internal `ConnectionPool`; the goal is to share **one** `Redis` instance instead of building a new one per request. Removes per-request connect overhead and means transient blips don't cost a fresh handshake.

Sketch:

```python
# api/main.py
from contextlib import asynccontextmanager
from redis import Redis

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = Redis.from_url(
        os.environ["REDIS_URL"],
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=30,
        socket_keepalive=True,
    )
    yield
    app.state.redis.close()

# api/deps.py
def get_redis(request: Request) -> Redis:
    return request.app.state.redis
```

Then `job.py` / `process.py` / `health.py` accept `redis_conn: Redis = Depends(get_redis)` and drop their local factories.

### 2. Add redis-py–level retry on transient errors (high leverage)

Distinct from RQ's `Retry`. Configure on the client:

```python
from redis.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError as RedisTimeoutError

Redis.from_url(
    url,
    retry=Retry(ExponentialBackoff(cap=2, base=0.1), retries=3),
    retry_on_error=[ConnectionError, RedisTimeoutError],
    retry_on_timeout=True,
    ...
)
```

This alone would have absorbed the 2026-05-06 incident transparently.

### 3. Long-lived worker connection: keepalive + health check (medium)

Worker's connection lives for the entire process lifetime. Cloud Redis idle connections can be silently reaped by the LB; the worker only notices on the next operation. Add to the worker's `Redis.from_url`:

```python
socket_keepalive=True,
health_check_interval=30,
```

Same options are useful in the API pool (item 1), but matter most here.

### 4. Collapse the four call sites into one factory (low; cleanup)

Once items 1–3 land, only the worker still constructs its own client. Move the kwargs (timeouts, retry, keepalive, health check) into a single helper, e.g. `utils.redis.build_redis_client(url)`, used by both `api/main.py` lifespan and `jobs/worker.py`. Keeps timeout/retry policy in one place.

### 5. Tier upgrade: Basic C0 → Standard C0 (situational)

Only if 503s recur. Basic C0 is single-node, no replica, no SLA. Standard C0 (≈2× the cost of Basic C0, ~€30/mo) gives a replica + 99.9% SLA. RQ metadata is tiny so capacity isn't the driver — availability is. Decision criterion: if items 1–3 are in place and we still see Redis-driven 503s in Sentry within a 30-day window, upgrade. Otherwise stay on Basic C0.

## Sequencing

1, 2, 3 ship together as one PR — they share files and are the actual fix. 4 follows as a small cleanup. 5 stays in the drawer until data justifies it.

## Out of Scope

- Switching from RQ to a different queue backend.
- Migrating off Azure Cache for Redis.
- Adding a circuit breaker in front of Redis (overkill for current scale).
