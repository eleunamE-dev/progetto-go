import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime

from bookreviews.catalog import Book, BookNotFoundError, SearchResult
from bookreviews.review_service import Review


@dataclass
class FakeCatalog:
    result: SearchResult = field(default_factory=lambda: SearchResult(0, (), has_next=False))
    books: dict[int, Book] = field(default_factory=dict)
    error: Exception | None = None
    searches: list[tuple[str, int]] = field(default_factory=list)
    book_requests: list[int] = field(default_factory=list)

    async def search(self, query: str, page: int = 1) -> SearchResult:
        self.searches.append((query, page))
        if self.error is not None:
            raise self.error
        return self.result

    async def get_book(self, book_id: int) -> Book:
        self.book_requests.append(book_id)
        if self.error is not None:
            raise self.error
        try:
            return self.books[book_id]
        except KeyError:
            raise BookNotFoundError(book_id) from None


@dataclass
class FakeReviewRepository:
    reviews: dict[uuid.UUID, Review] = field(default_factory=dict)

    async def add(self, review: Review) -> None:
        self.reviews[review.id] = review

    async def get(self, review_id: uuid.UUID) -> Review | None:
        return self.reviews.get(review_id)

    async def update(
        self, review_id: uuid.UUID, *, content: str, score: int, updated_at: datetime
    ) -> Review | None:
        review = self.reviews.get(review_id)
        if review is None:
            return None
        updated = replace(review, content=content, score=score, updated_at=updated_at)
        self.reviews[review_id] = updated
        return updated

    async def delete(self, review_id: uuid.UUID) -> bool:
        return self.reviews.pop(review_id, None) is not None
