"""Unit tests for the provider-neutral object-store abstraction."""

from __future__ import annotations

import pytest

from shared.lib.config import settings
from shared.lib.object_store import GCSObjectStore, S3ObjectStore, get_object_store

pytestmark = pytest.mark.unit


class _FakeMinioClient:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.buckets.add(bucket)

    def fput_object(self, bucket, key, file_path):
        with open(file_path, "rb") as handle:
            self.objects[(bucket, key)] = handle.read()

    def put_object(self, bucket, key, stream, length, content_type):  # noqa: ARG002
        self.objects[(bucket, key)] = stream.read()

    def presigned_get_object(self, bucket, key, expires):  # noqa: ARG002
        return f"https://fake-s3/{bucket}/{key}?signed=1"


def test_s3_object_store_upload_bytes_ensures_bucket_and_stores_object(tmp_path):
    client = _FakeMinioClient()
    store = S3ObjectStore(endpoint="x", access_key="a", secret_key="b", client=client)

    key = store.upload_bytes("my-bucket", "path/to/obj", b"hello")

    assert key == "path/to/obj"
    assert "my-bucket" in client.buckets
    assert client.objects[("my-bucket", "path/to/obj")] == b"hello"


def test_s3_object_store_presigned_get_url(tmp_path):
    client = _FakeMinioClient()
    store = S3ObjectStore(endpoint="x", access_key="a", secret_key="b", client=client)
    url = store.presigned_get_url("bucket", "key", expires_sec=60)
    assert url == "https://fake-s3/bucket/key?signed=1"


class _FakeBlob:
    def __init__(self, name: str, bucket: _FakeGCSBucket) -> None:
        self.name = name
        self._bucket = bucket
        self.size = None
        self.updated = None

    def exists(self) -> bool:
        return self.name in self._bucket.data

    def upload_from_filename(self, file_path: str) -> None:
        with open(file_path, "rb") as handle:
            self._bucket.data[self.name] = handle.read()

    def upload_from_string(self, data: bytes, content_type: str) -> None:  # noqa: ARG002
        self._bucket.data[self.name] = data

    def download_as_bytes(self) -> bytes:
        return self._bucket.data[self.name]

    def delete(self) -> None:
        del self._bucket.data[self.name]

    def generate_signed_url(self, expiration, method):  # noqa: ARG002
        return f"https://fake-gcs/{self._bucket.name}/{self.name}?signed=1"


class _FakeGCSBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.data: dict[str, bytes] = {}

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(name, self)


class _FakeGCSClient:
    def __init__(self) -> None:
        self._buckets: dict[str, _FakeGCSBucket] = {}

    def lookup_bucket(self, bucket: str):
        return self._buckets.get(bucket)

    def create_bucket(self, bucket: str):
        created = _FakeGCSBucket(bucket)
        self._buckets[bucket] = created
        return created

    def bucket(self, bucket: str) -> _FakeGCSBucket:
        return self._buckets.setdefault(bucket, _FakeGCSBucket(bucket))

    def list_blobs(self, bucket: str, prefix: str = ""):
        gcs_bucket = self._buckets.get(bucket)
        if gcs_bucket is None:
            return []
        return [gcs_bucket.blob(name) for name in gcs_bucket.data if name.startswith(prefix)]


def test_gcs_object_store_upload_and_download_roundtrip():
    client = _FakeGCSClient()
    store = GCSObjectStore(client=client)

    store.upload_bytes("bucket", "a/b.txt", b"payload")

    assert store.download_bytes("bucket", "a/b.txt") == b"payload"
    assert store.download_bytes("bucket", "missing") is None


def test_gcs_object_store_list_objects_and_delete():
    client = _FakeGCSClient()
    store = GCSObjectStore(client=client)
    store.upload_bytes("bucket", "agent-1/latest.tar.gz", b"x")
    store.upload_bytes("bucket", "agent-2/latest.tar.gz", b"y")

    infos = store.list_objects("bucket")
    assert {info.key for info in infos} == {"agent-1/latest.tar.gz", "agent-2/latest.tar.gz"}

    assert store.delete_object("bucket", "agent-1/latest.tar.gz") is True
    assert store.delete_object("bucket", "agent-1/latest.tar.gz") is False


def test_gcs_object_store_presigned_get_url():
    client = _FakeGCSClient()
    store = GCSObjectStore(client=client)
    store.upload_bytes("bucket", "key", b"x")
    url = store.presigned_get_url("bucket", "key")
    assert url == "https://fake-gcs/bucket/key?signed=1"


def test_get_object_store_selects_provider_from_settings(monkeypatch):
    import shared.lib.object_store as object_store_mod

    monkeypatch.setattr(object_store_mod, "_store", None)
    monkeypatch.setattr(settings, "object_store_provider", "s3")
    monkeypatch.setattr(settings, "object_store_secret_key", "secret")

    store = get_object_store()
    assert isinstance(store, S3ObjectStore)


def test_get_object_store_rejects_unknown_provider(monkeypatch):
    import shared.lib.object_store as object_store_mod

    monkeypatch.setattr(object_store_mod, "_store", None)
    monkeypatch.setattr(settings, "object_store_provider", "azure")

    with pytest.raises(ValueError, match="Unsupported object_store_provider"):
        get_object_store()
