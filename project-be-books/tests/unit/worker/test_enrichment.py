import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from bookreviews.adapters.queue import Topology
from bookreviews.core.catalog import Book, CatalogTimeoutError, CatalogUnavailableError, Person
from bookreviews.core.enrichment import Outcome, ReviewEnricher
from bookreviews.core.reviews import IdempotencyKey, Review, ReviewStatus
from bookreviews.worker.messages import MessageHandler
from bookreviews.worker.sweeper import Sweeper, SweepPolicy
from tests.conftest import LogRecords, sample
from tests.fakes import FakeCatalog, FakeMessage, FakeQueue, FakeReviewRepository

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
PRIDE_AND_PREJUDICE = Book(
    id=1342,
    title="Pride and Prejudice",
    authors=(Person("Austen, Jane", 1775, 1817),),
    cover_url="https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
)
TOPOLOGY = Topology()
DEADLINE = 86_400
POLICY = SweepPolicy(stale_after=600, deadline=DEADLINE, idempotency_key_ttl=86_400)


def new_review(**changes: Any) -> Review:
    review = Review(
        id=uuid.uuid7(),
        book_id=1342,
        content="A classic.",
        score=9,
        status=ReviewStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        owner="tests",
    )
    return replace(review, **changes)


def message_for(review_id: uuid.UUID, *, rejections: int = 0, **headers: object) -> FakeMessage:
    if rejections:
        headers["x-death"] = [{"queue": TOPOLOGY.queue, "reason": "rejected", "count": rejections}]
    return FakeMessage(json.dumps({"review_id": str(review_id)}).encode(), headers)


@pytest.fixture
def repository() -> FakeReviewRepository:
    return FakeReviewRepository()


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog(books={1342: PRIDE_AND_PREJUDICE})


@pytest.fixture
def enricher(repository: FakeReviewRepository, catalog: FakeCatalog) -> ReviewEnricher:
    return ReviewEnricher(repository, catalog, clock=lambda: NOW)


@pytest.fixture
def parking() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def handler(enricher: ReviewEnricher, parking: FakeQueue) -> MessageHandler:
    return MessageHandler(enricher, TOPOLOGY, max_attempts=3, parking=parking)


async def test_enrich_completes_a_pending_review(
    enricher: ReviewEnricher, repository: FakeReviewRepository
) -> None:
    review = new_review()
    await repository.add(review)

    assert await enricher.enrich(review.id) is Outcome.COMPLETED
    assert repository.reviews[review.id] == replace(
        review, status=ReviewStatus.COMPLETED, book=PRIDE_AND_PREJUDICE, version=2
    )


async def test_enrich_skips_deleted_reviews(enricher: ReviewEnricher, catalog: FakeCatalog) -> None:
    assert await enricher.enrich(uuid.uuid7()) is Outcome.SKIPPED
    assert catalog.book_requests == []


