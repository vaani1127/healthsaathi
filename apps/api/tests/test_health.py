import json
import logging

import pytest
from httpx import AsyncClient

from app.core.logging import JsonFormatter, request_id_var


async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_ready_checks_database(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


async def test_request_id_is_generated_and_echoed(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert len(resp.headers["x-request-id"]) == 32

    resp = await client.get("/api/v1/health", headers={"x-request-id": "abc-123"})
    assert resp.headers["x-request-id"] == "abc-123"


async def test_invalid_request_id_is_replaced(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/health", headers={"x-request-id": "bad id\n"})
    assert resp.headers["x-request-id"] != "bad id\n"


def test_json_formatter_includes_request_id() -> None:
    token = request_id_var.set("req-1")
    try:
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", (), None)
        record.path = "/x"
        out = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert out["msg"] == "hello"
    assert out["request_id"] == "req-1"
    assert out["path"] == "/x"


@pytest.mark.parametrize("path", ["/api/v1/openapi.json"])
async def test_openapi_lists_health_routes(client: AsyncClient, path: str) -> None:
    paths = (await client.get(path)).json()["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/ready" in paths
