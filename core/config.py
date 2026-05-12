import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(BASE_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

API_KEYS = set(
    k.strip()
    for k in os.getenv("INVOICE_API_KEYS", os.getenv("INVOICE_API_KEY", "")).split(",")
    if k.strip()
)