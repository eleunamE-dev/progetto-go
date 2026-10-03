import asyncio
import os
import re
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bookreviews.database import (
    SqlReviewRepository,
    create_engine,
    create_sessions,
    upgrade_database,
)

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


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_url)
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM reviews"))
    await engine.dispose()


@pytest.fixture
def repository(engine: AsyncEngine) -> SqlReviewRepository:
    return SqlReviewRepository(create_sessions(engine))
