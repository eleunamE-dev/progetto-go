import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from bookreviews.adapters.catalog import CachedCatalog, ResilienceOptions, ResilientCatalog
from bookreviews.adapters.queue import RabbitQueue, Topology
from bookreviews.api.dependencies import get_catalog, get_review_queue, get_review_repository
from bookreviews.core.catalog import (
    Book,
    BookNotFoundError,
    CatalogBusyError,
    CatalogCircuitOpenError,
    CatalogTimeoutError,
    CatalogUnavailableError,
)
from bookreviews.core.enrichment import ReviewEnricher
from bookreviews.core.reviews import (
    IdempotencyKey,
    QueueUnavailableError,
    Review,
    ReviewService,
    ReviewStatus,
)
from bookreviews.observability.catalog import MeasuredCatalog
from bookreviews.worker.messages import MessageHandler
from bookreviews.worker.sweeper import Sweeper, SweepPolicy
from tests.conftest import sample
from tests.fakes import FakeCatalog, FakeMessage, FakeQueue, FakeReviewRepository

BOOK = Book(id=1342, title="Pride and Prejudice")
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def pending(created_at: datetime = NOW) -> Review:
    return Review(
        id=uuid.uuid7(),
        book_id=1342,
        content="A classic.",
        score=9,
        status=ReviewStatus.PENDING,
        created_at=created_at,
        updated_at=created_at,
        owner="tests",
    )


async def test_http_requests_are_counted_by_route_template(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    repository = FakeReviewRepository()
    catalog, queue = FakeCatalog(), FakeQueue()
    app.dependency_overrides[get_review_repository] = lambda: repository
    app.dependency_overrides[get_catalog] = lambda: catalog
    app.dependency_overrides[get_review_queue] = lambda: queue
    route = {"method": "GET", "route": "/review/{review_id}"}
    before = sample("bookreviews_http_requests_total", **route, status="404")
    unmatched = sample(
        "bookreviews_http_requests_total", method="GET", route="unmatched", status="404"
    )
    timed = sample("bookreviews_http_request_duration_seconds_count", **route)

    await client.get(f"/review/{uuid.uuid7()}")
    await client.get(f"/review/{uuid.uuid7()}")
    await client.get("/no/such/page")

    assert sample("bookreviews_http_requests_total", **route, status="404") == before + 2
    assert (
        sample("bookreviews_http_requests_total", method="GET", route="unmatched", status="404")
        == unmatched + 1
    )
    assert sample("bookreviews_http_request_duration_seconds_count", **route) == timed + 2


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (None, "ok"),
        (BookNotFoundError(999), "not_found"),
        (CatalogTimeoutError("timed out"), "timeout"),
        (CatalogUnavailableError("503"), "unavailable"),
        (RuntimeError("bug"), "error"),
    ],
)
async def test_catalog_calls_are_counted_and_timed(error: Exception | None, outcome: str) -> None:
    inner = FakeCatalog(books={1342: BOOK}, error=error)
    catalog = MeasuredCatalog(inner)
    before = sample("bookreviews_catalog_requests_total", operation="get_book", outcome=outcome)
    timed = sample("bookreviews_catalog_request_duration_seconds_count", operation="get_book")

    if error is None:
        assert await catalog.get_book(1342) == BOOK
    else:
        with pytest.raises(type(error)):
            await catalog.get_book(1342)

    labels = {"operation": "get_book", "outcome": outcome}
    assert sample("bookreviews_catalog_requests_total", **labels) == before + 1
    assert sample("bookreviews_catalog_request_duration_seconds_count", operation="get_book") == (
        timed + 1
    )


async def test_searches_are_measured_too() -> None:
    before = sample("bookreviews_catalog_requests_total", operation="search", outcome="ok")

    await MeasuredCatalog(FakeCatalog()).search("austen")

    assert sample("bookreviews_catalog_requests_total", operation="search", outcome="ok") == (
        before + 1
    )


async def test_cache_hits_and_misses_are_counted() -> None:
    catalog = CachedCatalog(FakeCatalog(books={1342: BOOK}), ttl=60, max_books=10)
    hits, misses = (
        sample("bookreviews_catalog_cache_lookups_total", result=r) for r in ("hit", "miss")
    )

    await catalog.get_book(1342)
    await catalog.get_book(1342)
    await catalog.get_book(1342)

    assert sample("bookreviews_catalog_cache_lookups_total", result="miss") == misses + 1
    assert sample("bookreviews_catalog_cache_lookups_total", result="hit") == hits + 2


async def test_the_circuit_state_and_rejections_are_exported() -> None:
    now = [0.0]
    inner = FakeCatalog(books={1342: BOOK}, error=CatalogTimeoutError("timed out"))
    options = ResilienceOptions(max_concurrency=1, queue_timeout=0.01, failure_threshold=2)
    catalog = ResilientCatalog(inner, options, clock=lambda: now[0])
    rejected = sample("bookreviews_catalog_rejections_total", reason="circuit_open")

    for _ in range(2):
        with pytest.raises(CatalogTimeoutError):
            await catalog.get_book(1342)
    assert sample("bookreviews_catalog_circuit_open") == 1
    with pytest.raises(CatalogCircuitOpenError):
        await catalog.get_book(1342)
    assert sample("bookreviews_catalog_rejections_total", reason="circuit_open") == rejected + 1

    now[0] += options.reset_timeout
    inner.error = None
    await catalog.get_book(1342)
    assert sample("bookreviews_catalog_circuit_open") == 0


