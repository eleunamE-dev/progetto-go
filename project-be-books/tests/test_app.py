import httpx
from fastapi import FastAPI

from bookreviews.catalog import CachedCatalog
from bookreviews.database import SqlReviewRepository, create_engine
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
