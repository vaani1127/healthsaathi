from pathlib import Path

import httpx
import pytest

from app.storage import AzureBlobStorage, LocalStorage, StorageError, sha256_hex


async def test_local_round_trip(tmp_path: Path) -> None:
    store = LocalStorage(tmp_path)
    await store.put("c1/p1/doc.pdf", b"data", "application/pdf")
    assert await store.get("c1/p1/doc.pdf") == b"data"
    with pytest.raises(StorageError):
        await store.get("c1/p1/missing.pdf")


@pytest.mark.parametrize("bad", ["../escape.pdf", "/abs/path.pdf", "a/../../b", ""])
async def test_paths_cannot_escape(tmp_path: Path, bad: str) -> None:
    with pytest.raises(StorageError):
        await LocalStorage(tmp_path).put(bad, b"x", "application/pdf")


async def test_azure_uses_sas_url_and_block_blobs() -> None:
    seen: list[httpx.Request] = []
    blobs: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "PUT":
            blobs[request.url.path] = request.content
            return httpx.Response(201)
        if request.url.path in blobs:
            return httpx.Response(200, content=blobs[request.url.path])
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = AzureBlobStorage("https://acct.blob.core.windows.net/lab-docs?sv=1&sig=abc", client)
    await store.put("c1/doc.pdf", b"pdf", "application/pdf")
    assert await store.get("c1/doc.pdf") == b"pdf"
    put = seen[0]
    assert put.url.path == "/lab-docs/c1/doc.pdf"
    assert put.url.params["sig"] == "abc"
    assert put.headers["x-ms-blob-type"] == "BlockBlob"
    with pytest.raises(StorageError, match="not found"):
        await store.get("c1/other.pdf")


async def test_azure_errors_are_reported() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    store = AzureBlobStorage("https://acct.blob.core.windows.net/lab-docs?sig=x", client)
    with pytest.raises(StorageError, match="403"):
        await store.put("a.pdf", b"x", "application/pdf")
    with pytest.raises(StorageError, match="403"):
        await store.get("a.pdf")


def test_sha256_hex() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