async def test_saturation_is_counted() -> None:
    release = asyncio.Event()

    class SlowCatalog(FakeCatalog):
        async def get_book(self, book_id: int) -> Book:
            await release.wait()
            return BOOK

    catalog = ResilientCatalog(
        SlowCatalog(), ResilienceOptions(max_concurrency=1, queue_timeout=0.01)
    )
    busy = sample("bookreviews_catalog_rejections_total", reason="busy")
    running = asyncio.create_task(catalog.get_book(1342))
    await asyncio.sleep(0)

    with pytest.raises(CatalogBusyError):
        await catalog.get_book(1342)

    release.set()
    await running
    assert sample("bookreviews_catalog_rejections_total", reason="busy") == busy + 1


async def test_submissions_are_counted() -> None:
    service = ReviewService(FakeReviewRepository(), FakeCatalog(books={1342: BOOK}), FakeQueue())
    created, replayed = (
        sample("bookreviews_reviews_submitted_total", result=r) for r in ("created", "replayed")
    )

    await service.submit(1342, "A classic.", 9, "tests", idempotency_key="metrics-1")
    await service.submit(1342, "A classic.", 9, "tests", idempotency_key="metrics-1")

    assert sample("bookreviews_reviews_submitted_total", result="created") == created + 1
    assert sample("bookreviews_reviews_submitted_total", result="replayed") == replayed + 1


async def test_publish_failures_are_counted() -> None:
    queue = RabbitQueue("amqp://user:password@127.0.0.1:9/", Topology(queue="metrics.test"))
    before = sample("bookreviews_queue_publish_failures_total", queue="metrics.test")

    with pytest.raises(QueueUnavailableError):
        await queue.enqueue(uuid.uuid7())

    await queue.close()
    assert sample("bookreviews_queue_publish_failures_total", queue="metrics.test") == before + 1


async def test_enrichment_outcomes_are_counted() -> None:
    repository = FakeReviewRepository()
    review = pending()
    await repository.add(review)
    handler = MessageHandler(
        ReviewEnricher(repository, FakeCatalog(books={1342: BOOK})),
        Topology(),
        max_attempts=2,
        parking=FakeQueue(),
    )
    outcomes = ("completed", "skipped", "parked")
    before = {o: sample("bookreviews_enrichments_total", outcome=o) for o in outcomes}
    timed = sample("bookreviews_enrichment_duration_seconds_count")

    message = FakeMessage(f'{{"review_id": "{review.id}"}}'.encode())
    await handler(message)
    await handler(replace(message, acked=False))
    await handler(FakeMessage(b"not json"))

    assert {o: sample("bookreviews_enrichments_total", outcome=o) for o in outcomes} == {
        o: value + 1 for o, value in before.items()
    }
    assert sample("bookreviews_enrichment_duration_seconds_count") == timed + 3


async def test_retries_and_given_up_messages_are_counted() -> None:
    repository = FakeReviewRepository()
    review = pending()
    await repository.add(review)
    handler = MessageHandler(
        ReviewEnricher(repository, FakeCatalog(error=CatalogTimeoutError("timed out"))),
        Topology(),
        max_attempts=2,
        parking=FakeQueue(),
    )
    retried, gave_up = (
        sample("bookreviews_enrichments_total", outcome=o) for o in ("retried", "gave_up")
    )
    body = f'{{"review_id": "{review.id}"}}'.encode()

    await handler(FakeMessage(body))
    await handler(
        FakeMessage(
            body, {"x-death": [{"queue": "review.enrichment", "reason": "rejected", "count": 1}]}
        )
    )

    assert sample("bookreviews_enrichments_total", outcome="retried") == retried + 1
    assert sample("bookreviews_enrichments_total", outcome="gave_up") == gave_up + 1


async def test_the_sweeper_reports_its_work_and_the_backlog() -> None:
    repository = FakeReviewRepository()
    stale = pending(NOW - timedelta(hours=2))
    abandoned = pending(NOW - timedelta(days=2))
    completed = replace(pending(NOW - timedelta(days=3)), status=ReviewStatus.COMPLETED)
    await repository.add(stale)
    await repository.add(abandoned)
    await repository.add(completed, IdempotencyKey("tests", "old-key", "a" * 64))
    actions = ("requeued", "expired", "keys_forgotten")
    before = {a: sample("bookreviews_sweeper_actions_total", action=a) for a in actions}
    sweeper = Sweeper(
        repository,
        FakeQueue(),
        SweepPolicy(stale_after=600, deadline=86_400, idempotency_key_ttl=86_400),
        clock=lambda: NOW,
    )

    await sweeper.sweep()

    assert {a: sample("bookreviews_sweeper_actions_total", action=a) for a in actions} == {
        a: value + 1 for a, value in before.items()
    }
    assert sample("bookreviews_pending_reviews") == 1
    assert sample("bookreviews_oldest_pending_review_age_seconds") == 7200

    await repository.complete(stale.id, BOOK, NOW)
    await sweeper.sweep()

    assert sample("bookreviews_pending_reviews") == 0
    assert sample("bookreviews_oldest_pending_review_age_seconds") == 0
