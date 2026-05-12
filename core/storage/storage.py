# storage.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Optional
import io
import os
import shutil
import tempfile
from dotenv import load_dotenv

load_dotenv()

# S3 is optional until you use it
try:
    import boto3
except Exception:  # pragma: no cover
    boto3 = None


try:
    from azure.storage.blob import BlobServiceClient, ContentSettings
    from azure.core.exceptions import ResourceNotFoundError
except Exception:  # pragma: no cover
    BlobServiceClient = None
    ContentSettings = None
    ResourceNotFoundError = None


StorageKey = str  # could be a plain local path, "s3://bucket/key.pdf", or "az://container/blob.pdf"


class StorageBackend(Protocol):
    def read_bytes(self, key: StorageKey) -> bytes: ...
    def write_bytes(self, key: StorageKey, data: bytes, content_type: Optional[str] = None) -> None: ...
    def write_text(self, key: StorageKey, text: str, encoding: str = "utf-8") -> None: ...
    def delete(self, key: StorageKey) -> None: ...
    def exists(self, key: StorageKey) -> bool: ...

    def materialize_to_local(self, key: StorageKey, suffix: str = "") -> Path:
        """
        Ensure key is available as a local file path and return that path.
        For LocalStorage it's the original path; for S3 it downloads to temp.
        """


def is_s3_uri(key: str) -> bool:
    return key.startswith("s3://")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    # s3://bucket/some/path/file.pdf -> ("bucket", "some/path/file.pdf")
    if not uri.startswith("s3://"):
        raise ValueError(f"Not an s3 uri: {uri}")
    without = uri[len("s3://") :]
    bucket, _, key = without.partition("/")
    if not bucket or not key:
        raise ValueError(f"Invalid s3 uri: {uri}")
    return bucket, key


def is_az_uri(key: str) -> bool:
    return key.startswith("az://")


def parse_az_uri(uri: str) -> tuple[str, str]:
    """
    az://container/some/path/file.pdf -> ("container", "some/path/file.pdf")
    """
    if not uri.startswith("az://"):
        raise ValueError(f"Not an az uri: {uri}")
    without = uri[len("az://") :]
    container, _, blob = without.partition("/")
    if not container or not blob:
        raise ValueError(f"Invalid az uri: {uri}")
    return container, blob


@dataclass
class LocalStorage(StorageBackend):
    base_dir: Optional[Path] = None

    def _resolve(self, key: StorageKey) -> Path:
        p = Path(key)
        if self.base_dir and not p.is_absolute():
            p = self.base_dir / p
        return p

    def read_bytes(self, key: StorageKey) -> bytes:
        return self._resolve(key).read_bytes()

    def write_bytes(self, key: StorageKey, data: bytes, content_type: Optional[str] = None) -> None:
        p = self._resolve(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def write_text(self, key: StorageKey, text: str, encoding: str = "utf-8") -> None:
        p = self._resolve(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding=encoding)

    def delete(self, key: StorageKey) -> None:
        p = self._resolve(key)
        if p.exists():
            p.unlink()

    def exists(self, key: StorageKey) -> bool:
        return self._resolve(key).exists()

    def materialize_to_local(self, key: StorageKey, suffix: str = "") -> Path:
        # already local
        return self._resolve(key)


@dataclass
class S3Storage(StorageBackend):
    """
    key is expected as s3://bucket/path/to/file.ext
    """
    region_name: Optional[str] = None

    def __post_init__(self):
        if boto3 is None:
            raise ImportError("boto3 not installed. `pip install boto3`")
        self.s3 = boto3.client("s3", region_name=self.region_name)
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="invoice_s3_"))

    def read_bytes(self, key: StorageKey) -> bytes:
        bucket, obj_key = parse_s3_uri(key)
        buf = io.BytesIO()
        self.s3.download_fileobj(bucket, obj_key, buf)
        return buf.getvalue()

    def write_bytes(self, key: StorageKey, data: bytes, content_type: Optional[str] = None) -> None:
        bucket, obj_key = parse_s3_uri(key)
        extra = {}
        if content_type:
            extra["ContentType"] = content_type
        self.s3.put_object(Bucket=bucket, Key=obj_key, Body=data, **extra)

    def write_text(self, key: StorageKey, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(key, text.encode(encoding), content_type="text/plain; charset=utf-8")

    def delete(self, key: StorageKey) -> None:
        bucket, obj_key = parse_s3_uri(key)
        self.s3.delete_object(Bucket=bucket, Key=obj_key)

    def exists(self, key: StorageKey) -> bool:
        bucket, obj_key = parse_s3_uri(key)
        try:
            self.s3.head_object(Bucket=bucket, Key=obj_key)
            return True
        except Exception:
            return False

    def materialize_to_local(self, key: StorageKey, suffix: str = "") -> Path:
        bucket, obj_key = parse_s3_uri(key)
        filename = Path(obj_key).name
        if suffix and not filename.endswith(suffix):
            # if caller wants a suffix, enforce it (helpful if key has no extension)
            filename = filename + suffix
        local_path = self._tmp_dir / filename
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with local_path.open("wb") as f:
            self.s3.download_fileobj(bucket, obj_key, f)
        return local_path

    def cleanup_tmp(self) -> None:
        if self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)


@dataclass
class AzureBlobStorage(StorageBackend):
    """
    key is expected as az://<container>/<blob_path>
    Auth uses account name + account key.
    """

    account_name: str
    account_key: str

    def __post_init__(self):
        if BlobServiceClient is None:
            raise ImportError("azure-storage-blob not installed. `pip install azure-storage-blob`")

        self._service = BlobServiceClient(
            account_url=f"https://{self.account_name}.blob.core.windows.net",
            credential=self.account_key,
        )
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="invoice_az_"))

    def _blob_client(self, key: StorageKey):
        container, blob = parse_az_uri(key)
        return self._service.get_blob_client(container=container, blob=blob), container, blob

    def read_bytes(self, key: StorageKey) -> bytes:
        blob_client, _, _ = self._blob_client(key)
        return blob_client.download_blob().readall()

    def write_bytes(self, key: StorageKey, data: bytes, content_type: Optional[str] = None) -> None:
        blob_client, _, _ = self._blob_client(key)
        kwargs = {"overwrite": True}
        if content_type and ContentSettings is not None:
            kwargs["content_settings"] = ContentSettings(content_type=content_type)
        blob_client.upload_blob(data, **kwargs)

    def write_text(self, key: StorageKey, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(key, text.encode(encoding), content_type="text/plain; charset=utf-8")

    def delete(self, key: StorageKey) -> None:
        blob_client, _, _ = self._blob_client(key)
        blob_client.delete_blob()

    def exists(self, key: StorageKey) -> bool:
        blob_client, _, _ = self._blob_client(key)
        try:
            blob_client.get_blob_properties()
            return True
        except Exception as e:
            # Prefer the specific Azure exception if available, otherwise be permissive like S3Storage.
            if ResourceNotFoundError is not None and isinstance(e, ResourceNotFoundError):
                return False
            return False

    def materialize_to_local(self, key: StorageKey, suffix: str = "") -> Path:
        _, _, blob = self._blob_client(key)
        filename = Path(blob).name
        if suffix and not filename.endswith(suffix):
            filename = filename + suffix
        local_path = self._tmp_dir / filename
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self.read_bytes(key))
        return local_path

    def cleanup_tmp(self) -> None:
        if self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)