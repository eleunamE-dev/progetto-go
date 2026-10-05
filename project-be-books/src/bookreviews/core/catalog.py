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


class CatalogBusyError(CatalogUnavailableError):
    pass


class CatalogCircuitOpenError(CatalogUnavailableError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"the catalog keeps failing, calls are suspended for {retry_after:.0f}s")
        self.retry_after = retry_after


class BookCatalog(Protocol):
    async def search(self, query: str, page: int = 1) -> SearchResult: ...

    async def get_book(self, book_id: int) -> Book: ...
