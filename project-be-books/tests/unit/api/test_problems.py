import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import exc as sqlalchemy_errors

from bookreviews.api.app import create_app
from bookreviews.config import Settings
from tests.conftest import LogRecords


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/items/{item_id}")
    async def read_item(item_id: int, q: str) -> dict[str, object]:
        return {"item_id": item_id, "q": q}

    @app.get("/conflict")
    async def conflict() -> None:
        raise HTTPException(status_code=409, detail="the item was changed in the meantime")

    @app.get("/database")
    async def database(request: Request) -> None:
        raise request.app.state.database_error

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
    assert body["instance"] == "/items/abc"
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


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (
            sqlalchemy_errors.OperationalError(
                "SELECT 1", {}, ConnectionRefusedError(111, "Connection refused")
            ),
            "ConnectionRefusedError: [Errno 111] Connection refused",
        ),
        (
            sqlalchemy_errors.InterfaceError("SELECT 1", {}, BrokenPipeError(32, "Broken pipe")),
            "BrokenPipeError: [Errno 32] Broken pipe",
        ),
        (
            sqlalchemy_errors.TimeoutError("QueuePool limit of size 5 overflow 10 reached"),
            "TimeoutError: QueuePool limit of size 5 overflow 10 reached",
        ),
    ],
)
async def test_database_outages_ask_to_retry_later(
    app: FastAPI,
    client: httpx.AsyncClient,
    json_logs: LogRecords,
    error: Exception,
    logged: str,
) -> None:
    app.state.database_error = error

    response = await client.get("/database")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "the database is not available, try again later"
    assert response.json()["instance"] == "/database"
    assert "SELECT" not in response.text
    [record] = [r for r in json_logs() if r["msg"] == "database unavailable"]
    assert record["level"] == "ERROR"
    assert record["error"] == logged
    assert record["request_id"] == response.headers["x-request-id"]
