import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


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
