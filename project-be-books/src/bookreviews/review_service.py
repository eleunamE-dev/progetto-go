import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from bookreviews.catalog import BookCatalog


class ReviewStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class Review:
    id: uuid.UUID
    book_id: int
    content: str
    score: int
    status: ReviewStatus
    created_at: datetime
    updated_at: datetime


class ReviewNotFoundError(Exception):
    def __init__(self, review_id: uuid.UUID) -> None:
        super().__init__(f"review {review_id} not found")
        self.review_id = review_id


class ReviewRepository(Protocol):
    async def add(self, review: Review) -> None: ...

    async def get(self, review_id: uuid.UUID) -> Review | None: ...

    async def update(
        self, review_id: uuid.UUID, *, content: str, score: int, updated_at: datetime
    ) -> Review | None: ...

    async def delete(self, review_id: uuid.UUID) -> bool: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReviewService:
    def __init__(
        self,
        repository: ReviewRepository,
        catalog: BookCatalog,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._clock = clock

    async def submit(self, book_id: int, content: str, score: int) -> Review:
        await self._catalog.get_book(book_id)
        now = self._clock()
        review = Review(
            id=uuid.uuid7(),
            book_id=book_id,
            content=content,
            score=score,
            status=ReviewStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        await self._repository.add(review)
        return review

    async def get(self, review_id: uuid.UUID) -> Review:
        review = await self._repository.get(review_id)
        if review is None:
            raise ReviewNotFoundError(review_id)
        return review

    async def update(self, review_id: uuid.UUID, content: str, score: int) -> Review:
        review = await self._repository.update(
            review_id, content=content, score=score, updated_at=self._clock()
        )
        if review is None:
            raise ReviewNotFoundError(review_id)
        return review

    async def delete(self, review_id: uuid.UUID) -> None:
        if not await self._repository.delete(review_id):
            raise ReviewNotFoundError(review_id)
