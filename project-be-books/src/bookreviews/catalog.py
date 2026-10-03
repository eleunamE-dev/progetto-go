import asyncio
import contextlib
import logging
import math
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger("bookreviews.catalog")


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    birth_year: int | None = None
    death_year: int | None = None


@dataclass(frozen=True, slots=True)
class Book:
    id: int
    title: str
    authors: tuple[Person, ...] = ()
    subjects: tuple[str, ...] = ()
    bookshelves: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    summaries: tuple[str, ...] = ()
    cover_url: str | None = None
    download_count: int = 0


@dataclass(frozen=True, slots=True)
class SearchResult:
    total: int
    books: tuple[Book, ...]
    has_next: bool


class CatalogError(Exception):
    pass


class BookNotFoundError(CatalogError):
    def __init__(self, book_id: int) -> None:
        super().__init__(f"book {book_id} not found")
        self.book_id = book_id


class PageOutOfRangeError(CatalogError):
    def __init__(self, page: int) -> None:
        super().__init__(f"page {page} out of range")
        self.page = page


class CatalogUnavailableError(CatalogError):
    pass


class CatalogTimeoutError(CatalogUnavailableError):
    pass


class CatalogBusyError(CatalogUnavailableError):
    pass


class CatalogCircuitOpenError(CatalogUnavailableError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"the catalog keeps failing, calls are suspended for {retry_after:.0f}s")
        self.retry_after = retry_after


class BookCatalog(Protocol):
    async def search(self, query: str, page: int = 1) -> SearchResult: ...

    async def get_book(self, book_id: int) -> Book: ...


class CachedCatalog:
    def __init__(
        self,
        inner: BookCatalog,
        *,
        ttl: float,
        max_books: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl
        self._max_books = max_books
        self._clock = clock
        self._books: OrderedDict[int, tuple[float, Book]] = OrderedDict()

    async def search(self, query: str, page: int = 1) -> SearchResult:
        result = await self._inner.search(query, page)
        for book in result.books:
            self._remember(book)
        return result

    async def get_book(self, book_id: int) -> Book:
        cached = self._books.get(book_id)
        if cached is not None and cached[0] > self._clock():
            self._books.move_to_end(book_id)
            return cached[1]
        book = await self._inner.get_book(book_id)
        self._remember(book)
        return book

    def _remember(self, book: Book) -> None:
        self._books[book.id] = (self._clock() + self._ttl, book)
        self._books.move_to_end(book.id)
        while len(self._books) > self._max_books:
            self._books.popitem(last=False)


@dataclass(frozen=True, slots=True)
class ResilienceOptions:
    max_concurrency: int = 8
    queue_timeout: float = 10
    failure_threshold: int = 5
    reset_timeout: float = 30


class ResilientCatalog:
    def __init__(
        self,
        inner: BookCatalog,
        options: ResilienceOptions,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._options = options
        self._slots = asyncio.Semaphore(options.max_concurrency)
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    async def search(self, query: str, page: int = 1) -> SearchResult:
        return await self._call(lambda: self._inner.search(query, page))

    async def get_book(self, book_id: int) -> Book:
        return await self._call(lambda: self._inner.get_book(book_id))

    async def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        probe = self._admit()
        try:
            async with self._slot():
                result = await operation()
        except CatalogBusyError:
            raise
        except CatalogUnavailableError:
            self._record_failure()
            raise
        except CatalogError:
            self._record_success()
            raise
        else:
            self._record_success()
            return result
        finally:
            if probe:
                self._probing = False

    def _admit(self) -> bool:
        if self._opened_at is None:
            return False
        remaining = self._opened_at + self._options.reset_timeout - self._clock()
        if remaining > 0 or self._probing:
            raise CatalogCircuitOpenError(math.ceil(max(remaining, 1)))
        self._probing = True
        return True

    @contextlib.asynccontextmanager
    async def _slot(self) -> AsyncIterator[None]:
        try:
            async with asyncio.timeout(self._options.queue_timeout):
                await self._slots.acquire()
        except TimeoutError:
            raise CatalogBusyError("too many requests to the catalog are in progress") from None
        try:
            yield
        finally:
            self._slots.release()

    def _record_failure(self) -> None:
        self._failures += 1
        if self._opened_at is not None or self._failures >= self._options.failure_threshold:
            self._opened_at = self._clock()
            logger.warning(
                "catalog circuit opened",
                extra={"failures": self._failures, "reset_timeout": self._options.reset_timeout},
            )

    def _record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("catalog circuit closed")
        self._failures = 0
        self._opened_at = None
