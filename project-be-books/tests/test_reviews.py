import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from bookreviews.app import create_app
from bookreviews.books import get_catalog
from bookreviews.catalog import Book, CatalogCircuitOpenError, CatalogTimeoutError, Person
from bookreviews.config import Settings
from bookreviews.database import SqlReviewRepository, create_engine, create_sessions
from bookreviews.review_service import Review, ReviewStatus
from bookreviews.reviews import get_review_queue, get_review_repository
from tests.fakes import FakeCatalog, FakeQueue, FakeReviewRepository

PRIDE_AND_PREJUDICE = Book(
    id=1342,
    title="Pride and Prejudice",
    authors=(Person("Austen, Jane", 1775, 1817),),
    subjects=("Courtship -- Fiction",),
    bookshelves=("Harvard Classics",),
    languages=("en",),
    summaries=('"Pride and Prejudice" by Jane Austen is a novel published in 1813.',),
    cover_url="https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
    download_count=190246,
)
CREATED_AT = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
VALID = {"id": 1342, "review": "A classic.", "score": 9}


def stored_review(status: ReviewStatus = ReviewStatus.PENDING, book: Book | None = None) -> Review:
    return Review(
        id=uuid.uuid7(),
        book_id=1342,
        content="A classic.",
        score=9,
        status=status,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        book=book,
    )


@pytest.fixture
def repository() -> FakeReviewRepository:
    return FakeReviewRepository()


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog(books={1342: PRIDE_AND_PREJUDICE})


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def app(repository: FakeReviewRepository, catalog: FakeCatalog, queue: FakeQueue) -> FastAPI:
    app = create_app(Settings())
    app.dependency_overrides[get_review_repository] = lambda: repository
    app.dependency_overrides[get_catalog] = lambda: catalog
    app.dependency_overrides[get_review_queue] = lambda: queue
    return app


async def test_submit_review(
    client: httpx.AsyncClient, repository: FakeReviewRepository, queue: FakeQueue
) -> None:
    response = await client.post("/review", json=VALID | {"review": "  A classic.  "})

    assert response.status_code == 202
    body = response.json()
    review_id = uuid.UUID(body["id"])
    assert response.headers["location"] == f"/review/{review_id}"
    assert body == {
        "id": str(review_id),
        "status": "pending",
        "book_id": 1342,
        "review": "A classic.",
        "score": 9,
        "created_at": body["created_at"],
        "updated_at": body["created_at"],
        "book": None,
    }
    assert repository.reviews[review_id].content == "A classic."
    assert queue.enqueued == [review_id]


async def test_book_id_may_be_a_numeric_string(client: httpx.AsyncClient) -> None:
    response = await client.post("/review", json=VALID | {"id": "1342"})

    assert response.status_code == 202
    assert response.json()["book_id"] == 1342


async def test_review_text_may_contain_line_breaks_and_tabs(client: httpx.AsyncClient) -> None:
    text = "Slow start.\n\tThen it grows on you."

    response = await client.post("/review", json=VALID | {"review": text})

    assert response.status_code == 202
    assert response.json()["review"] == text


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"id": None}, "body.id", "Input should be a valid integer"),
        ({"id": 0}, "body.id", "Input should be greater than 0"),
        (
            {"id": ""},
            "body.id",
            "Input should be a valid integer, unable to parse string as an integer",
        ),
        ({"score": 0}, "body.score", "Input should be greater than or equal to 1"),
        ({"score": 11}, "body.score", "Input should be less than or equal to 10"),
        (
            {"score": 6.5},
            "body.score",
            "Input should be a valid integer, got a number with a fractional part",
        ),
        ({"review": "  ok  "}, "body.review", "String should have at least 3 characters"),
        ({"review": "x" * 5001}, "body.review", "String should have at most 5000 characters"),
        ({"review": "Nice\x07"}, "body.review", "Value error, must not contain control characters"),
        ({"rating": 5}, "body.rating", "Extra inputs are not permitted"),
    ],
)
async def test_submit_rejects_invalid_reviews(
    client: httpx.AsyncClient,
    repository: FakeReviewRepository,
    changes: dict[str, Any],
    field: str,
    message: str,
) -> None:
    response = await client.post("/review", json=VALID | changes)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["errors"] == [{"field": field, "message": message}]
    assert repository.reviews == {}


async def test_submit_requires_every_field(client: httpx.AsyncClient) -> None:
    response = await client.post("/review", json={})

    assert response.status_code == 422
    assert {e["field"] for e in response.json()["errors"]} == {
        "body.id",
        "body.review",
        "body.score",
    }


async def test_submit_rejects_books_missing_from_the_catalog(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    response = await client.post("/review", json=VALID | {"id": 999999})

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"field": "body.id", "message": "no book with this id in the catalog"}
    ]
    assert repository.reviews == {}


async def test_submit_when_the_catalog_does_not_answer(
    client: httpx.AsyncClient, catalog: FakeCatalog, repository: FakeReviewRepository
) -> None:
    catalog.error = CatalogTimeoutError("Gutendex GET /books/1342/ timed out")

    response = await client.post("/review", json=VALID)

    assert response.status_code == 504
    assert repository.reviews == {}


