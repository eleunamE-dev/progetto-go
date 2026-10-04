import hashlib
import json
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
    version: int = 1
    book: Book | None = None


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    owner: str
    value: str
    fingerprint: str


type Condition = Callable[[Review], bool]


class ReviewNotFoundError(Exception):
    def __init__(self, review_id: uuid.UUID) -> None:
        super().__init__(f"review {review_id} not found")
        self.review_id = review_id


class ReviewForbiddenError(Exception):
    def __init__(self, review_id: uuid.UUID) -> None:
        super().__init__(f"review {review_id} belongs to another client")
        self.review_id = review_id


class PreconditionFailedError(Exception):
    def __init__(self, review_id: uuid.UUID) -> None:
        super().__init__(f"review {review_id} does not match the precondition")
        self.review_id = review_id


class IdempotencyKeyReusedError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(f"the idempotency key {key!r} was used for a different request")
        self.key = key


class IdempotencyKeyTakenError(Exception):
    pass


class QueueUnavailableError(Exception):
    pass


class ReviewRepository(Protocol):
    async def add(self, review: Review, idempotency_key: IdempotencyKey | None = None) -> None: ...

    async def get(self, review_id: uuid.UUID) -> Review | None: ...

    async def find_by_idempotency_key(self, owner: str, key: str) -> tuple[Review, str] | None: ...

    async def update(
        self,
        review_id: uuid.UUID,
        *,
        content: str,
        score: int,
        updated_at: datetime,
        expected_version: int | None = None,
    ) -> Review | None: ...

    async def delete(self, review_id: uuid.UUID, expected_version: int | None = None) -> bool: ...


class ReviewQueue(Protocol):
    async def enqueue(self, review_id: uuid.UUID) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def request_fingerprint(book_id: int, content: str, score: int) -> str:
    request = json.dumps({"book_id": book_id, "content": content, "score": score}, sort_keys=True)
    return hashlib.sha256(request.encode()).hexdigest()


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

    async def submit(
        self,
        book_id: int,
        content: str,
        score: int,
        owner: str,
        *,
        idempotency_key: str | None = None,
    ) -> Review:
        if idempotency_key is None:
            return await self._create(book_id, content, score, owner, None)
        key = IdempotencyKey(owner, idempotency_key, request_fingerprint(book_id, content, score))
        if (previous := await self._replay(key)) is not None:
            return previous
        try:
            return await self._create(book_id, content, score, owner, key)
        except IdempotencyKeyTakenError:
            if (previous := await self._replay(key)) is not None:
                return previous
            raise

    async def get(self, review_id: uuid.UUID) -> Review:
        review = await self._repository.get(review_id)
        if review is None:
            raise ReviewNotFoundError(review_id)
        return review

    async def update(
        self,
        review_id: uuid.UUID,
        content: str,
        score: int,
        client: str,
        *,
        condition: Condition | None = None,
    ) -> Review:
        expected = self._expected_version(await self._owned(review_id, client), condition)
        review = await self._repository.update(
            review_id,
            content=content,
            score=score,
            updated_at=self._clock(),
            expected_version=expected,
        )
        if review is None:
            raise await self._missed_write(review_id, expected)
        return review

    async def delete(
        self, review_id: uuid.UUID, client: str, *, condition: Condition | None = None
    ) -> None:
        expected = self._expected_version(await self._owned(review_id, client), condition)
        if not await self._repository.delete(review_id, expected_version=expected):
            raise await self._missed_write(review_id, expected)

    async def _create(
        self, book_id: int, content: str, score: int, owner: str, key: IdempotencyKey | None
    ) -> Review:
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
        await self._repository.add(review, key)
        try:
            await self._queue.enqueue(review.id)
        except QueueUnavailableError as exc:
            logger.warning(
                "review saved but not queued, the sweeper will queue it",
                extra={"review_id": str(review.id), "error": str(exc)},
            )
        return review

    async def _replay(self, key: IdempotencyKey) -> Review | None:
        found = await self._repository.find_by_idempotency_key(key.owner, key.value)
        if found is None:
            return None
        review, fingerprint = found
        if fingerprint != key.fingerprint:
            raise IdempotencyKeyReusedError(key.value)
        logger.info(
            "repeated request answered with its review", extra={"review_id": str(review.id)}
        )
        return review

    async def _owned(self, review_id: uuid.UUID, client: str) -> Review:
        review = await self.get(review_id)
        if review.owner != client:
            raise ReviewForbiddenError(review_id)
        return review

    @staticmethod
    def _expected_version(review: Review, condition: Condition | None) -> int | None:
        if condition is None:
            return None
        if not condition(review):
            raise PreconditionFailedError(review.id)
        return review.version

    async def _missed_write(self, review_id: uuid.UUID, expected_version: int | None) -> Exception:
        if expected_version is not None and await self._repository.get(review_id) is not None:
            return PreconditionFailedError(review_id)
        return ReviewNotFoundError(review_id)
