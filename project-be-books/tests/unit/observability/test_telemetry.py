import logging
import uuid

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.aio_pika import AioPikaInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine

from bookreviews import wiring
from bookreviews.api.app import create_app
from bookreviews.api.dependencies import get_catalog, get_review_queue, get_review_repository
from bookreviews.config import Settings
from bookreviews.observability import telemetry
from tests.conftest import API_KEY, LogRecords
from tests.fakes import FakeCatalog, FakeQueue, FakeReviewRepository

ENDPOINT = HttpUrl("http://collector:4318")


def test_tracing_is_off_without_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(telemetry, "setup_tracing", calls.append)

    with telemetry.tracing(Settings(), "bookreviews-api"):
        pass

    assert calls == []


def test_tracing_is_set_up_and_flushed_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    class Provider:
        shut_down = False

        def shutdown(self) -> None:
            self.shut_down = True

    provider = Provider()
    services: list[str] = []

    def setup(service: str) -> Provider:
        services.append(service)
        return provider

    monkeypatch.setattr(telemetry, "setup_tracing", setup)

    with telemetry.tracing(Settings(otel_exporter_otlp_endpoint=ENDPOINT), "bookreviews-worker"):
        assert not provider.shut_down

    assert services == ["bookreviews-worker"]
    assert provider.shut_down


@pytest.mark.parametrize(
    ("variable", "service"), [(None, "bookreviews-api"), ("reviews-eu", "reviews-eu")]
)
def test_setup_tracing_installs_the_instrumentations(
    monkeypatch: pytest.MonkeyPatch, variable: str | None, service: str
) -> None:
    instrumented: list[str] = []
    installed: list[object] = []
    monkeypatch.setattr(
        HTTPXClientInstrumentor, "instrument", lambda _self: instrumented.append("httpx")
    )
    monkeypatch.setattr(
        AioPikaInstrumentor, "instrument", lambda _self: instrumented.append("aio-pika")
    )
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
    if variable is None:
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    else:
        monkeypatch.setenv("OTEL_SERVICE_NAME", variable)
    exporter = InMemorySpanExporter()

    provider = telemetry.setup_tracing("bookreviews-api", exporter)
    with provider.get_tracer("tests").start_as_current_span("work"):
        pass
    provider.force_flush()

    assert instrumented == ["httpx", "aio-pika"]
    assert installed == [provider]
    assert provider.resource.attributes["service.name"] == service
    assert [span.name for span in exporter.get_finished_spans()] == ["work"]
    provider.shutdown()


async def test_requests_are_traced_and_logs_carry_the_trace(
    spans: InMemorySpanExporter, settings: Settings, json_logs: LogRecords
) -> None:
    traced_settings = settings.model_copy(update={"otel_exporter_otlp_endpoint": ENDPOINT})
    app: FastAPI = create_app(traced_settings)
    repository = FakeReviewRepository()
    catalog, queue = FakeCatalog(), FakeQueue()
    app.dependency_overrides[get_review_repository] = lambda: repository
    app.dependency_overrides[get_catalog] = lambda: catalog
    app.dependency_overrides[get_review_queue] = lambda: queue
    review_id = uuid.uuid7()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": API_KEY}
    ) as client:
        response = await client.get(f"/review/{review_id}")
        await client.get("/healthz")

    assert response.status_code == 404
    [server_span] = [s for s in spans.get_finished_spans() if s.kind is trace.SpanKind.SERVER]
    assert server_span.name == "GET /review/{review_id}"
    assert server_span.attributes is not None
    assert server_span.attributes["http.route"] == "/review/{review_id}"
    trace_id = trace.format_trace_id(server_span.context.trace_id)
    [access] = [r for r in json_logs() if r["msg"] == "http request" and r["path"] != "/healthz"]
    assert access["trace_id"] == trace_id
    assert len(access["span_id"]) == 16


def test_logs_outside_a_trace_have_no_trace_id(json_logs: LogRecords) -> None:
    logging.getLogger("bookreviews.test").info("untraced")

    [record] = json_logs()
    assert "trace_id" not in record


async def test_the_engine_is_traced_only_when_tracing_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traced: list[AsyncEngine] = []
    monkeypatch.setattr(wiring, "trace_engine", traced.append)

    engine = wiring.build_engine(Settings(otel_exporter_otlp_endpoint=ENDPOINT))
    untraced = wiring.build_engine(Settings())

    assert traced == [engine]
    await engine.dispose()
    await untraced.dispose()