async def test_enrich_skips_reviews_already_processed(
    enricher: ReviewEnricher, repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    review = new_review(status=ReviewStatus.COMPLETED)
    await repository.add(review)

    assert await enricher.enrich(review.id) is Outcome.SKIPPED
    assert catalog.book_requests == []


async def test_enrich_fails_reviews_of_books_gone_from_the_catalog(
    enricher: ReviewEnricher, repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    review = new_review()
    await repository.add(review)
    catalog.books.clear()

    assert await enricher.enrich(review.id) is Outcome.FAILED
    assert repository.reviews[review.id].status is ReviewStatus.FAILED


async def test_enrich_lets_catalog_outages_through(
    enricher: ReviewEnricher, repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    review = new_review()
    await repository.add(review)
    catalog.error = CatalogTimeoutError("Gutendex GET /books/1342/ timed out")

    with pytest.raises(CatalogTimeoutError):
        await enricher.enrich(review.id)
    assert repository.reviews[review.id].status is ReviewStatus.PENDING


async def test_handler_acknowledges_processed_messages(
    handler: MessageHandler, repository: FakeReviewRepository, json_logs: LogRecords
) -> None:
    review = new_review()
    await repository.add(review)
    message = message_for(review.id, request_id="req-42")

    await handler(message)

    assert message.acked
    assert not message.rejected
    assert repository.reviews[review.id].status is ReviewStatus.COMPLETED
    [record] = [r for r in json_logs() if r["msg"] == "review processed"]
    assert record | {"time": None} == {
        "time": None,
        "level": "INFO",
        "logger": "bookreviews.enrichment",
        "msg": "review processed",
        "request_id": "req-42",
        "review_id": str(review.id),
        "attempt": 1,
        "outcome": "completed",
    }


async def test_handler_gives_each_message_a_request_id(
    handler: MessageHandler, json_logs: LogRecords
) -> None:
    await handler(message_for(uuid.uuid7()))

    [record] = [r for r in json_logs() if r["msg"] == "review processed"]
    assert len(record["request_id"]) == 32


async def test_handler_retries_transient_failures(
    handler: MessageHandler,
    repository: FakeReviewRepository,
    catalog: FakeCatalog,
    json_logs: LogRecords,
) -> None:
    review = new_review()
    await repository.add(review)
    catalog.error = CatalogUnavailableError("Gutendex GET /books/1342/ returned status 503")
    message = message_for(review.id, rejections=1)

    await handler(message)

    assert message.rejected
    assert not message.acked
    [record] = [r for r in json_logs() if r["level"] == "WARNING"]
    assert record["msg"] == "enrichment failed, retrying later"
    assert record["attempt"] == 2
    assert record["error"] == "Gutendex GET /books/1342/ returned status 503"


async def test_handler_leaves_the_review_to_the_sweeper_after_the_last_attempt(
    handler: MessageHandler,
    repository: FakeReviewRepository,
    catalog: FakeCatalog,
    json_logs: LogRecords,
) -> None:
    review = new_review()
    await repository.add(review)
    catalog.error = CatalogTimeoutError("Gutendex GET /books/1342/ timed out")
    message = message_for(review.id, rejections=2)

    await handler(message)

    assert message.acked
    assert not message.rejected
    assert repository.reviews[review.id].status is ReviewStatus.PENDING
    assert [r["msg"] for r in json_logs() if r["level"] == "ERROR"] == [
        "enrichment failed, left to the sweeper"
    ]


async def test_handler_retries_unexpected_errors(
    handler: MessageHandler,
    repository: FakeReviewRepository,
    catalog: FakeCatalog,
    json_logs: LogRecords,
) -> None:
    review = new_review()
    await repository.add(review)
    catalog.error = RuntimeError("bug")
    message = message_for(review.id)

    await handler(message)

    assert message.rejected
    [record] = [r for r in json_logs() if r["msg"] == "unexpected error while enriching a review"]
    assert "RuntimeError: bug" in record["exception"]


async def test_handler_parks_malformed_messages(
    handler: MessageHandler, parking: FakeQueue, json_logs: LogRecords
) -> None:
    message = FakeMessage(b"not json")

    await handler(message)

    assert message.acked
    [(body, reason)] = parking.parked
    assert body == b"not json"
    assert reason.startswith("not an enrichment request")
    [record] = [r for r in json_logs() if r["level"] == "ERROR"]
    assert record["msg"] == "malformed message parked"
    assert record["queue"] == "review.enrichment.parked"


async def test_handler_retries_parking_later_when_rabbitmq_is_down(
    handler: MessageHandler, parking: FakeQueue
) -> None:
    parking.accepted = 0
    message = FakeMessage(b"not json")
    retried = sample("bookreviews_enrichments_total", outcome="retried")

    await handler(message)

    assert message.rejected
    assert not message.acked
    assert sample("bookreviews_enrichments_total", outcome="retried") == retried + 1


async def test_drain_waits_for_messages_in_flight(
    repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    release = asyncio.Event()

    class SlowCatalog(FakeCatalog):
        async def get_book(self, book_id: int) -> Book:
            await release.wait()
            return await super().get_book(book_id)

    slow = SlowCatalog(books=catalog.books)
    handler = MessageHandler(
        ReviewEnricher(repository, slow), TOPOLOGY, max_attempts=3, parking=FakeQueue()
    )
    review = new_review()
    await repository.add(review)
    message = message_for(review.id)
    in_flight = asyncio.create_task(handler(message))
    await asyncio.sleep(0)

    await handler.drain(grace_period=0.01)
    assert not message.acked

    release.set()
    await handler.drain(grace_period=1)
    assert message.acked
    await in_flight


async def test_sweep_queues_stale_pending_reviews_again(
    repository: FakeReviewRepository, json_logs: LogRecords
) -> None:
    old = NOW - timedelta(hours=1)
    first, second = new_review(created_at=old), new_review(created_at=old + timedelta(minutes=1))
    recent = new_review()
    done = new_review(created_at=old, status=ReviewStatus.COMPLETED)
    for review in (second, first, recent, done):
        await repository.add(review)
    queue = FakeQueue()
    sweeper = Sweeper(repository, queue, POLICY, clock=lambda: NOW)

    assert await sweeper.sweep() == 2

    assert queue.enqueued == [first.id, second.id]
    assert repository.queued_at[first.id] == NOW
    assert repository.queued_at[second.id] == NOW
    assert [r["count"] for r in json_logs() if r["msg"] == "queued stale reviews again"] == [2]


async def test_sweep_stops_when_the_queue_is_unavailable(
    repository: FakeReviewRepository,
) -> None:
    old = NOW - timedelta(hours=1)
    first, second = new_review(created_at=old), new_review(created_at=old + timedelta(minutes=1))
    await repository.add(first)
    await repository.add(second)
    sweeper = Sweeper(repository, FakeQueue(accepted=1), POLICY, clock=lambda: NOW)

    assert await sweeper.sweep() == 1

    assert repository.queued_at == {first.id: NOW, second.id: second.created_at}


async def test_sweeper_runs_until_stopped(repository: FakeReviewRepository) -> None:
    review = new_review(created_at=NOW - timedelta(hours=1))
    await repository.add(review)
    queued = asyncio.Event()

    class SignallingQueue(FakeQueue):
        async def enqueue(self, review_id: uuid.UUID) -> None:
            await super().enqueue(review_id)
            queued.set()

    queue = SignallingQueue()
    stop = asyncio.Event()
    sweeper = Sweeper(repository, queue, POLICY, clock=lambda: NOW)

    running = asyncio.create_task(sweeper.run(interval=0.01, stop=stop))
    await asyncio.wait_for(queued.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(running, timeout=1)

    assert queue.enqueued == [review.id]


async def test_sweeper_survives_database_errors(json_logs: LogRecords) -> None:
    calls = 0
    stop = asyncio.Event()

    class FlakyRepository(FakeReviewRepository):
        async def stale_pending(self, *, queued_before: datetime, limit: int) -> list[uuid.UUID]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("SELECT", {}, ConnectionError("database restarting"))
            stop.set()
            return []

    sweeper = Sweeper(FlakyRepository(), FakeQueue(), POLICY, clock=lambda: NOW)

    await asyncio.wait_for(sweeper.run(interval=0.01, stop=stop), timeout=1)

    assert calls == 2
    assert [r["msg"] for r in json_logs() if r["level"] == "WARNING"] == ["sweep failed"]


async def test_sweeper_survives_unexpected_errors(json_logs: LogRecords) -> None:
    calls = 0
    stop = asyncio.Event()

    class BrokenRepository(FakeReviewRepository):
        async def stale_pending(self, *, queued_before: datetime, limit: int) -> list[uuid.UUID]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Connection was not opened")
            stop.set()
            return []

    sweeper = Sweeper(BrokenRepository(), FakeQueue(), POLICY, clock=lambda: NOW)

    await asyncio.wait_for(sweeper.run(interval=0.01, stop=stop), timeout=1)

    assert calls == 2
    [record] = [r for r in json_logs() if r["msg"] == "unexpected error while sweeping"]
    assert "RuntimeError: Connection was not opened" in record["exception"]


async def test_sweep_gives_up_on_reviews_pending_for_too_long(
    repository: FakeReviewRepository, json_logs: LogRecords
) -> None:
    abandoned = new_review(created_at=NOW - timedelta(days=2))
    recent = new_review(created_at=NOW - timedelta(hours=1))
    await repository.add(abandoned)
    await repository.add(recent)
    queue = FakeQueue()
    sweeper = Sweeper(repository, queue, POLICY, clock=lambda: NOW)

    await sweeper.sweep()

    assert repository.reviews[abandoned.id].status is ReviewStatus.FAILED
    assert repository.reviews[abandoned.id].updated_at == NOW
    assert repository.reviews[recent.id].status is ReviewStatus.PENDING
    assert queue.enqueued == [recent.id]
    [record] = [r for r in json_logs() if r["msg"] == "gave up on reviews pending for too long"]
    assert record["count"] == 1


async def test_sweep_forgets_old_idempotency_keys(
    repository: FakeReviewRepository, json_logs: LogRecords
) -> None:
    old = new_review(created_at=NOW - timedelta(days=2), status=ReviewStatus.COMPLETED)
    recent = new_review(created_at=NOW - timedelta(hours=1), status=ReviewStatus.COMPLETED)
    await repository.add(old, IdempotencyKey("tests", "old-key", "a" * 64))
    await repository.add(recent, IdempotencyKey("tests", "new-key", "b" * 64))

    await Sweeper(repository, FakeQueue(), POLICY, clock=lambda: NOW).sweep()

    assert set(repository.keys) == {("tests", "new-key")}
    assert set(repository.reviews) == {old.id, recent.id}
    [record] = [r for r in json_logs() if r["msg"] == "forgot old idempotency keys"]
    assert record["count"] == 1


async def test_a_failed_review_can_be_enriched_again(
    enricher: ReviewEnricher, repository: FakeReviewRepository
) -> None:
    review = new_review(status=ReviewStatus.FAILED)
    await repository.add(review)

    assert await enricher.retry(review.id) is Outcome.COMPLETED

    assert repository.reviews[review.id] == replace(
        review, status=ReviewStatus.COMPLETED, book=PRIDE_AND_PREJUDICE, version=2
    )


async def test_a_retried_review_whose_book_is_still_missing_stays_failed(
    enricher: ReviewEnricher, repository: FakeReviewRepository
) -> None:
    review = new_review(book_id=999, status=ReviewStatus.FAILED)
    await repository.add(review)

    assert await enricher.retry(review.id) is Outcome.FAILED

    assert repository.reviews[review.id] == review


@pytest.mark.parametrize("status", [ReviewStatus.PENDING, ReviewStatus.COMPLETED])
async def test_only_failed_reviews_are_retried(
    enricher: ReviewEnricher,
    repository: FakeReviewRepository,
    catalog: FakeCatalog,
    status: ReviewStatus,
) -> None:
    review = new_review(status=status)
    await repository.add(review)

    assert await enricher.retry(review.id) is Outcome.SKIPPED
    assert await enricher.retry(uuid.uuid7()) is Outcome.SKIPPED

    assert repository.reviews[review.id] == review
    assert catalog.book_requests == []


async def test_a_retry_meeting_an_unavailable_catalog_changes_nothing(
    enricher: ReviewEnricher, repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    review = new_review(status=ReviewStatus.FAILED)
    await repository.add(review)
    catalog.error = CatalogUnavailableError("Gutendex GET /books/1342/ returned 503")

    with pytest.raises(CatalogUnavailableError):
        await enricher.retry(review.id)

    assert repository.reviews[review.id] == review


async def test_enriched_reviews_record_when_they_changed(
    repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    later = NOW + timedelta(minutes=5)
    enricher = ReviewEnricher(repository, catalog, clock=lambda: later)
    completed, failed, retried = (
        new_review(),
        new_review(book_id=999),
        new_review(status=ReviewStatus.FAILED),
    )
    for review in (completed, failed, retried):
        await repository.add(review)

    assert await enricher.enrich(completed.id) is Outcome.COMPLETED
    assert await enricher.enrich(failed.id) is Outcome.FAILED
    assert await enricher.retry(retried.id) is Outcome.COMPLETED

    for review in (completed, failed, retried):
        assert repository.reviews[review.id].updated_at == later


class DeletedMeanwhile(FakeReviewRepository):
    async def complete(
        self,
        review_id: uuid.UUID,
        book: Book,
        at: datetime,
        expected_status: ReviewStatus = ReviewStatus.PENDING,
    ) -> bool:
        await self.delete(review_id)
        return await super().complete(review_id, book, at, expected_status)

    async def fail(self, review_id: uuid.UUID, at: datetime) -> bool:
        await self.delete(review_id)
        return await super().fail(review_id, at)


async def test_reviews_deleted_while_the_book_was_fetched_are_skipped(
    catalog: FakeCatalog,
) -> None:
    repository = DeletedMeanwhile()
    enricher = ReviewEnricher(repository, catalog, clock=lambda: NOW)
    pending, missing_book, failed = (
        new_review(),
        new_review(book_id=999),
        new_review(status=ReviewStatus.FAILED),
    )
    for review in (pending, missing_book, failed):
        await repository.add(review)

    assert await enricher.enrich(pending.id) is Outcome.SKIPPED
    assert await enricher.enrich(missing_book.id) is Outcome.SKIPPED
    assert await enricher.retry(failed.id) is Outcome.SKIPPED


async def test_drain_waits_for_every_message_in_flight(repository: FakeReviewRepository) -> None:
    releases = {1342: asyncio.Event(), 98: asyncio.Event()}

    class SlowCatalog(FakeCatalog):
        async def get_book(self, book_id: int) -> Book:
            await releases[book_id].wait()
            return await super().get_book(book_id)

    books = {1342: PRIDE_AND_PREJUDICE, 98: Book(id=98, title="A Tale of Two Cities")}
    handler = MessageHandler(
        ReviewEnricher(repository, SlowCatalog(books=books), clock=lambda: NOW),
        TOPOLOGY,
        max_attempts=3,
        parking=FakeQueue(),
    )
    await asyncio.wait_for(handler.drain(grace_period=60), timeout=1)
    first, second = new_review(), new_review(book_id=98)
    for review in (first, second):
        await repository.add(review)
    messages = [message_for(first.id), message_for(second.id)]
    tasks = [asyncio.create_task(handler(message)) for message in messages]
    await asyncio.sleep(0)

    releases[1342].set()
    await asyncio.wait_for(tasks[0], timeout=1)
    draining = asyncio.create_task(handler.drain(grace_period=60))
    await asyncio.sleep(0.05)
    assert not draining.done()

    releases[98].set()
    await asyncio.wait_for(draining, timeout=1)
    assert all(message.acked for message in messages)
    await tasks[1]


async def test_a_sweep_queues_at_most_100_reviews(repository: FakeReviewRepository) -> None:
    for minute in range(101):
        await repository.add(new_review(created_at=NOW - timedelta(hours=2, minutes=minute)))
    queue = FakeQueue()
    sweeper = Sweeper(repository, queue, POLICY, clock=lambda: NOW)

    assert await sweeper.sweep() == 100
    assert await sweeper.sweep() == 1
    assert len(set(queue.enqueued)) == 101