async def test_submit_while_the_catalog_is_suspended(
    client: httpx.AsyncClient, catalog: FakeCatalog, repository: FakeReviewRepository
) -> None:
    catalog.error = CatalogCircuitOpenError(20)

    response = await client.post("/review", json=VALID)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "20"
    assert repository.reviews == {}


async def test_reviews_are_unavailable_while_the_database_is_down(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    engine = create_engine("mysql+aiomysql://user:password@127.0.0.1:9/bookreviews")
    repository = SqlReviewRepository(create_sessions(engine))
    app.dependency_overrides[get_review_repository] = lambda: repository
    try:
        response = await client.get(f"/review/{uuid.uuid7()}")
    finally:
        await engine.dispose()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["detail"] == "the database is not available, try again later"


async def test_get_pending_review(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    review = stored_review(ReviewStatus.PENDING)
    await repository.add(review)

    response = await client.get(f"/review/{review.id}")

    assert response.status_code == 202
    assert response.headers["retry-after"] == "5"
    assert response.json() == {
        "id": str(review.id),
        "status": "pending",
        "book_id": 1342,
        "review": "A classic.",
        "score": 9,
        "created_at": "2026-10-03T12:00:00Z",
        "updated_at": "2026-10-03T12:00:00Z",
        "book": None,
    }


async def test_get_completed_review(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    review = stored_review(ReviewStatus.COMPLETED, PRIDE_AND_PREJUDICE)
    await repository.add(review)

    response = await client.get(f"/review/{review.id}")

    assert response.status_code == 200
    assert "retry-after" not in response.headers
    body = response.json()
    assert body["status"] == "completed"
    assert body["book"] == {
        "id": 1342,
        "title": "Pride and Prejudice",
        "authors": [{"name": "Austen, Jane", "birth_year": 1775, "death_year": 1817}],
        "subjects": ["Courtship -- Fiction"],
        "bookshelves": ["Harvard Classics"],
        "languages": ["en"],
        "summaries": ['"Pride and Prejudice" by Jane Austen is a novel published in 1813.'],
        "cover_url": "https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
        "download_count": 190246,
    }


async def test_get_failed_review(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    review = stored_review(ReviewStatus.FAILED)
    await repository.add(review)

    response = await client.get(f"/review/{review.id}")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["book"] is None


async def test_get_unknown_review(client: httpx.AsyncClient) -> None:
    review_id = uuid.uuid7()

    response = await client.get(f"/review/{review_id}")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == f"no review with id {review_id}"


async def test_review_ids_must_be_uuids(client: httpx.AsyncClient) -> None:
    response = await client.get("/review/42")

    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["path.review_id"]


async def test_update_review(client: httpx.AsyncClient, repository: FakeReviewRepository) -> None:
    review = stored_review()
    await repository.add(review)

    response = await client.put(
        f"/review/{review.id}", json={"review": " Better on a second read. ", "score": 10}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["review"] == "Better on a second read."
    assert body["score"] == 10
    assert body["book_id"] == 1342
    assert body["created_at"] == "2026-10-03T12:00:00Z"
    assert datetime.fromisoformat(body["updated_at"]) > CREATED_AT
    assert repository.reviews[review.id].content == "Better on a second read."


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"review": "A classic."}, "body.score"),
        ({"score": 9}, "body.review"),
        ({"review": "A classic.", "score": 9, "id": 84}, "body.id"),
    ],
)
async def test_update_rejects_invalid_changes(
    client: httpx.AsyncClient, repository: FakeReviewRepository, payload: dict[str, Any], field: str
) -> None:
    review = stored_review()
    await repository.add(review)

    response = await client.put(f"/review/{review.id}", json=payload)

    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == [field]
    assert repository.reviews[review.id] == review


async def test_update_unknown_review(client: httpx.AsyncClient) -> None:
    response = await client.put(f"/review/{uuid.uuid7()}", json={"review": "Changed.", "score": 1})

    assert response.status_code == 404


async def test_delete_review(client: httpx.AsyncClient, repository: FakeReviewRepository) -> None:
    review = stored_review()
    await repository.add(review)

    response = await client.delete(f"/review/{review.id}")

    assert response.status_code == 204
    assert response.content == b""
    assert repository.reviews == {}
    assert (await client.get(f"/review/{review.id}")).status_code == 404


async def test_delete_unknown_review(client: httpx.AsyncClient) -> None:
    response = await client.delete(f"/review/{uuid.uuid7()}")

    assert response.status_code == 404


async def test_reviews_are_documented(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert set(paths["/review"]) == {"post"}
    assert set(paths["/review/{review_id}"]) == {"get", "put", "delete"}
    assert {"200", "202", "404", "503"} <= paths["/review/{review_id}"]["get"]["responses"].keys()
    assert "503" in paths["/review"]["post"]["responses"]


def test_get_review_repository_reads_the_application_state() -> None:
    app = FastAPI()
    repository = FakeReviewRepository()
    app.state.reviews = repository

    assert get_review_repository(Request({"type": "http", "app": app})) is repository


def test_get_review_queue_reads_the_application_state() -> None:
    app = FastAPI()
    queue = FakeQueue()
    app.state.queue = queue

    assert get_review_queue(Request({"type": "http", "app": app})) is queue
