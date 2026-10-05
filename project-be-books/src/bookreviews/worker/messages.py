import asyncio
import contextlib
import logging
import time
import uuid
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from bookreviews.adapters.queue import (
    REQUEST_ID_HEADER,
    Delivery,
    InvalidMessageError,
    Topology,
    attempt,
    decode,
)
from bookreviews.core.catalog import CatalogUnavailableError
from bookreviews.core.enrichment import ReviewEnricher
from bookreviews.core.reviews import QueueUnavailableError
from bookreviews.observability import metrics
from bookreviews.observability.logs import request_id_var

logger = logging.getLogger("bookreviews.enrichment")
EXPECTED_ERRORS = (CatalogUnavailableError, SQLAlchemyError, OSError)


class Parking(Protocol):
    async def park(self, body: bytes, reason: str) -> None: ...


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
        started = time.perf_counter()
        try:
            await self._handle(message)
        finally:
            metrics.enrichment_duration.observe(time.perf_counter() - started)
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
            metrics.enrichments.labels(outcome.value).inc()
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
            metrics.enrichments.labels("retried").inc()
            await message.reject(requeue=False)
            return
        metrics.enrichments.labels("parked").inc()
        logger.error(
            "malformed message parked",
            extra={"queue": self._topology.parking_queue, "reason": reason},
        )
        await message.ack()

    async def _retry_or_give_up(
        self, message: Delivery, number: int, context: dict[str, object]
    ) -> None:
        if number < self._max_attempts:
            metrics.enrichments.labels("retried").inc()
            logger.warning("enrichment failed, retrying later", extra=context)
            await message.reject(requeue=False)
        else:
            metrics.enrichments.labels("gave_up").inc()
            logger.error("enrichment failed, left to the sweeper", extra=context)
            await message.ack()
