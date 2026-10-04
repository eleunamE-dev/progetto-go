import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.aio_pika import AioPikaInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.ext.asyncio import AsyncEngine

from bookreviews.catalog import Book
from bookreviews.database import SqlReviewRepository, create_sessions
from bookreviews.queue import RabbitQueue, Topology
from bookreviews.review_service import Review, ReviewStatus
from bookreviews.telemetry import trace_engine
from bookreviews.worker import WorkerOptions
from tests.conftest import LogRecords
from tests.fakes import FakeCatalog
from tests.integration.test_worker import running_worker, wait_for_status

pytestmark = pytest.mark.integration

tracer = trace.get_tracer("tests")


@pytest.fixture
def traced_engine(engine: AsyncEngine) -> Iterator[AsyncEngine]:
    trace_engine(engine)
    yield engine
    SQLAlchemyInstrumentor().uninstrument()


@pytest.fixture
def traced_rabbitmq() -> Iterator[None]:
    AioPikaInstrumentor().instrument()
    yield
    AioPikaInstrumentor().uninstrument()


def new_review() -> Review:
    now = datetime.now(UTC)
    return Review(
        id=uuid.uuid7(),
        book_id=1342,
        content="A classic.",
        score=9,
        status=ReviewStatus.PENDING,
        created_at=now,
        updated_at=now,
        owner="tests",
    )


async def test_database_queries_are_traced_on_sqlalchemy_2_1(
    spans: InMemorySpanExporter, traced_engine: AsyncEngine
) -> None:
    repository = SqlReviewRepository(create_sessions(traced_engine))
    review = new_review()

    with tracer.start_as_current_span("request") as parent:
        await repository.add(review)
        await repository.get(review.id)

    statements = [
        s for s in spans.get_finished_spans() if s.attributes and "db.statement" in s.attributes
    ]
    assert [s.name.split()[0] for s in statements][:2] == ["INSERT", "SELECT"]
    assert all(
        s.parent and s.parent.span_id == parent.get_span_context().span_id for s in statements
    )


async def test_a_review_is_traced_from_the_api_to_the_worker(
    spans: InMemorySpanExporter,
    traced_rabbitmq: None,
    rabbitmq_url: str,
    repository: SqlReviewRepository,
    topology: Topology,
    json_logs: LogRecords,
) -> None:
    review = new_review()
    await repository.add(review)
    catalog = FakeCatalog(books={1342: Book(id=1342, title="Pride and Prejudice")})
    publisher = RabbitQueue(rabbitmq_url, topology)
    options = WorkerOptions(topology=topology, sweep_interval=60, sweep_after=600)

    try:
        async with running_worker(rabbitmq_url, repository, catalog, options):
            with tracer.start_as_current_span("POST /review") as request_span:
                await publisher.enqueue(review.id)
            await wait_for_status(repository, review.id, ReviewStatus.COMPLETED)
            await asyncio.sleep(0.1)
    finally:
        await publisher.close()

    trace_id = request_span.get_span_context().trace_id
    kinds = {s.kind for s in spans.get_finished_spans() if s.context.trace_id == trace_id}
    assert {trace.SpanKind.PRODUCER, trace.SpanKind.CONSUMER} <= kinds
    [processed] = [r for r in json_logs() if r["msg"] == "review processed"]
    assert processed["trace_id"] == trace.format_trace_id(trace_id)
