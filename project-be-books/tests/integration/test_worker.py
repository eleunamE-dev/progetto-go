import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import aio_pika
import pytest
from pydantic import SecretStr

from bookreviews.catalog import Book, BookCatalog, CatalogUnavailableError, Person
from bookreviews.config import Settings
from bookreviews.database import SqlReviewRepository
from bookreviews.logs import request_id_var
from bookreviews.queue import RabbitQueue, Topology
from bookreviews.review_service import Review, ReviewStatus
from bookreviews.worker import WorkerOptions, consume, serve
from tests.conftest import LogRecords
from tests.fakes import FakeCatalog

pytestmark = pytest.mark.integration

PRIDE_AND_PREJUDICE = Book(
    id=1342,
    title="Pride and Prejudice",
    authors=(Person("Austen, Jane", 1775, 1817),),
    languages=("en",),
    cover_url="https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
    download_count=190246,
)


@dataclass
class UnreliableCatalog(FakeCatalog):
    failures: int = 0

    async def get_book(self, book_id: int) -> Book:
        if len(self.book_requests) < self.failures:
            self.book_requests.append(book_id)
            raise CatalogUnavailableError("Gutendex GET /books/1342/ returned status 503")
        return await super().get_book(book_id)


@asynccontextmanager
async def running_worker(
    rabbitmq_url: str,
    repository: SqlReviewRepository,
    catalog: BookCatalog,
    options: WorkerOptions,
) -> AsyncIterator[None]:
    stop = asyncio.Event()
    worker = asyncio.create_task(consume(rabbitmq_url, repository, catalog, stop, options))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=10)


async def wait_for_status(
    repository: SqlReviewRepository, review_id: uuid.UUID, status: ReviewStatus
) -> Review:
    async with asyncio.timeout(10):
        while True:
            review = await repository.get(review_id)
            if review is not None and review.status is status:
                return review
            await asyncio.sleep(0.05)


async def pending_review(repository: SqlReviewRepository, created_at: datetime) -> Review:
    review = Review(
        id=uuid.uuid7(),
        book_id=1342,
        content="A classic.",
        score=9,
        status=ReviewStatus.PENDING,
        created_at=created_at,
        updated_at=created_at,
        owner="tests",
    )
    await repository.add(review)
    return review


@pytest.fixture
def options(topology: Topology) -> WorkerOptions:
    return WorkerOptions(topology=topology, max_attempts=3, sweep_interval=0.1, sweep_after=60)


@pytest.fixture
async def publisher(rabbitmq_url: str, topology: Topology) -> AsyncIterator[RabbitQueue]:
    queue = RabbitQueue(rabbitmq_url, topology)
    yield queue
    await queue.close()


async def test_queued_reviews_are_enriched(
    rabbitmq_url: str,
    repository: SqlReviewRepository,
    publisher: RabbitQueue,
    options: WorkerOptions,
    json_logs: LogRecords,
) -> None:
    review = await pending_review(repository, datetime.now(UTC))
    catalog = FakeCatalog(books={1342: PRIDE_AND_PREJUDICE})

    async with running_worker(rabbitmq_url, repository, catalog, options):
        token = request_id_var.set("req-42")
        try:
            await publisher.enqueue(review.id)
        finally:
            request_id_var.reset(token)
        enriched = await wait_for_status(repository, review.id, ReviewStatus.COMPLETED)

    assert enriched == replace(review, status=ReviewStatus.COMPLETED, book=PRIDE_AND_PREJUDICE)
    [record] = [r for r in json_logs() if r["msg"] == "review processed"]
    assert record["request_id"] == "req-42"
    assert record["outcome"] == "completed"


async def test_failed_attempts_are_retried(
    rabbitmq_url: str,
    repository: SqlReviewRepository,
    publisher: RabbitQueue,
    options: WorkerOptions,
) -> None:
    review = await pending_review(repository, datetime.now(UTC))
    catalog = UnreliableCatalog(books={1342: PRIDE_AND_PREJUDICE}, failures=2)

    async with running_worker(rabbitmq_url, repository, catalog, options):
        await publisher.enqueue(review.id)
        await wait_for_status(repository, review.id, ReviewStatus.COMPLETED)

    assert catalog.book_requests == [1342, 1342, 1342]


