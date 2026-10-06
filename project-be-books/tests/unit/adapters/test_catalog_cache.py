from dataclasses import dataclass

import pytest

from bookreviews.adapters.catalog import CachedCatalog
from bookreviews.core.catalog import Book, BookNotFoundError, SearchResult
from tests.fakes import FakeCatalog

PRIDE_AND_PREJUDICE = Book(id=1342, title="Pride and Prejudice")
TALE_OF_TWO_CITIES = Book(id=98, title="A Tale of Two Cities")
MOBY_DICK = Book(id=2701, title="Moby Dick")
TTL = 60.0


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def inner() -> FakeCatalog:
    books = (PRIDE_AND_PREJUDICE, TALE_OF_TWO_CITIES, MOBY_DICK)
    return FakeCatalog(
        result=SearchResult(total=2, books=books[:2], has_next=False),
        books={book.id: book for book in books},
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cache(inner: FakeCatalog, clock: Clock) -> CachedCatalog:
    return CachedCatalog(inner, ttl=TTL, max_books=2, clock=clock)


async def test_search_is_not_cached(cache: CachedCatalog, inner: FakeCatalog) -> None:
    await cache.search("austen")
    await cache.search("austen")
    await cache.search("austen", page=2)

    assert inner.searches == [("austen", 1), ("austen", 1), ("austen", 2)]


async def test_books_found_by_a_search_need_no_lookup(
    cache: CachedCatalog, inner: FakeCatalog
) -> None:
    await cache.search("classics")

    assert await cache.get_book(1342) == PRIDE_AND_PREJUDICE
    assert inner.book_requests == []


async def test_books_are_looked_up_once(cache: CachedCatalog, inner: FakeCatalog) -> None:
    assert await cache.get_book(2701) == MOBY_DICK
    assert await cache.get_book(2701) == MOBY_DICK

    assert inner.book_requests == [2701]


async def test_books_expire(cache: CachedCatalog, inner: FakeCatalog, clock: Clock) -> None:
    await cache.get_book(2701)
    clock.now += TTL

    await cache.get_book(2701)

    assert inner.book_requests == [2701, 2701]


async def test_least_recently_used_books_are_evicted(
    cache: CachedCatalog, inner: FakeCatalog
) -> None:
    await cache.get_book(1342)
    await cache.get_book(98)
    await cache.get_book(1342)
    await cache.get_book(2701)

    await cache.get_book(1342)
    await cache.get_book(98)

    assert inner.book_requests == [1342, 98, 2701, 98]


async def test_missing_books_are_not_cached(cache: CachedCatalog, inner: FakeCatalog) -> None:
    for _ in range(2):
        with pytest.raises(BookNotFoundError):
            await cache.get_book(999)

    assert inner.book_requests == [999, 999]
