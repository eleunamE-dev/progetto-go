import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator

import aio_pika
import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bookreviews.adapters.database import (
    SqlReviewRepository,
    create_engine,
    create_sessions,
    upgrade_database,
)
from bookreviews.adapters.queue import Topology

_SAFE_NAME = re.compile(r"\w+")


async def _create_database(url: str) -> None:
    parsed = make_url(url)
    if parsed.database is None or not _SAFE_NAME.fullmatch(parsed.database):
        pytest.fail(f"TEST_DATABASE_URL needs a plain database name, got {parsed.database!r}")
    server = create_async_engine(parsed.set(database=""))
    try:
        async with server.begin() as connection:
            await connection.execute(
                text(f"CREATE DATABASE IF NOT EXISTS `{parsed.database}` CHARACTER SET utf8mb4")
            )
    finally:
        await server.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to run the database tests")
    asyncio.run(_create_database(url))
    upgrade_database(url)
    return url


@pytest.fixture(scope="session")
def rabbitmq_url() -> str:
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("set TEST_RABBITMQ_URL to run the RabbitMQ tests")
    return url


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_url)
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM reviews"))
        await connection.execute(text("DELETE FROM books"))
    await engine.dispose()


@pytest.fixture
def repository(engine: AsyncEngine) -> SqlReviewRepository:
    return SqlReviewRepository(create_sessions(engine))


@pytest.fixture
async def topology(rabbitmq_url: str) -> AsyncIterator[Topology]:
    topology = Topology(queue=f"test.enrichment.{uuid.uuid4().hex[:8]}", retry_delay=0.2)
    yield topology
    connection = await aio_pika.connect_robust(rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        await channel.queue_delete(topology.queue)
        await channel.queue_delete(topology.retry_queue)
        await channel.queue_delete(topology.parking_queue)
