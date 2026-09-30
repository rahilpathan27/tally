"""Blob storage for payout instructions, dispute evidence, recon files and audit anchors."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Protocol


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes) -> str:
        """Store ``data`` and return its SHA-256 hex digest."""

    async def get(self, key: str) -> bytes: ...

    async def list(self, prefix: str) -> list[str]: ...


def _check_key(key: str) -> str:
    if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key:
        raise ValueError("object keys must be relative paths without traversal")
    return key


class FilesystemObjectStore:
    """Local directory store used by tests and when no S3 endpoint is configured."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def put(self, key: str, data: bytes) -> str:
        path = self.root / _check_key(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return hashlib.sha256(data).hexdigest()

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread((self.root / _check_key(key)).read_bytes)

    async def list(self, prefix: str) -> list[str]:
        base = self.root
        return sorted(
            str(p.relative_to(base))
            for p in base.rglob("*")
            if p.is_file() and str(p.relative_to(base)).startswith(prefix)
        )


class S3ObjectStore:
    """S3-compatible store (SeaweedFS locally; AWS S3 with SSE-KMS/Object Lock in cloud)."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str = "ap-south-1",
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        import boto3

        self.bucket = bucket
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        self._ready = False

    async def _ensure_bucket(self) -> None:
        if self._ready:
            return

        def create() -> None:
            existing = {b["Name"] for b in self._client.list_buckets().get("Buckets", [])}
            if self.bucket not in existing:
                self._client.create_bucket(Bucket=self.bucket)

        await asyncio.to_thread(create)
        self._ready = True

    async def put(self, key: str, data: bytes) -> str:
        await self._ensure_bucket()
        digest = hashlib.sha256(data).hexdigest()
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket,
            Key=_check_key(key),
            Body=data,
            Metadata={"sha256": digest},
        )
        return digest

    async def get(self, key: str) -> bytes:
        await self._ensure_bucket()
        response = await asyncio.to_thread(
            self._client.get_object, Bucket=self.bucket, Key=_check_key(key)
        )
        return bytes(response["Body"].read())

    async def list(self, prefix: str) -> list[str]:
        await self._ensure_bucket()
        response = await asyncio.to_thread(
            self._client.list_objects_v2, Bucket=self.bucket, Prefix=prefix
        )
        return sorted(item["Key"] for item in response.get("Contents", []))
