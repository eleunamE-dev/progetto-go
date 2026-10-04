import io
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from bookreviews.app import create_app
from bookreviews.auth import key_digest
from bookreviews.config import Settings
from bookreviews.logs import JsonFormatter

type LogRecords = Callable[[], list[dict[str, Any]]]

CLIENT = "tests"
API_KEY = "test-key"
OTHER_CLIENT = "other-app"
OTHER_API_KEY = "other-key"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_keys={CLIENT: [key_digest(API_KEY)], OTHER_CLIENT: [key_digest(OTHER_API_KEY)]}
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": API_KEY}
    ) as client:
        yield client


@pytest.fixture
async def anonymous(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def json_logs() -> Iterator[LogRecords]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("bookreviews")
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    def records() -> list[dict[str, Any]]:
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