async def test_the_worker_stops_retrying_after_the_last_attempt(
    rabbitmq_url: str,
    repository: SqlReviewRepository,
    publisher: RabbitQueue,
    options: WorkerOptions,
    json_logs: LogRecords,
) -> None:
    review = await pending_review(repository, datetime.now(UTC))
    catalog = UnreliableCatalog(books={1342: PRIDE_AND_PREJUDICE}, failures=10)

    async with running_worker(rabbitmq_url, repository, catalog, options):
        await publisher.enqueue(review.id)
        async with asyncio.timeout(10):
            while True:
                if any(r["msg"] == "enrichment failed, left to the sweeper" for r in json_logs()):
                    break
                await asyncio.sleep(0.05)
        await asyncio.sleep(options.topology.retry_delay * 3)

    assert catalog.book_requests == [1342, 1342, 1342]
    still_pending = await repository.get(review.id)
    assert still_pending is not None
    assert still_pending.status is ReviewStatus.PENDING


async def test_reviews_of_books_gone_from_the_catalog_fail(
    rabbitmq_url: str,
    repository: SqlReviewRepository,
    publisher: RabbitQueue,
    options: WorkerOptions,
) -> None:
    review = await pending_review(repository, datetime.now(UTC))

    async with running_worker(rabbitmq_url, repository, FakeCatalog(), options):
        await publisher.enqueue(review.id)
        failed = await wait_for_status(repository, review.id, ReviewStatus.FAILED)

    assert failed.book is None


async def test_the_sweeper_queues_forgotten_reviews(
    rabbitmq_url: str,
    repository: SqlReviewRepository,
    options: WorkerOptions,
) -> None:
    forgotten = await pending_review(repository, datetime.now(UTC) - timedelta(hours=1))
    catalog = FakeCatalog(books={1342: PRIDE_AND_PREJUDICE})

    async with running_worker(rabbitmq_url, repository, catalog, options):
        await wait_for_status(repository, forgotten.id, ReviewStatus.COMPLETED)


async def test_the_sweeper_gives_up_on_reviews_pending_for_too_long(
    rabbitmq_url: str,
    repository: SqlReviewRepository,
    options: WorkerOptions,
) -> None:
    abandoned = await pending_review(repository, datetime.now(UTC) - timedelta(days=2))
    catalog = FakeCatalog(books={1342: PRIDE_AND_PREJUDICE})

    async with running_worker(rabbitmq_url, repository, catalog, options):
        await wait_for_status(repository, abandoned.id, ReviewStatus.FAILED)

    assert catalog.book_requests == []


async def test_malformed_messages_are_parked(
    rabbitmq_url: str,
    repository: SqlReviewRepository,
    options: WorkerOptions,
    json_logs: LogRecords,
) -> None:
    topology = options.topology
    connection = await aio_pika.connect_robust(rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        await topology.declare(channel)

        async with running_worker(rabbitmq_url, repository, FakeCatalog(), options):
            await channel.default_exchange.publish(
                aio_pika.Message(b"not json"), routing_key=topology.queue
            )
            parked = await channel.get_queue(topology.parking_queue)
            async with asyncio.timeout(10):
                while True:
                    message = await parked.get(no_ack=True, fail=False)
                    if message is not None:
                        break
                    await asyncio.sleep(0.05)

    assert message.body == b"not json"
    assert message.headers == {"x-parked-reason": "not an enrichment request: b'not json'"}
    [record] = [r for r in json_logs() if r["msg"] == "malformed message parked"]
    assert record["level"] == "ERROR"
    assert record["queue"] == topology.parking_queue


async def test_serve_starts_and_stops(
    database_url: str, rabbitmq_url: str, json_logs: LogRecords
) -> None:
    settings = Settings(database_url=SecretStr(database_url), rabbitmq_url=SecretStr(rabbitmq_url))
    stop = asyncio.Event()

    serving = asyncio.create_task(serve(settings, stop))
    async with asyncio.timeout(10):
        while True:
            if any(r["msg"] == "worker started" for r in json_logs()):
                break
            await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(serving, timeout=10)

    assert [r["msg"] for r in json_logs() if r["logger"] == "bookreviews.worker"] == [
        "worker started",
        "worker stopping",
        "worker stopped",
    ]
