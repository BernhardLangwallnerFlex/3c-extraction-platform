# storage/file_storage.py
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

from core.storage.storage import LocalStorage, S3Storage, AzureBlobStorage  # adjust to your actual module


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _build_storage():
    backend = os.getenv("STORAGE_BACKEND", "local").lower()
    if backend == "s3":
        region = os.getenv("AWS_DEFAULT_REGION", "eu-central-1")
        return S3Storage(region_name=region)
    if backend == "azure":
        return AzureBlobStorage(
            account_name=_require_env("AZURE_STORAGE_ACCOUNT_NAME"),
            account_key=_require_env("AZURE_STORAGE_ACCOUNT_KEY"),
        )
    return LocalStorage(base_dir=Path(os.getenv("LOCAL_STORAGE_BASE_DIR", Path.cwd())))


def _uploads_prefix() -> str:
    backend = os.getenv("STORAGE_BACKEND", "local").lower()

    if backend == "azure":
        prefix = os.getenv("AZURE_INPUT_PREFIX", "uploads").rstrip("/")
    elif backend == "s3":
        prefix = os.getenv("S3_INPUT_PREFIX", "uploads").rstrip("/")
    else:
        prefix = os.getenv("LOCAL_STORAGE_BASE_DIR", str(Path.cwd())).rstrip("/")

    if backend == "azure" and not prefix.startswith("az://"):
        raise RuntimeError(
            f"STORAGE_BACKEND is 'azure' but UPLOADS_PREFIX is '{prefix}'. "
            "Set UPLOADS_PREFIX like 'az://<container>/uploads'."
        )
    if backend == "s3" and not prefix.startswith("s3://"):
        raise RuntimeError(
            f"STORAGE_BACKEND is 's3' but UPLOADS_PREFIX is '{prefix}'. "
            "Set UPLOADS_PREFIX like 's3://<bucket>/uploads'."
        )

    return prefix


def get_file_key(file_id: str) -> str:
    return f"{_uploads_prefix()}/{file_id}"


def save_upload(file_bytes: bytes, original_filename: Optional[str] = None, content_type: Optional[str] = None) -> str:
    storage = _build_storage()

    # determine extension (optional but helpful)
    ext = ""
    if original_filename:
        ext = Path(original_filename).suffix.lower()
        # basic allowlist to avoid weird stuff
        if ext and ext not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            ext = ""  # or raise if you want strict

    file_id = f"{uuid.uuid4().hex}{ext}"
    key = get_file_key(file_id)

    storage.write_bytes(key, file_bytes, content_type=content_type)
    return file_id