import httpx
from fastapi import FastAPI

from bookreviews.gutendex import GutendexClient


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


async def test_openapi_documents_the_api(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert {"/healthz", "/book/search"} <= response.json()["paths"].keys()


async def test_lifespan_opens_and_closes_the_catalog_client(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        catalog = app.state.catalog
        assert isinstance(catalog, GutendexClient)
        assert not catalog.is_closed

    assert catalog.is_closed
