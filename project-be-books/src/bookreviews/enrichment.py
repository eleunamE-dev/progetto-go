import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from bookreviews.catalog import Book, BookCatalog, BookNotFoundError, CatalogUnavailableError
from bookreviews.logs import request_id_var
from bookreviews.queue import (
    REQUEST_ID_HEADER,
    Delivery,
    InvalidMessageError,
    Topology,
    attempt,
    decode,
)
from bookreviews.review_service import (
    QueueUnavailableError,
    Review,
    ReviewQueue,
    ReviewStatus,
    utc_now,
)

logger = logging.getLogger("bookreviews.enrichment")

EXPECTED_ERRORS = (CatalogUnavailableError, SQLAlchemyError, OSError)
SWEEP_BATCH_SIZE = 100


class EnrichmentRepository(Protocol):
    async def get(self, review_id: uuid.UUID) -> Review | None: ...

    async def complete(self, review_id: uuid.UUID, book: Book, at: datetime) -> bool: ...

    async def fail(self, review_id: uuid.UUID, at: datetime) -> bool: ...

    async def stale_pending(self, *, queued_before: datetime, limit: int) -> list[uuid.UUID]: ...

    async def mark_queued(self, review_ids: Sequence[uuid.UUID], at: datetime) -> None: ...

    async def expire_pending(self, *, created_before: datetime, at: datetime) -> int: ...


class Parking(Protocol):
    async def park(self, body: bytes, reason: str) -> None: ...


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


class MessageHandler:
    def __init__(
        self,
        enricher: ReviewEnricher,
        topology: Topology,
        max_attempts: int,
        parking: Parking,
    ) -> None:
        self._enricher = enricher
        self._topology = topology
        self._max_attempts = max_attempts
        self._parking = parking
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def __call__(self, message: Delivery) -> None:
        self._active += 1
        self._idle.clear()
        request_id = message.headers.get(REQUEST_ID_HEADER)
        token = request_id_var.set(request_id if isinstance(request_id, str) else uuid.uuid4().hex)
        try:
            await self._handle(message)
        finally:
            request_id_var.reset(token)
            self._active -= 1
            if self._active == 0:
                self._idle.set()

    async def drain(self, grace_period: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), grace_period)

    async def _handle(self, message: Delivery) -> None:
        try:
            review_id = decode(message)
        except InvalidMessageError as invalid:
            await self._park(message, str(invalid))
            return

        number = attempt(message, self._topology)
        context = {"review_id": str(review_id), "attempt": number}
        try:
            outcome = await self._enricher.enrich(review_id)
        except EXPECTED_ERRORS as exc:
            await self._retry_or_give_up(message, number, context | {"error": str(exc)})
        except Exception:
            logger.exception("unexpected error while enriching a review", extra=context)
            await self._retry_or_give_up(message, number, context)
        else:
            logger.info("review processed", extra=context | {"outcome": outcome.value})
            await message.ack()

    async def _park(self, message: Delivery, reason: str) -> None:
        try:
            await self._parking.park(message.body, reason)
        except QueueUnavailableError as exc:
            logger.warning(
                "could not park a malformed message, retrying later",
                extra={"error": str(exc)},
            )
            await message.reject(requeue=False)
            return
        logger.error(
            "malformed message parked",
            extra={"queue": self._topology.parking_queue, "reason": reason},
        )
        await message.ack()

    async def _retry_or_give_up(
        self, message: Delivery, number: int, context: dict[str, object]
    ) -> None:
        if number < self._max_attempts:
            logger.warning("enrichment failed, retrying later", extra=context)
            await message.reject(requeue=False)
        else:
            logger.error("enrichment failed, left to the sweeper", extra=context)
            await message.ack()


class Sweeper:
    def __init__(
        self,
        reviews: EnrichmentRepository,
        queue: ReviewQueue,
        *,
        stale_after: float,
        deadline: float,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._reviews = reviews
        self._queue = queue
        self._stale_after = timedelta(seconds=stale_after)
        self._deadline = timedelta(seconds=deadline)
        self._clock = clock

    async def sweep(self) -> int:
        now = self._clock()
        expired = await self._reviews.expire_pending(created_before=now - self._deadline, at=now)
        if expired:
            logger.warning("gave up on reviews pending for too long", extra={"count": expired})
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
            logger.info("queued stale reviews again", extra={"count": len(queued)})
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
