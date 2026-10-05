import re
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
from tests.conftest import CLIENT, OTHER_API_KEY, OTHER_CLIENT, LogRecords
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


def stored_review(
    status: ReviewStatus = ReviewStatus.PENDING, book: Book | None = None, owner: str = CLIENT
) -> Review:
    return Review(
        id=uuid.uuid7(),
        book_id=1342,
        content="A classic.",
        score=9,
        status=status,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        owner=owner,
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
def app(
    settings: Settings,
    repository: FakeReviewRepository,
    catalog: FakeCatalog,
    queue: FakeQueue,
) -> FastAPI:
    app = create_app(settings)
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
    assert repository.reviews[review_id].owner == CLIENT
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
        ({"id": True}, "body.id", "Input should be a whole number or a string of digits"),
        ({"id": 1342.0}, "body.id", "Input should be a whole number or a string of digits"),
        ({"score": 0}, "body.score", "Input should be greater than or equal to 1"),
        ({"score": 11}, "body.score", "Input should be less than or equal to 10"),
        ({"score": 6.5}, "body.score", "Input should be a valid integer"),
        ({"score": 6.0}, "body.score", "Input should be a valid integer"),
        ({"score": True}, "body.score", "Input should be a valid integer"),
        ({"score": "6"}, "body.score", "Input should be a valid integer"),
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
        ({"review": "A classic.", "score": True}, "body.score"),
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


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": ""}, {"X-API-Key": "wrong-key"}])
async def test_writes_need_a_valid_api_key(
    anonymous: httpx.AsyncClient, repository: FakeReviewRepository, headers: dict[str, str]
) -> None:
    review = stored_review()
    await repository.add(review)

    responses = [
        await anonymous.post("/review", json=VALID, headers=headers),
        await anonymous.put(
            f"/review/{review.id}", json={"review": "Changed.", "score": 1}, headers=headers
        ),
        await anonymous.delete(f"/review/{review.id}", headers=headers),
        await anonymous.delete("/review/not-a-uuid", headers=headers),
    ]

    for response in responses:
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "ApiKey"
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["detail"] == "a valid X-API-Key header is required"
    assert repository.reviews == {review.id: review}


async def test_reading_a_review_needs_no_api_key(
    anonymous: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    review = stored_review(ReviewStatus.COMPLETED, PRIDE_AND_PREJUDICE)
    await repository.add(review)

    response = await anonymous.get(f"/review/{review.id}")

    assert response.status_code == 200


async def test_only_the_owner_changes_a_review(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    review = stored_review(owner=OTHER_CLIENT)
    await repository.add(review)

    put = await client.put(f"/review/{review.id}", json={"review": "Changed.", "score": 1})
    delete = await client.delete(f"/review/{review.id}")

    for response in (put, delete):
        assert response.status_code == 403
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["detail"] == f"review {review.id} belongs to another client"
    assert repository.reviews == {review.id: review}


async def test_each_client_writes_with_its_own_key(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    review = stored_review(owner=OTHER_CLIENT)
    await repository.add(review)

    response = await client.delete(f"/review/{review.id}", headers={"X-API-Key": OTHER_API_KEY})

    assert response.status_code == 204
    assert repository.reviews == {}


async def test_the_client_is_logged(client: httpx.AsyncClient, json_logs: LogRecords) -> None:
    response = await client.post("/review", json=VALID)

    [record] = [r for r in json_logs() if r["msg"] == "http request"]
    assert record["client"] == CLIENT
    assert record["request_id"] == response.headers["x-request-id"]


async def test_anonymous_requests_log_no_client(
    anonymous: httpx.AsyncClient, json_logs: LogRecords
) -> None:
    await anonymous.get(f"/review/{uuid.uuid7()}")

    [record] = [r for r in json_logs() if r["msg"] == "http request"]
    assert "client" not in record


async def test_reviews_are_documented(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert set(paths["/review"]) == {"post"}
    assert set(paths["/review/{review_id}"]) == {"get", "put", "delete"}
    post, review = paths["/review"]["post"], paths["/review/{review_id}"]
    assert [p["name"] for p in post["parameters"]] == ["Idempotency-Key"]
    assert {p["name"] for p in review["get"]["parameters"]} == {"review_id", "if-none-match"}
    assert {p["name"] for p in review["put"]["parameters"]} == {"review_id", "if-match"}
    assert {p["name"] for p in review["delete"]["parameters"]} == {"review_id", "if-match"}
    assert {"304"} <= review["get"]["responses"].keys()
    assert "ETag" in review["get"]["responses"]["200"]["headers"]
    assert "412" in review["put"]["responses"]
    assert "412" in review["delete"]["responses"]
    assert post["security"] == [{"ApiKey": []}]
    assert review["put"]["security"] == review["delete"]["security"] == [{"ApiKey": []}]
    assert "security" not in review["get"]
    assert {"401", "413"} <= post["responses"].keys()
    assert {"401", "403", "413"} <= review["put"]["responses"].keys()
    assert {"401", "403"} <= review["delete"]["responses"].keys()
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


async def test_reviews_carry_an_entity_tag(client: httpx.AsyncClient) -> None:
    posted = await client.post("/review", json=VALID)
    location = posted.headers["location"]

    fetched = await client.get(location)
    again = await client.get(location)
    updated = await client.put(location, json={"review": "Changed.", "score": 1})

    assert re.fullmatch(r'"[0-9a-f]{32}"', posted.headers["etag"])
    assert posted.headers["etag"] == fetched.headers["etag"] == again.headers["etag"]
    assert fetched.headers["cache-control"] == "no-cache"
    assert updated.headers["etag"] != fetched.headers["etag"]
    assert (await client.get(location)).headers["etag"] == updated.headers["etag"]


@pytest.mark.parametrize("header", ["{tag}", "W/{tag}", '"older", {tag}', "*"])
async def test_get_answers_304_when_the_client_has_the_current_version(
    client: httpx.AsyncClient, repository: FakeReviewRepository, header: str
) -> None:
    review = stored_review()
    await repository.add(review)
    tag = (await client.get(f"/review/{review.id}")).headers["etag"]

    response = await client.get(
        f"/review/{review.id}", headers={"If-None-Match": header.format(tag=tag)}
    )

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == tag
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["retry-after"] == "5"


async def test_get_answers_in_full_when_the_review_changed(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    review = stored_review(ReviewStatus.COMPLETED, PRIDE_AND_PREJUDICE)
    await repository.add(review)

    response = await client.get(f"/review/{review.id}", headers={"If-None-Match": '"older"'})

    assert response.status_code == 200
    assert response.json()["book"]["title"] == "Pride and Prejudice"
    assert "retry-after" not in response.headers


async def test_if_match_protects_updates_and_deletes(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    review = stored_review()
    await repository.add(review)
    location = f"/review/{review.id}"
    tag = (await client.get(location)).headers["etag"]
    change = {"review": "Changed.", "score": 1}

    stale = await client.put(location, json=change, headers={"If-Match": '"older"'})
    weak = await client.put(location, json=change, headers={"If-Match": f"W/{tag}"})
    updated = await client.put(location, json=change, headers={"If-Match": f'"older", {tag}'})
    late_delete = await client.delete(location, headers={"If-Match": tag})
    deleted = await client.delete(location, headers={"If-Match": updated.headers["etag"]})

    for refused in (stale, weak, late_delete):
        assert refused.status_code == 412
        assert refused.headers["content-type"] == "application/problem+json"
        assert refused.json()["detail"] == (
            f"review {review.id} has changed, read it again to get its current ETag"
        )
    assert updated.status_code == 200
    assert deleted.status_code == 204
    assert repository.reviews == {}


async def test_if_match_any_version(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    review = stored_review()
    await repository.add(review)

    response = await client.put(
        f"/review/{review.id}", json={"review": "Changed.", "score": 1}, headers={"If-Match": "*"}
    )

    assert response.status_code == 200


async def test_a_repeated_post_returns_the_same_review(
    client: httpx.AsyncClient, repository: FakeReviewRepository, queue: FakeQueue
) -> None:
    headers = {"Idempotency-Key": "4f9a0f2e-0b8e-4d1c-9a51-7c1b2d3e4f50"}

    first = await client.post("/review", json=VALID, headers=headers)
    again = await client.post("/review", json=VALID, headers=headers)

    assert first.status_code == again.status_code == 202
    assert again.json() == first.json()
    assert again.headers["location"] == first.headers["location"]
    assert again.headers["etag"] == first.headers["etag"]
    assert len(repository.reviews) == 1
    assert len(queue.enqueued) == 1


async def test_the_idempotency_key_may_be_quoted(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    first = await client.post("/review", json=VALID, headers={"Idempotency-Key": "abc-1"})
    again = await client.post("/review", json=VALID, headers={"Idempotency-Key": '"abc-1"'})

    assert again.json()["id"] == first.json()["id"]
    assert len(repository.reviews) == 1


async def test_an_idempotency_key_reused_for_another_request(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    await client.post("/review", json=VALID, headers={"Idempotency-Key": "abc-1"})

    response = await client.post(
        "/review", json=VALID | {"score": 3}, headers={"Idempotency-Key": "abc-1"}
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {
            "field": "header.Idempotency-Key",
            "message": "abc-1 was already used for a different request",
        }
    ]
    assert len(repository.reviews) == 1


async def test_idempotency_keys_are_per_client(
    client: httpx.AsyncClient, repository: FakeReviewRepository
) -> None:
    mine = await client.post("/review", json=VALID, headers={"Idempotency-Key": "abc-1"})
    theirs = await client.post(
        "/review", json=VALID, headers={"Idempotency-Key": "abc-1", "X-API-Key": OTHER_API_KEY}
    )

    assert mine.json()["id"] != theirs.json()["id"]
    assert len(repository.reviews) == 2


@pytest.mark.parametrize("key", ["", "with space", "x" * 256, "a/b"])
async def test_invalid_idempotency_keys_are_rejected(
    client: httpx.AsyncClient, repository: FakeReviewRepository, key: str
) -> None:
    response = await client.post("/review", json=VALID, headers={"Idempotency-Key": key})

    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["header.Idempotency-Key"]
    assert repository.reviews == {}
