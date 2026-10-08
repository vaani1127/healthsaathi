"""File storage behind one interface: a local folder in development, Azure Blob in production.

Azure is used through its REST API with a container SAS URL, so no SDK or account key is needed in
the app; the SAS can be limited to one container and rotated.
"""

import asyncio
import hashlib
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import REPO_ROOT, get_settings


class StorageError(Exception):
    pass


class BlobStorage(Protocol):
    async def put(self, path: str, data: bytes, content_type: str) -> None: ...

    async def get(self, path: str) -> bytes: ...


def _safe_path(path: str) -> PurePosixPath:
    clean = PurePosixPath(path)
    if clean.is_absolute() or ".." in clean.parts or not clean.parts:
        raise StorageError("invalid blob path")
    return clean


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        target = self.root / _safe_path(path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)

    async def get(self, path: str) -> bytes:
        target = self.root / _safe_path(path)
        if not target.is_file():
            raise StorageError("blob not found")
        return await asyncio.to_thread(target.read_bytes)


class AzureBlobStorage:
    """Uses a container URL with a SAS query string, for example
    https://acct.blob.core.windows.net/lab-docs?sv=...&sig=..."""

    def __init__(self, container_sas_url: str, client: httpx.AsyncClient | None = None) -> None:
        parts = urlsplit(container_sas_url)
        self._base = parts
        self._client = client

    def _url(self, path: str) -> str:
        p = self._base
        return urlunsplit(
            (p.scheme, p.netloc, f"{p.path.rstrip('/')}/{_safe_path(path)}", p.query, "")
        )

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(method, self._url(path), **kwargs)  # type: ignore[arg-type]
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.request(method, self._url(path), **kwargs)  # type: ignore[arg-type]

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        resp = await self._request(
            "PUT",
            path,
            content=data,
            headers={"x-ms-blob-type": "BlockBlob", "content-type": content_type},
        )
        if resp.status_code not in (200, 201):
            raise StorageError(f"blob upload failed with status {resp.status_code}")

    async def get(self, path: str) -> bytes:
        resp = await self._request("GET", path)
        if resp.status_code == 404:
            raise StorageError("blob not found")
        if resp.status_code != 200:
            raise StorageError(f"blob download failed with status {resp.status_code}")
        return resp.content


@lru_cache
def get_storage() -> BlobStorage:
    settings = get_settings()
    if settings.azure_blob_container_sas_url is not None:
        return AzureBlobStorage(settings.azure_blob_container_sas_url.get_secret_value())
    root = Path(settings.storage_dir) if settings.storage_dir else REPO_ROOT / "data" / "blobs"
    return LocalStorage(root)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
