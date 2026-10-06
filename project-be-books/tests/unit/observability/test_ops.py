import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from bookreviews.observability import ops
from bookreviews.observability.ops import OpsServer


@pytest.fixture
async def server() -> AsyncIterator[OpsServer]:
    server = OpsServer("127.0.0.1", 0)
    await server.start()
    yield server
    await server.close()


async def test_serves_the_metrics(server: OpsServer) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{server.port}/metrics?debug=1")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=")
    assert "# TYPE bookreviews_http_requests_total counter" in response.text
    assert "process_resident_memory_bytes" in response.text or "python_info" in response.text


async def test_answers_health_checks(server: OpsServer) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{server.port}/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_other_requests(server: OpsServer) -> None:
    async with httpx.AsyncClient() as client:
        missing = await client.get(f"http://127.0.0.1:{server.port}/nope")
        posted = await client.post(f"http://127.0.0.1:{server.port}/metrics")

    assert missing.status_code == 404
    assert posted.status_code == 405


async def test_survives_garbage(server: OpsServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(b"nonsense\r\n\r\n")
    await writer.drain()
    assert await reader.read() == b""
    writer.close()

    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.close()

    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{server.port}/healthz")
    assert response.status_code == 200


async def test_disconnects_clients_that_send_nothing(
    server: OpsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops, "READ_TIMEOUT", 0.1)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)

    assert await asyncio.wait_for(reader.read(), timeout=2) == b""
    writer.close()


async def test_close_is_idempotent() -> None:
    server = OpsServer("127.0.0.1", 0)
    assert server.port == 0

    await server.start()
    port = server.port
    await server.close()
    await server.close()

    assert port > 0
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{port}/healthz")
