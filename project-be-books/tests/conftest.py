import io
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from bookreviews.app import create_app
from bookreviews.config import Settings
from bookreviews.logs import JsonFormatter

type LogRecords = Callable[[], list[dict[str, Any]]]


@pytest.fixture
def app() -> FastAPI:
    return create_app(Settings())


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
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
