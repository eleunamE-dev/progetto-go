import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from bookreviews.catalog import Book, BookCatalog

logger = logging.getLogger("bookreviews.reviews")


class ReviewStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Review:
    id: uuid.UUID
    book_id: int
    content: str
    score: int
    status: ReviewStatus
    created_at: datetime
    updated_at: datetime
    owner: str
    book: Book | None = None


class ReviewNotFoundError(Exception):
    def __init__(self, review_id: uuid.UUID) -> None:
        super().__init__(f"review {review_id} not found")
        self.review_id = review_id


class ReviewForbiddenError(Exception):
    def __init__(self, review_id: uuid.UUID) -> None:
        super().__init__(f"review {review_id} belongs to another client")
        self.review_id = review_id


class QueueUnavailableError(Exception):
    pass


class ReviewRepository(Protocol):
    async def add(self, review: Review) -> None: ...

    async def get(self, review_id: uuid.UUID) -> Review | None: ...

    async def update(
        self, review_id: uuid.UUID, *, content: str, score: int, updated_at: datetime
    ) -> Review | None: ...

    async def delete(self, review_id: uuid.UUID) -> bool: ...


class ReviewQueue(Protocol):
    async def enqueue(self, review_id: uuid.UUID) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReviewService:
    def __init__(
        self,
        repository: ReviewRepository,
        catalog: BookCatalog,
        queue: ReviewQueue,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._queue = queue
        self._clock = clock

    async def submit(self, book_id: int, content: str, score: int, owner: str) -> Review:
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
            owner=owner,
        )
        await self._repository.add(review)
        try:
            await self._queue.enqueue(review.id)
        except QueueUnavailableError as exc:
            logger.warning(
                "review saved but not queued, the sweeper will queue it",
                extra={"review_id": str(review.id), "error": str(exc)},
            )
        return review

    async def get(self, review_id: uuid.UUID) -> Review:
        review = await self._repository.get(review_id)
        if review is None:
            raise ReviewNotFoundError(review_id)
        return review

    async def update(self, review_id: uuid.UUID, content: str, score: int, client: str) -> Review:
        await self._check_owner(review_id, client)
        review = await self._repository.update(
            review_id, content=content, score=score, updated_at=self._clock()
        )
        if review is None:
            raise ReviewNotFoundError(review_id)
        return review

    async def delete(self, review_id: uuid.UUID, client: str) -> None:
        await self._check_owner(review_id, client)
        if not await self._repository.delete(review_id):
            raise ReviewNotFoundError(review_id)

    async def _check_owner(self, review_id: uuid.UUID, client: str) -> None:
        if (await self.get(review_id)).owner != client:
            raise ReviewForbiddenError(review_id)
