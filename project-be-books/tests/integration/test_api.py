import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from bookreviews.adapters.database import SqlReviewRepository
from bookreviews.api.app import create_app
from bookreviews.api.books import get_catalog
from bookreviews.api.reviews import get_review_queue, get_review_repository
from bookreviews.config import Settings
from bookreviews.core.catalog import Book
from bookreviews.core.reviews import Review, utc_now
from tests.fakes import FakeCatalog, FakeQueue

pytestmark = pytest.mark.integration


@pytest.fixture
def app(settings: Settings, repository: SqlReviewRepository, engine: AsyncEngine) -> FastAPI:
    app = create_app(settings)
    catalog = FakeCatalog(books={1342: Book(id=1342, title="Pride and Prejudice")})
    app.dependency_overrides[get_review_repository] = lambda: repository
    app.dependency_overrides[get_catalog] = lambda: catalog
    queue = FakeQueue()
    app.dependency_overrides[get_review_queue] = lambda: queue
    app.state.engine = engine
    return app


async def test_review_lifecycle(client: httpx.AsyncClient) -> None:
    submitted = await client.post("/review", json={"id": 1342, "review": "A classic.", "score": 9})
    assert submitted.status_code == 202
    location = submitted.headers["location"]

    fetched = await client.get(location)
    assert fetched.status_code == 202
    assert fetched.json() == submitted.json()

    updated = await client.put(location, json={"review": "Even better.", "score": 10})
    assert updated.status_code == 200
    assert (await client.get(location)).json()["review"] == "Even better."

    assert (await client.delete(location)).status_code == 204
    assert (await client.get(location)).status_code == 404


async def test_readyz(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_conditional_requests(client: httpx.AsyncClient) -> None:
    submitted = await client.post("/review", json={"id": 1342, "review": "A classic.", "score": 9})
    location, tag = submitted.headers["location"], submitted.headers["etag"]
    change = {"review": "Even better.", "score": 10}

    assert (await client.get(location, headers={"If-None-Match": tag})).status_code == 304
    assert (
        await client.put(location, json=change, headers={"If-Match": '"older"'})
    ).status_code == 412
    updated = await client.put(location, json=change, headers={"If-Match": tag})
    assert updated.status_code == 200
    assert (await client.delete(location, headers={"If-Match": tag})).status_code == 412
    deleted = await client.delete(location, headers={"If-Match": updated.headers["etag"]})
    assert deleted.status_code == 204


async def test_a_repeated_post_creates_one_review(client: httpx.AsyncClient) -> None:
    review = {"id": 1342, "review": "A classic.", "score": 9}
    headers = {"Idempotency-Key": "integration-1"}

    first = await client.post("/review", json=review, headers=headers)
    again = await client.post("/review", json=review, headers=headers)
    other = await client.post("/review", json=review | {"score": 1}, headers=headers)

    assert first.status_code == again.status_code == 202
    assert again.json()["id"] == first.json()["id"]
    assert other.status_code == 422


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_shared_book_refresh_during_conditional_write(
    client: httpx.AsyncClient,
    repository: SqlReviewRepository,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    payload = {"id": 1342, "review": "A classic.", "score": 9}
    first = await client.post("/review", json=payload)
    second = await client.post("/review", json=payload)
    review_id = uuid.UUID(first.json()["id"])
    other_id = uuid.UUID(second.json()["id"])
    await repository.complete(review_id, Book(1342, "Original title"), utc_now())
    location = first.headers["location"]
    before = await client.get(location)
    original_get = repository.get

    async def read_then_refresh(target: uuid.UUID) -> Review | None:
        review = await original_get(target)
        await repository.complete(other_id, Book(1342, "Refreshed title"), utc_now())
        return review

    with monkeypatch.context() as patch:
        patch.setattr(repository, "get", read_then_refresh)
        response = await client.request(
            method,
            location,
            headers={"If-Match": before.headers["etag"]},
            json={"review": "Changed text.", "score": 1} if method == "PUT" else None,
        )
    assert response.status_code == 412
    current = await client.get(location)
    assert current.json()["review"] == payload["review"]
    assert current.json()["book"]["title"] == "Refreshed title"
    assert current.headers["etag"] != before.headers["etag"]
    accepted = await client.request(
        method,
        location,
        headers={"If-Match": current.headers["etag"]},
        json={"review": "Changed text.", "score": 1} if method == "PUT" else None,
    )
    assert accepted.status_code == (200 if method == "PUT" else 204)
