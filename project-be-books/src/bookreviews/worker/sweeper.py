import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from bookreviews.core.catalog import CatalogUnavailableError
from bookreviews.core.reviews import QueueUnavailableError, ReviewQueue, utc_now
from bookreviews.observability import metrics

logger = logging.getLogger("bookreviews.enrichment")
EXPECTED_ERRORS = (CatalogUnavailableError, SQLAlchemyError, OSError)
SWEEP_BATCH_SIZE = 100


class SweepRepository(Protocol):
    async def stale_pending(self, *, queued_before: datetime, limit: int) -> list[uuid.UUID]: ...

    async def mark_queued(self, review_ids: Sequence[uuid.UUID], at: datetime) -> None: ...

    async def expire_pending(self, *, created_before: datetime, at: datetime) -> int: ...

    async def forget_idempotency_keys(self, *, created_before: datetime) -> int: ...

    async def pending_summary(self) -> tuple[int, datetime | None]: ...


@dataclass(frozen=True, slots=True)
class SweepPolicy:
    stale_after: float = 600
    deadline: float = 86_400
    idempotency_key_ttl: float = 86_400


class Sweeper:
    def __init__(
        self,
        reviews: SweepRepository,
        queue: ReviewQueue,
        policy: SweepPolicy,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._reviews = reviews
        self._queue = queue
        self._stale_after = timedelta(seconds=policy.stale_after)
        self._deadline = timedelta(seconds=policy.deadline)
        self._key_ttl = timedelta(seconds=policy.idempotency_key_ttl)
        self._clock = clock

    async def sweep(self) -> int:
        now = self._clock()
        expired = await self._reviews.expire_pending(created_before=now - self._deadline, at=now)
        if expired:
            metrics.sweeper_actions.labels("expired").inc(expired)
            logger.warning("gave up on reviews pending for too long", extra={"count": expired})
        forgotten = await self._reviews.forget_idempotency_keys(created_before=now - self._key_ttl)
        if forgotten:
            metrics.sweeper_actions.labels("keys_forgotten").inc(forgotten)
            logger.info("forgot old idempotency keys", extra={"count": forgotten})
        stale = await self._reviews.stale_pending(
            queued_before=now - self._stale_after, limit=SWEEP_BATCH_SIZE
        )
        queued: list[uuid.UUID] = []
        for review_id in stale:
            try:
                await self._queue.enqueue(review_id)
            except QueueUnavailableError as exc:
                logger.warning(
                    "sweep interrupted, the queue is unavailable", extra={"error": str(exc)}
                )
                break
            queued.append(review_id)
        await self._reviews.mark_queued(queued, now)
        if queued:
            metrics.sweeper_actions.labels("requeued").inc(len(queued))
            logger.info("queued stale reviews again", extra={"count": len(queued)})
        pending, oldest = await self._reviews.pending_summary()
        metrics.pending_reviews.set(pending)
        metrics.oldest_pending_review_age.set(
            0 if oldest is None else (now - oldest).total_seconds()
        )
        return len(queued)

    async def run(self, interval: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.sweep()
            except EXPECTED_ERRORS as exc:
                logger.warning("sweep failed", extra={"error": str(exc)})
            except Exception:
                logger.exception("unexpected error while sweeping")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
