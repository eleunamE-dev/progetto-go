import contextlib
import os
from collections.abc import Iterator, MutableMapping
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.aio_pika import AioPikaInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from sqlalchemy.ext.asyncio import AsyncEngine

from bookreviews.config import Settings

UNTRACED_PATHS = frozenset({"/healthz", "/readyz"})


def setup_tracing(service: str, exporter: SpanExporter | None = None) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME", service)})
    )
    provider.add_span_processor(BatchSpanProcessor(exporter or OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    AioPikaInstrumentor().instrument()
    return provider


@contextlib.contextmanager
def tracing(settings: Settings, service: str) -> Iterator[None]:
    if not settings.tracing_enabled:
        yield
        return
    provider = setup_tracing(service)
    try:
        yield
    finally:
        provider.shutdown()


def untraced(scope: MutableMapping[str, Any]) -> bool:
    return scope.get("path") in UNTRACED_PATHS


def trace_engine(engine: AsyncEngine) -> None:
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, skip_dep_check=True)
