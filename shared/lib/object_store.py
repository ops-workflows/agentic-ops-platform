"""Provider-neutral object storage for agent memory backups and workflow bundles.

Selects between an S3-compatible backend (MinIO, AWS S3, or any other
S3-compatible endpoint) and Google Cloud Storage based on
``settings.object_store_provider``, so memory backups and workflow bundles
use identical code across compose, Kubernetes, and GCP deployments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol

from minio import Minio
from minio.error import S3Error

from shared.lib.config import settings

logger = logging.getLogger(__name__)

BUCKET_AGENT_MEMORY = "agent-memory"


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int | None = None
    last_modified: object | None = None


class ObjectStore(Protocol):
    def ensure_bucket(self, bucket: str) -> None: ...

    def upload_file(self, bucket: str, key: str, file_path: str) -> str: ...

    def upload_bytes(
        self, bucket: str, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str: ...

    def download_file(self, bucket: str, key: str, file_path: str) -> bool: ...

    def download_bytes(self, bucket: str, key: str) -> bytes | None: ...

    def list_objects(self, bucket: str, prefix: str = "") -> list[ObjectInfo]: ...

    def delete_object(self, bucket: str, key: str) -> bool: ...

    def presigned_get_url(self, bucket: str, key: str, *, expires_sec: int = 3600) -> str: ...


class S3ObjectStore:
    """S3-compatible backend — works for MinIO and AWS S3 alike."""

    def __init__(
        self, *, endpoint: str, access_key: str, secret_key: str, secure: bool = False, client: Minio | None = None
    ) -> None:
        self._client = client or Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)

    def ensure_bucket(self, bucket: str) -> None:
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)

    def upload_file(self, bucket: str, key: str, file_path: str) -> str:
        self.ensure_bucket(bucket)
        self._client.fput_object(bucket, key, file_path)
        logger.info("Uploaded file %s to %s/%s", file_path, bucket, key)
        return key

    def upload_bytes(
        self, bucket: str, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        self.ensure_bucket(bucket)
        self._client.put_object(bucket, key, BytesIO(data), length=len(data), content_type=content_type)
        logger.info("Uploaded %d bytes to %s/%s", len(data), bucket, key)
        return key

    def download_file(self, bucket: str, key: str, file_path: str) -> bool:
        try:
            self._client.fget_object(bucket, key, file_path)
            return True
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return False
            raise

    def download_bytes(self, bucket: str, key: str) -> bytes | None:
        try:
            response = self._client.get_object(bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchBucket"}:
                return None
            raise

    def list_objects(self, bucket: str, prefix: str = "") -> list[ObjectInfo]:
        try:
            return [
                ObjectInfo(key=item.object_name, size=item.size, last_modified=item.last_modified)
                for item in self._client.list_objects(bucket, prefix=prefix, recursive=True)
                if item.object_name
            ]
        except S3Error as exc:
            if exc.code == "NoSuchBucket":
                return []
            raise

    def delete_object(self, bucket: str, key: str) -> bool:
        try:
            self._client.remove_object(bucket, key)
            logger.info("Deleted object %s/%s", bucket, key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchBucket"}:
                return False
            raise

    def presigned_get_url(self, bucket: str, key: str, *, expires_sec: int = 3600) -> str:
        from datetime import timedelta

        return self._client.presigned_get_object(bucket, key, expires=timedelta(seconds=expires_sec))


class GCSObjectStore:
    """Google Cloud Storage backend."""

    def __init__(self, *, project: str = "", client: Any = None) -> None:
        if client is not None:
            self._client = client
        else:
            from google.cloud import storage as gcs_storage

            self._client = gcs_storage.Client(project=project or None)

    def ensure_bucket(self, bucket: str) -> None:
        if not self._client.lookup_bucket(bucket):
            self._client.create_bucket(bucket)

    def upload_file(self, bucket: str, key: str, file_path: str) -> str:
        self.ensure_bucket(bucket)
        blob = self._client.bucket(bucket).blob(key)
        blob.upload_from_filename(file_path)
        logger.info("Uploaded file %s to %s/%s", file_path, bucket, key)
        return key

    def upload_bytes(
        self, bucket: str, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        self.ensure_bucket(bucket)
        blob = self._client.bucket(bucket).blob(key)
        blob.upload_from_string(data, content_type=content_type)
        logger.info("Uploaded %d bytes to %s/%s", len(data), bucket, key)
        return key

    def download_file(self, bucket: str, key: str, file_path: str) -> bool:
        blob = self._client.bucket(bucket).blob(key)
        if not blob.exists():
            return False
        blob.download_to_filename(file_path)
        return True

    def download_bytes(self, bucket: str, key: str) -> bytes | None:
        blob = self._client.bucket(bucket).blob(key)
        if not blob.exists():
            return None
        return blob.download_as_bytes()

    def list_objects(self, bucket: str, prefix: str = "") -> list[ObjectInfo]:
        gcs_bucket = self._client.lookup_bucket(bucket)
        if gcs_bucket is None:
            return []
        return [
            ObjectInfo(key=blob.name, size=blob.size, last_modified=blob.updated)
            for blob in self._client.list_blobs(bucket, prefix=prefix)
            if blob.name
        ]

    def delete_object(self, bucket: str, key: str) -> bool:
        blob = self._client.bucket(bucket).blob(key)
        if not blob.exists():
            return False
        blob.delete()
        logger.info("Deleted object %s/%s", bucket, key)
        return True

    def presigned_get_url(self, bucket: str, key: str, *, expires_sec: int = 3600) -> str:
        from datetime import timedelta

        blob = self._client.bucket(bucket).blob(key)
        return blob.generate_signed_url(expiration=timedelta(seconds=expires_sec), method="GET")


_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _store
    if _store is not None:
        return _store

    provider = settings.object_store_provider.strip().lower()
    if provider == "gcs":
        _store = GCSObjectStore(project=settings.object_store_gcp_project)
    elif provider == "s3":
        _store = S3ObjectStore(
            endpoint=settings.object_store_endpoint,
            access_key=settings.object_store_access_key,
            secret_key=settings.object_store_secret_key,
            secure=settings.object_store_secure,
        )
    else:
        raise ValueError(f"Unsupported object_store_provider: {provider!r}")
    return _store


# ── Module-level convenience wrappers (mirrors the ObjectStore protocol) ──


def ensure_bucket(bucket: str) -> None:
    get_object_store().ensure_bucket(bucket)


def upload_file(bucket: str, key: str, file_path: str) -> str:
    return get_object_store().upload_file(bucket, key, file_path)


def upload_bytes(bucket: str, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
    return get_object_store().upload_bytes(bucket, key, data, content_type=content_type)


def download_file(bucket: str, key: str, file_path: str) -> bool:
    return get_object_store().download_file(bucket, key, file_path)


def download_bytes(bucket: str, key: str) -> bytes | None:
    return get_object_store().download_bytes(bucket, key)


def list_objects(bucket: str, prefix: str = "") -> list[ObjectInfo]:
    return get_object_store().list_objects(bucket, prefix=prefix)


def delete_object(bucket: str, key: str) -> bool:
    return get_object_store().delete_object(bucket, key)


def presigned_get_url(bucket: str, key: str, *, expires_sec: int = 3600) -> str:
    return get_object_store().presigned_get_url(bucket, key, expires_sec=expires_sec)
