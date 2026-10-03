from dataclasses import dataclass, field

from bookreviews.catalog import Book, BookNotFoundError, SearchResult


@dataclass
class FakeCatalog:
    result: SearchResult = field(default_factory=lambda: SearchResult(0, (), has_next=False))
    books: dict[int, Book] = field(default_factory=dict)
    error: Exception | None = None
    searches: list[tuple[str, int]] = field(default_factory=list)

    async def search(self, query: str, page: int = 1) -> SearchResult:
        self.searches.append((query, page))
        if self.error is not None:
            raise self.error
        return self.result

    async def get_book(self, book_id: int) -> Book:
        if self.error is not None:
            raise self.error
        try:
            return self.books[book_id]
        except KeyError:
            raise BookNotFoundError(book_id) from None
