import uuid
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from bookreviews.core.catalog import Book, BookCatalog, BookNotFoundError
from bookreviews.core.reviews import Review, ReviewStatus, utc_now


class EnrichmentRepository(Protocol):
    async def get(self, review_id: uuid.UUID) -> Review | None: ...

    async def complete(
        self,
        review_id: uuid.UUID,
        book: Book,
        at: datetime,
        expected_status: ReviewStatus = ReviewStatus.PENDING,
    ) -> bool: ...

    async def fail(self, review_id: uuid.UUID, at: datetime) -> bool: ...


class Outcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ReviewEnricher:
    def __init__(
        self,
        reviews: EnrichmentRepository,
        catalog: BookCatalog,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._reviews = reviews
        self._catalog = catalog
        self._clock = clock

    async def enrich(self, review_id: uuid.UUID) -> Outcome:
        review = await self._reviews.get(review_id)
        if review is None or review.status is not ReviewStatus.PENDING:
            return Outcome.SKIPPED
        try:
            book = await self._catalog.get_book(review.book_id)
        except BookNotFoundError:
            failed = await self._reviews.fail(review_id, self._clock())
            return Outcome.FAILED if failed else Outcome.SKIPPED
        completed = await self._reviews.complete(review_id, book, self._clock())
        return Outcome.COMPLETED if completed else Outcome.SKIPPED

    async def retry(self, review_id: uuid.UUID) -> Outcome:
        review = await self._reviews.get(review_id)
        if review is None or review.status is not ReviewStatus.FAILED:
            return Outcome.SKIPPED
        try:
            book = await self._catalog.get_book(review.book_id)
        except BookNotFoundError:
            return Outcome.FAILED
        completed = await self._reviews.complete(
            review_id, book, self._clock(), expected_status=ReviewStatus.FAILED
        )
        return Outcome.COMPLETED if completed else Outcome.SKIPPED
