import io
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

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
        api_keys={CLIENT: [key_digest(API_KEY)], OTHER_CLIENT: [key_digest(OTHER_API_KEY)]},
        metrics_port=0,
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


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


_SPANS = InMemorySpanExporter()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_SPANS))
        trace.set_tracer_provider(provider)
    _SPANS.clear()
    yield _SPANS
    _SPANS.clear()
