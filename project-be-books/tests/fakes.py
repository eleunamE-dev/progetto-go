import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from bookreviews.core.catalog import Book, BookNotFoundError, SearchResult
from bookreviews.core.reviews import (
    Condition,
    IdempotencyKey,
    IdempotencyKeyTakenError,
    QueueUnavailableError,
    Review,
    ReviewStatus,
)


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
    queued_at: dict[uuid.UUID, datetime] = field(default_factory=dict)
    keys: dict[tuple[str, str], tuple[uuid.UUID, str, datetime]] = field(default_factory=dict)

    async def add(self, review: Review, idempotency_key: IdempotencyKey | None = None) -> None:
        if idempotency_key is not None:
            slot = (idempotency_key.owner, idempotency_key.value)
            if slot in self.keys:
                raise IdempotencyKeyTakenError(idempotency_key.value)
            self.keys[slot] = (review.id, idempotency_key.fingerprint, review.created_at)
        self.reviews[review.id] = review
        self.queued_at[review.id] = review.created_at

    async def get(self, review_id: uuid.UUID) -> Review | None:
        return self.reviews.get(review_id)

    async def find_by_idempotency_key(self, owner: str, key: str) -> tuple[Review, str] | None:
        found = self.keys.get((owner, key))
        if found is None or found[0] not in self.reviews:
            return None
        return self.reviews[found[0]], found[1]

    async def update(
        self,
        review_id: uuid.UUID,
        *,
        content: str,
        score: int,
        updated_at: datetime,
        expected_version: int | None = None,
        condition: Condition | None = None,
    ) -> Review | None:
        review = self.reviews.get(review_id)
        if review is None or (expected_version is not None and review.version != expected_version):
            return None
        if condition is not None and not condition(review):
            return None
        updated = replace(
            review,
            content=content,
            score=score,
            updated_at=updated_at,
            version=review.version + 1,
        )
        self.reviews[review_id] = updated
        return updated

    async def delete(
        self,
        review_id: uuid.UUID,
        expected_version: int | None = None,
        *,
        condition: Condition | None = None,
    ) -> bool:
        review = self.reviews.get(review_id)
        if review is None or (expected_version is not None and review.version != expected_version):
            return False
        if condition is not None and not condition(review):
            return False
        del self.reviews[review_id]
        self.queued_at.pop(review_id, None)
        self.keys = {slot: entry for slot, entry in self.keys.items() if entry[0] != review_id}
        return True

    async def complete(
        self,
        review_id: uuid.UUID,
        book: Book,
        at: datetime,
        expected_status: ReviewStatus = ReviewStatus.PENDING,
    ) -> bool:
        return self._finish(review_id, ReviewStatus.COMPLETED, book, expected_status)

    async def fail(self, review_id: uuid.UUID, at: datetime) -> bool:
        return self._finish(review_id, ReviewStatus.FAILED, None)

    async def stale_pending(self, *, queued_before: datetime, limit: int) -> list[uuid.UUID]:
        stale = sorted(
            (queued_at, review_id)
            for review_id, queued_at in self.queued_at.items()
            if self.reviews[review_id].status is ReviewStatus.PENDING and queued_at < queued_before
        )
        return [review_id for _, review_id in stale][:limit]

    async def mark_queued(self, review_ids: Sequence[uuid.UUID], at: datetime) -> None:
        for review_id in review_ids:
            self.queued_at[review_id] = at

    async def expire_pending(self, *, created_before: datetime, at: datetime) -> int:
        expired = [
            review_id
            for review_id, review in self.reviews.items()
            if review.status is ReviewStatus.PENDING and review.created_at < created_before
        ]
        for review_id in expired:
            self._finish(review_id, ReviewStatus.FAILED, None)
        return len(expired)

    async def forget_idempotency_keys(self, *, created_before: datetime) -> int:
        old = [slot for slot, entry in self.keys.items() if entry[2] < created_before]
        for slot in old:
            del self.keys[slot]
        return len(old)

    async def pending_summary(self) -> tuple[int, datetime | None]:
        pending = [r.created_at for r in self.reviews.values() if r.status is ReviewStatus.PENDING]
        return len(pending), min(pending, default=None)

    def _finish(
        self,
        review_id: uuid.UUID,
        status: ReviewStatus,
        book: Book | None,
        expected_status: ReviewStatus = ReviewStatus.PENDING,
    ) -> bool:
        review = self.reviews.get(review_id)
        if review is None or review.status is not expected_status:
            return False
        self.reviews[review_id] = replace(
            review, status=status, book=book, version=review.version + 1
        )
        return True


@dataclass
class FakeQueue:
    enqueued: list[uuid.UUID] = field(default_factory=list)
    parked: list[tuple[bytes, str]] = field(default_factory=list)
    accepted: int | None = None

    async def enqueue(self, review_id: uuid.UUID) -> None:
        if self.accepted is not None and len(self.enqueued) >= self.accepted:
            raise QueueUnavailableError("RabbitMQ is down")
        self.enqueued.append(review_id)

    async def park(self, body: bytes, reason: str) -> None:
        if self.accepted == 0:
            raise QueueUnavailableError("RabbitMQ is down")
        self.parked.append((body, reason))


@dataclass
class FakeMessage:
    body: bytes
    headers: dict[str, object] = field(default_factory=dict)
    acked: bool = False
    rejected: bool = False

    async def ack(self) -> None:
        self.acked = True

    async def reject(self, requeue: bool = False) -> None:
        assert not requeue
        self.rejected = True
