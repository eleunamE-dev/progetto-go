import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from bookreviews.app import create_app
from bookreviews.catalog import CachedCatalog
from bookreviews.config import Settings
from bookreviews.database import SqlReviewRepository, create_engine
from bookreviews.queue import RabbitQueue
from tests.conftest import LogRecords


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


async def test_openapi_documents_the_api(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert {"/healthz", "/readyz", "/book/search", "/review"} <= response.json()["paths"].keys()


async def test_lifespan_sets_up_the_dependencies(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.catalog, CachedCatalog)
        assert isinstance(app.state.reviews, SqlReviewRepository)
        assert isinstance(app.state.queue, RabbitQueue)


async def test_readyz_reports_an_unreachable_database(
    app: FastAPI, client: httpx.AsyncClient, json_logs: LogRecords
) -> None:
    app.state.engine = create_engine("mysql+aiomysql://user:password@127.0.0.1:9/bookreviews")
    try:
        response = await client.get("/readyz")
    finally:
        await app.state.engine.dispose()

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "the database is not reachable"
    assert [r["msg"] for r in json_logs() if r["level"] == "WARNING"] == ["database not reachable"]


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_the_docs_can_be_turned_off() -> None:
    async with client_for(create_app(Settings(api_docs_enabled=False))) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404


PREFLIGHT = {
    "Origin": "https://reviews.example.com",
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type, x-api-key",
}


async def test_cross_origin_requests_are_off_by_default(client: httpx.AsyncClient) -> None:
    response = await client.options("/review", headers=PREFLIGHT)

    assert "access-control-allow-origin" not in response.headers


async def test_cross_origin_requests_from_the_allowed_origins() -> None:
    app = create_app(Settings(cors_allow_origins=["https://reviews.example.com"]))
    async with client_for(app) as client:
        preflight = await client.options("/review", headers=PREFLIGHT)
        stranger = await client.options(
            "/review", headers=PREFLIGHT | {"Origin": "https://elsewhere.example.com"}
        )
        simple = await client.get("/healthz", headers={"Origin": "https://reviews.example.com"})

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://reviews.example.com"
    assert "x-api-key" in preflight.headers["access-control-allow-headers"].lower()
    assert stranger.status_code == 400
    assert "access-control-allow-origin" not in stranger.headers
    assert simple.headers["access-control-allow-origin"] == "https://reviews.example.com"
    assert "x-request-id" in simple.headers["access-control-expose-headers"].lower()


async def test_warns_when_no_api_keys_are_configured(json_logs: LogRecords) -> None:
    app = create_app(Settings(metrics_port=0))

    async with app.router.lifespan_context(app):
        pass

    [record] = [r for r in json_logs() if r["level"] == "WARNING"]
    assert record["msg"] == "no API keys configured, every write will be refused"


async def test_metrics_are_served_on_their_own_port(settings: Settings) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_app(settings.model_copy(update={"http_host": "127.0.0.1", "metrics_port": port}))

    async with app.router.lifespan_context(app), httpx.AsyncClient() as client:
        metrics = await client.get(f"http://127.0.0.1:{port}/metrics")

    assert metrics.status_code == 200
    assert "bookreviews_http_requests_total" in metrics.text
    async with client_for(app) as api:
        assert (await api.get("/metrics")).status_code == 404
