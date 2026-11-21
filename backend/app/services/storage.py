"""Private object/file storage adapter boundary with local filesystem implementation and S3 boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

import boto3

from app.core.config import get_settings
from app.core.security import is_safe_relative_storage_key


class StorageAdapter(Protocol):
    """Abstract private storage contract."""

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        ...

    def get_bytes(self, key: str) -> bytes:
        ...

    def delete_bytes(self, key: str) -> None:
        ...


class LocalPrivateStorage:
    """Private single-host filesystem storage outside public web roots."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        raw_dir = base_dir or get_settings().local_storage_path
        self.base_dir = Path(raw_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_key(self, key: str) -> Path:
        if not is_safe_relative_storage_key(key):
            raise ValueError(f"Unsafe storage key: {key}")
        target = (self.base_dir / key).resolve()
        if not str(target).startswith(str(self.base_dir)):
            raise ValueError(f"Storage path traversal blocked: {key}")
        return target

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        target = self._resolve_key(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        return key

    def get_bytes(self, key: str) -> bytes:
        target = self._resolve_key(key)
        if not target.is_file():
            raise FileNotFoundError(f"Stored file missing: {key}")
        return target.read_bytes()

    def delete_bytes(self, key: str) -> None:
        target = self._resolve_key(key)
        if target.is_file():
            target.unlink()


class S3PrivateStorage:
    """Private S3-compatible object storage adapter (never public ACL)."""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.s3_bucket:
            raise ValueError("S3_BUCKET must be configured when STORAGE_BACKEND=s3")
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            region_name=settings.s3_region or None,
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key_id or None,
            aws_secret_access_key=settings.s3_secret_access_key or None,
        )

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        if not is_safe_relative_storage_key(key):
            raise ValueError(f"Unsafe storage key: {key}")
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ACL="private",
        )
        return key

    def get_bytes(self, key: str) -> bytes:
        if not is_safe_relative_storage_key(key):
            raise ValueError(f"Unsafe storage key: {key}")
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def delete_bytes(self, key: str) -> None:
        if not is_safe_relative_storage_key(key):
            raise ValueError(f"Unsafe storage key: {key}")
        self.client.delete_object(Bucket=self.bucket, Key=key)


def get_storage_adapter() -> StorageAdapter:
    settings = get_settings()
    if settings.storage_backend == "s3":
        return S3PrivateStorage()
    return LocalPrivateStorage(settings.local_storage_path)


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
