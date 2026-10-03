import httpx
import pytest
from fastapi import FastAPI, HTTPException

from bookreviews.app import create_app
from bookreviews.config import Settings


@pytest.fixture
def app() -> FastAPI:
    app = create_app(Settings())

    @app.get("/items/{item_id}")
    async def read_item(item_id: int, q: str) -> dict[str, object]:
        return {"item_id": item_id, "q": q}

    @app.get("/conflict")
    async def conflict() -> None:
        raise HTTPException(status_code=409, detail="the item was changed in the meantime")

    return app


async def test_unknown_path(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "title": "Not Found",
        "status": 404,
        "instance": "/nope",
        "request_id": response.headers["x-request-id"],
    }


async def test_unsupported_method(client: httpx.AsyncClient) -> None:
    response = await client.delete("/healthz")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["title"] == "Method Not Allowed"


async def test_validation_errors_list_the_invalid_fields(client: httpx.AsyncClient) -> None:
    response = await client.get("/items/abc")

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["title"] == "Unprocessable Content"
    assert body["detail"] == "the request is not valid"
    assert body["errors"] == [
        {
            "field": "path.item_id",
            "message": "Input should be a valid integer, unable to parse string as an integer",
        },
        {"field": "query.q", "message": "Field required"},
    ]


async def test_http_errors_keep_their_detail(client: httpx.AsyncClient) -> None:
    response = await client.get("/conflict")

    assert response.status_code == 409
    assert response.json()["detail"] == "the item was changed in the meantime"
