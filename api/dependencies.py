import structlog
from fastapi import Header, HTTPException, status
from config import API_KEYS

log = structlog.get_logger()

async def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key not in API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    structlog.contextvars.bind_contextvars(api_key_suffix=x_api_key[-4:])
