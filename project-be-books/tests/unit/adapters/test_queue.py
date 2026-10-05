import json
import uuid

import aio_pika
import pytest

from bookreviews.adapters.queue import (
    InvalidMessageError,
    RabbitQueue,
    Topology,
    attempt,
    decode,
    encode,
)
from bookreviews.core.reviews import QueueUnavailableError
from bookreviews.observability.logs import request_id_var
from tests.fakes import FakeMessage


def test_encode_builds_a_persistent_json_message() -> None:
    review_id = uuid.uuid7()
    token = request_id_var.set("req-42")
    try:
        message = encode(review_id)
    finally:
        request_id_var.reset(token)

    assert json.loads(message.body) == {"review_id": str(review_id)}
    assert message.content_type == "application/json"
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert message.message_id == str(review_id)
    assert message.headers == {"request_id": "req-42"}


def test_encode_outside_a_request() -> None:
    assert encode(uuid.uuid7()).headers == {}


def test_decode() -> None:
    review_id = uuid.uuid7()

    assert decode(FakeMessage(encode(review_id).body)) == review_id


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b"{}", b'{"review_id": 42}', b'{"review_id": "not a uuid"}'],
)
def test_decode_rejects_malformed_messages(body: bytes) -> None:
    with pytest.raises(InvalidMessageError):
        decode(FakeMessage(body))


@pytest.mark.parametrize(
    ("x_death", "expected"),
    [
        (None, 1),
        ([{"queue": "review.enrichment", "reason": "rejected", "count": 2}], 3),
        (
            [
                {"queue": "review.enrichment.retry", "reason": "expired", "count": 2},
                {"queue": "review.enrichment", "reason": "rejected", "count": 2},
            ],
            3,
        ),
        ([{"queue": "another.queue", "reason": "rejected", "count": 5}], 1),
        ("not a list", 1),
    ],
)
def test_attempt_counts_previous_rejections(x_death: object, expected: int) -> None:
    headers = {} if x_death is None else {"x-death": x_death}

    assert attempt(FakeMessage(b"{}", headers), Topology()) == expected


def test_queue_names() -> None:
    topology = Topology(queue="reviews")

    assert topology.retry_queue == "reviews.retry"
    assert topology.parking_queue == "reviews.parked"


async def test_enqueue_reports_an_unreachable_broker() -> None:
    queue = RabbitQueue("amqp://user:password@127.0.0.1:9/")

    with pytest.raises(QueueUnavailableError):
        await queue.enqueue(uuid.uuid7())

    await queue.close()


class ReconnectingConnection:
    is_closed = False

    async def channel(self) -> None:
        raise RuntimeError("Connection was not opened")

    async def close(self) -> None:
        pass


async def test_enqueue_reports_a_broker_that_is_reconnecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def connect_robust(*args: object, **kwargs: object) -> ReconnectingConnection:
        return ReconnectingConnection()

    monkeypatch.setattr(aio_pika, "connect_robust", connect_robust)
    queue = RabbitQueue("amqp://user:password@rabbitmq:5672/")

    with pytest.raises(QueueUnavailableError, match="Connection was not opened"):
        await queue.enqueue(uuid.uuid7())
