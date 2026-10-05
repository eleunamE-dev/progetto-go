import re
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bookreviews.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
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
    assert response.headers["x-content-type-options"] == "nosniff"

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


@pytest.mark.parametrize(
    "middleware",
    [
        RequestContextMiddleware,
        SecurityHeadersMiddleware,
        lambda app: BodySizeLimitMiddleware(app, max_size=10),
    ],
)
async def test_passes_other_scopes_through(middleware: Callable[[ASGIApp], ASGIApp]) -> None:
    seen: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        pass

    await middleware(inner)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]


async def test_security_headers(client: httpx.AsyncClient) -> None:
    for response in (await client.get("/healthz"), await client.get("/nope")):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"] == (
            "default-src 'none'; frame-ancestors 'none'"
        )


async def test_the_docs_page_may_load_its_scripts(client: httpx.AsyncClient) -> None:
    response = await client.get("/docs")

    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"


async def test_endpoints_may_set_their_own_cache_policy(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    @app.get("/cached")
    async def cached(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "max-age=60"
        return {}

    response = await client.get("/cached")

    assert response.headers["cache-control"] == "max-age=60"


async def test_bodies_up_to_the_limit_are_read(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/review", content=b"x" * 65_536, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422


async def test_a_declared_length_over_the_limit_is_refused(
    anonymous: httpx.AsyncClient, json_logs: LogRecords
) -> None:
    response = await anonymous.post(
        "/review", content=b"x" * 65_537, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "title": "Content Too Large",
        "status": 413,
        "detail": "the request body must not exceed 65536 bytes",
        "instance": "/review",
        "request_id": response.headers["x-request-id"],
    }
    [record] = [r for r in json_logs() if r["msg"] == "http request"]
    assert record["status"] == 413


async def test_a_streamed_body_over_the_limit_is_refused(client: httpx.AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * 30_000

    response = await client.post(
        "/review", content=chunks(), headers={"Content-Type": "application/json"}
    )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["detail"] == "the request body must not exceed 65536 bytes"
