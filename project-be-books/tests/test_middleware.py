import re
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from bookreviews.middleware import RequestContextMiddleware
from tests.conftest import LogRecords


async def test_generates_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_keeps_the_callers_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "req-42.retry_1"})

    assert response.headers["x-request-id"] == "req-42.retry_1"


@pytest.mark.parametrize("request_id", ['id" injected="1', "with space", "a" * 65])
async def test_replaces_an_unsafe_request_id(client: httpx.AsyncClient, request_id: str) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": request_id})

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_access_log(client: httpx.AsyncClient, json_logs: LogRecords) -> None:
    await client.get("/healthz?verbose=1", headers={"X-Request-ID": "req-42"})

    [record] = [r for r in json_logs() if r["msg"] == "http request"]
    assert record["level"] == "INFO"
    assert record["logger"] == "bookreviews.http"
    assert record["method"] == "GET"
    assert record["path"] == "/healthz"
    assert record["status"] == 200
    assert record["request_id"] == "req-42"
    assert record["duration_ms"] >= 0


async def test_unhandled_errors_become_500_problems(
    app: FastAPI, client: httpx.AsyncClient, json_logs: LogRecords
) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals")

    response = await client.get("/boom")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    request_id = response.headers["x-request-id"]
    assert response.json() == {
        "title": "Internal Server Error",
        "status": 500,
        "instance": "/boom",
        "request_id": request_id,
    }
    assert "secret internals" not in response.text

    records = json_logs()
    [error] = [r for r in records if r["msg"] == "unhandled error"]
    assert error["level"] == "ERROR"
    assert error["request_id"] == request_id
    assert "RuntimeError: secret internals" in error["exception"]
    [access] = [r for r in records if r["msg"] == "http request"]
    assert access["level"] == "ERROR"
    assert access["status"] == 500


async def test_errors_after_the_response_started_are_reraised(
    app: FastAPI, client: httpx.AsyncClient, json_logs: LogRecords
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("stream broke")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(chunks())

    with pytest.raises(RuntimeError, match="stream broke"):
        await client.get("/stream")

    [access] = [r for r in json_logs() if r["msg"] == "http request"]
    assert access["status"] == 200


async def test_passes_other_scopes_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        pass

    await RequestContextMiddleware(inner)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]
