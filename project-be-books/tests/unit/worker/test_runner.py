import asyncio

import aio_pika
import pytest

from bookreviews.adapters.queue import Topology
from bookreviews.config import Settings
from bookreviews.worker import runner
from bookreviews.worker.runner import WorkerOptions
from tests.conftest import LogRecords
from tests.fakes import FakeCatalog, FakeReviewRepository


def test_options_come_from_the_settings() -> None:
    settings = Settings(
        worker_concurrency=8,
        enrichment_max_attempts=3,
        enrichment_deadline=3600,
        idempotency_key_ttl=7200,
        sweep_interval=30,
        sweep_after=120,
        worker_shutdown_timeout=5,
    )

    assert WorkerOptions.from_settings(settings) == WorkerOptions(
        topology=Topology(),
        concurrency=8,
        max_attempts=3,
        deadline=3600,
        idempotency_key_ttl=7200,
        sweep_interval=30,
        sweep_after=120,
        shutdown_timeout=5,
    )


async def test_main_runs_the_worker_until_a_stop_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Settings, asyncio.Event]] = []

    async def fake_serve(settings: Settings, stop: asyncio.Event) -> None:
        calls.append((settings, stop))

    monkeypatch.setattr(runner, "serve", fake_serve)
    settings = Settings()

    await runner.main(settings)

    [(served, stop)] = calls
    assert served is settings
    assert not stop.is_set()


async def test_the_worker_waits_for_rabbitmq(
    monkeypatch: pytest.MonkeyPatch, json_logs: LogRecords
) -> None:
    connection = object()
    attempts = 0

    async def connect_robust(url: str, **_options: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 5:
            raise ConnectionRefusedError(111, "Connection refused")
        return connection

    monkeypatch.setattr(aio_pika, "connect_robust", connect_robust)
    monkeypatch.setattr(runner, "CONNECT_RETRY_DELAY", 0.001)
    monkeypatch.setattr(runner, "CONNECT_RETRY_LIMIT", 0.004)

    assert await runner.connect("amqp://rabbitmq", asyncio.Event()) is connection

    retries = [r for r in json_logs() if r["msg"] == "RabbitMQ not reachable, retrying"]
    assert [r["retry_in"] for r in retries] == [0.001, 0.002, 0.004, 0.004]
    assert retries[0]["error"] == "ConnectionRefusedError: [Errno 111] Connection refused"


async def test_a_worker_stopped_while_waiting_for_rabbitmq_exits(
    monkeypatch: pytest.MonkeyPatch, json_logs: LogRecords
) -> None:
    stop = asyncio.Event()

    async def connect_robust(url: str, **_options: object) -> object:
        stop.set()
        raise TimeoutError

    monkeypatch.setattr(aio_pika, "connect_robust", connect_robust)

    await runner.consume(
        "amqp://rabbitmq", FakeReviewRepository(), FakeCatalog(), stop, WorkerOptions()
    )

    messages = [r["msg"] for r in json_logs()]
    assert messages == [
        "RabbitMQ not reachable, retrying",
        "worker stopped before RabbitMQ was reachable",
    ]
