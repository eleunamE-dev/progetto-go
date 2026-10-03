import asyncio
from dataclasses import dataclass, field

import pytest

from bookreviews.catalog import (
    Book,
    BookNotFoundError,
    CatalogBusyError,
    CatalogCircuitOpenError,
    CatalogTimeoutError,
    ResilienceOptions,
    ResilientCatalog,
    SearchResult,
)
from tests.conftest import LogRecords
from tests.fakes import FakeCatalog

PRIDE_AND_PREJUDICE = Book(id=1342, title="Pride and Prejudice")
OPTIONS = ResilienceOptions(
    max_concurrency=2, queue_timeout=0.05, failure_threshold=3, reset_timeout=30
)


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def inner() -> FakeCatalog:
    return FakeCatalog(
        books={1342: PRIDE_AND_PREJUDICE},
        result=SearchResult(total=1, books=(PRIDE_AND_PREJUDICE,), has_next=False),
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def catalog(inner: FakeCatalog, clock: Clock) -> ResilientCatalog:
    return ResilientCatalog(inner, OPTIONS, clock)


async def fail(catalog: ResilientCatalog, times: int) -> None:
    for _ in range(times):
        with pytest.raises(CatalogTimeoutError):
            await catalog.get_book(1342)


async def test_answers_pass_through(catalog: ResilientCatalog) -> None:
    assert await catalog.get_book(1342) == PRIDE_AND_PREJUDICE
    assert (await catalog.search("austen")).total == 1


async def test_opens_after_consecutive_failures(
    catalog: ResilientCatalog, inner: FakeCatalog, json_logs: LogRecords
) -> None:
    inner.error = CatalogTimeoutError("Gutendex GET /books/1342/ timed out")
    await fail(catalog, OPTIONS.failure_threshold)

    with pytest.raises(CatalogCircuitOpenError) as excinfo:
        await catalog.search("austen")

    assert catalog.is_open
    assert excinfo.value.retry_after == 30
    assert len(inner.book_requests) + len(inner.searches) == OPTIONS.failure_threshold
    [record] = [r for r in json_logs() if r["msg"] == "catalog circuit opened"]
    assert record["failures"] == OPTIONS.failure_threshold


async def test_a_success_resets_the_failure_count(
    catalog: ResilientCatalog, inner: FakeCatalog
) -> None:
    inner.error = CatalogTimeoutError("timed out")
    await fail(catalog, OPTIONS.failure_threshold - 1)
    inner.error = None
    await catalog.get_book(1342)
    inner.error = CatalogTimeoutError("timed out")

    await fail(catalog, OPTIONS.failure_threshold - 1)

    assert not catalog.is_open


async def test_not_found_is_an_answer_not_a_failure(
    catalog: ResilientCatalog, inner: FakeCatalog
) -> None:
    for _ in range(OPTIONS.failure_threshold * 2):
        with pytest.raises(BookNotFoundError):
            await catalog.get_book(999)

    assert not catalog.is_open


async def test_retry_after_counts_down(
    catalog: ResilientCatalog, inner: FakeCatalog, clock: Clock
) -> None:
    inner.error = CatalogTimeoutError("timed out")
    await fail(catalog, OPTIONS.failure_threshold)
    clock.now += 20.5

    with pytest.raises(CatalogCircuitOpenError) as excinfo:
        await catalog.get_book(1342)

    assert excinfo.value.retry_after == 10


async def test_a_successful_probe_closes_the_circuit(
    catalog: ResilientCatalog, inner: FakeCatalog, clock: Clock, json_logs: LogRecords
) -> None:
    inner.error = CatalogTimeoutError("timed out")
    await fail(catalog, OPTIONS.failure_threshold)
    clock.now += OPTIONS.reset_timeout
    inner.error = None

    assert await catalog.get_book(1342) == PRIDE_AND_PREJUDICE

    assert not catalog.is_open
    assert any(r["msg"] == "catalog circuit closed" for r in json_logs())


async def test_a_failed_probe_opens_the_circuit_again(
    catalog: ResilientCatalog, inner: FakeCatalog, clock: Clock
) -> None:
    inner.error = CatalogTimeoutError("timed out")
    await fail(catalog, OPTIONS.failure_threshold)
    clock.now += OPTIONS.reset_timeout

    await fail(catalog, 1)

    with pytest.raises(CatalogCircuitOpenError) as excinfo:
        await catalog.get_book(1342)
    assert excinfo.value.retry_after == 30


@dataclass
class GatedCatalog(FakeCatalog):
    gate: asyncio.Event = field(default_factory=asyncio.Event)

    async def get_book(self, book_id: int) -> Book:
        await self.gate.wait()
        return await super().get_book(book_id)


async def test_only_one_probe_at_a_time(clock: Clock) -> None:
    gated = GatedCatalog(books={1342: PRIDE_AND_PREJUDICE})
    gated.gate.set()
    catalog = ResilientCatalog(gated, OPTIONS, clock)
    gated.error = CatalogTimeoutError("timed out")
    await fail(catalog, OPTIONS.failure_threshold)
    clock.now += OPTIONS.reset_timeout
    gated.error = None
    gated.gate.clear()

    probe = asyncio.create_task(catalog.get_book(1342))
    await asyncio.sleep(0)
    with pytest.raises(CatalogCircuitOpenError):
        await catalog.get_book(1342)

    gated.gate.set()
    assert await probe == PRIDE_AND_PREJUDICE
    assert await catalog.get_book(1342) == PRIDE_AND_PREJUDICE


async def test_limits_the_requests_in_flight(inner: FakeCatalog, clock: Clock) -> None:
    release = asyncio.Event()
    started = 0

    class SlowCatalog(FakeCatalog):
        async def get_book(self, book_id: int) -> Book:
            nonlocal started
            started += 1
            await release.wait()
            return await super().get_book(book_id)

    catalog = ResilientCatalog(SlowCatalog(books=inner.books), OPTIONS, clock)
    running = [asyncio.create_task(catalog.get_book(1342)) for _ in range(OPTIONS.max_concurrency)]
    await asyncio.sleep(0)

    with pytest.raises(CatalogBusyError):
        await catalog.get_book(1342)

    assert started == OPTIONS.max_concurrency
    release.set()
    assert await asyncio.gather(*running) == [PRIDE_AND_PREJUDICE] * OPTIONS.max_concurrency
    assert not catalog.is_open
