import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection
from aio_pika.exceptions import CONNECTION_EXCEPTIONS, AMQPException

from bookreviews import metrics
from bookreviews.logs import request_id_var
from bookreviews.review_service import QueueUnavailableError

CONNECT_TIMEOUT = 5.0
PUBLISH_TIMEOUT = 5.0
REQUEST_ID_HEADER = "request_id"


@dataclass(frozen=True, slots=True)
class Topology:
    queue: str = "review.enrichment"
    retry_delay: float = 30.0

    @property
    def retry_queue(self) -> str:
        return f"{self.queue}.retry"

    @property
    def parking_queue(self) -> str:
        return f"{self.queue}.parked"

    async def declare(self, channel: AbstractChannel) -> AbstractQueue:
        await channel.declare_queue(self.parking_queue, durable=True)
        await channel.declare_queue(
            self.retry_queue,
            durable=True,
            arguments={
                "x-message-ttl": round(self.retry_delay * 1000),
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": self.queue,
            },
        )
        return await channel.declare_queue(
            self.queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": self.retry_queue,
            },
        )


class Delivery(Protocol):
    @property
    def body(self) -> bytes: ...

    @property
    def headers(self) -> Mapping[str, object]: ...

    async def ack(self) -> None: ...

    async def reject(self, requeue: bool = False) -> None: ...


class InvalidMessageError(Exception):
    pass


def encode(review_id: uuid.UUID) -> aio_pika.Message:
    request_id = request_id_var.get()
    return aio_pika.Message(
        json.dumps({"review_id": str(review_id)}).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        message_id=str(review_id),
        headers={} if request_id is None else {REQUEST_ID_HEADER: request_id},
    )


def decode(message: Delivery) -> uuid.UUID:
    invalid = InvalidMessageError(f"not an enrichment request: {message.body[:200]!r}")
    try:
        data = json.loads(message.body)
    except ValueError as exc:
        raise invalid from exc
    value = data.get("review_id") if isinstance(data, dict) else None
    if not isinstance(value, str):
        raise invalid
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise invalid from exc


def attempt(message: Delivery, topology: Topology) -> int:
    deaths = message.headers.get("x-death")
    rejections = 0
    if isinstance(deaths, list):
        for death in deaths:
            if (
                isinstance(death, Mapping)
                and death.get("queue") == topology.queue
                and death.get("reason") == "rejected"
                and isinstance(count := death.get("count"), int)
            ):
                rejections += count
    return rejections + 1


class RabbitQueue:
    def __init__(self, url: str, topology: Topology | None = None) -> None:
        self._url = url
        self._topology = topology or Topology()
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._lock = asyncio.Lock()

    async def enqueue(self, review_id: uuid.UUID) -> None:
        await self._publish(encode(review_id), self._topology.queue)

    async def park(self, body: bytes, reason: str) -> None:
        message = aio_pika.Message(
            body,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers={"x-parked-reason": reason[:500]},
        )
        await self._publish(message, self._topology.parking_queue)

    async def _publish(self, message: aio_pika.Message, routing_key: str) -> None:
        try:
            channel = await self._open_channel()
            await channel.default_exchange.publish(
                message, routing_key=routing_key, timeout=PUBLISH_TIMEOUT
            )
        except (*CONNECTION_EXCEPTIONS, AMQPException, TimeoutError) as exc:
            metrics.queue_publish_failures.labels(routing_key).inc()
            raise QueueUnavailableError(f"could not publish to RabbitMQ: {exc!r}") from exc

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    async def _open_channel(self) -> AbstractChannel:
        async with self._lock:
            if self._connection is None or self._connection.is_closed:
                self._connection = await aio_pika.connect_robust(self._url, timeout=CONNECT_TIMEOUT)
                self._channel = None
            if self._channel is None or self._channel.is_closed:
                self._channel = await self._connection.channel()
                await self._topology.declare(self._channel)
            return self._channel
