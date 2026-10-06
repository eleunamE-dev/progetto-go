import itertools
from collections.abc import Sequence

from prometheus_client import Counter, Gauge, Histogram, disable_created_metrics

disable_created_metrics()

REQUEST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
CATALOG_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 45, 60)
CATALOG_OPERATIONS = ("search", "get_book")


def _from_zero[M: (Counter, Histogram)](metric: M, **known: Sequence[str]) -> M:
    for values in itertools.product(*known.values()):
        metric.labels(**dict(zip(known, values, strict=True)))
    return metric


http_requests = Counter(
    "bookreviews_http_requests",
    "HTTP requests answered by the API",
    ["method", "route", "status"],
)
http_request_duration = Histogram(
    "bookreviews_http_request_duration_seconds",
    "Time taken to answer HTTP requests",
    ["method", "route"],
    buckets=REQUEST_BUCKETS,
)
catalog_requests = _from_zero(
    Counter(
        "bookreviews_catalog_requests",
        "Calls made to the book catalog (Gutendex), by outcome",
        ["operation", "outcome"],
    ),
    operation=CATALOG_OPERATIONS,
    outcome=("ok", "not_found", "timeout", "unavailable", "error"),
)
catalog_request_duration = _from_zero(
    Histogram(
        "bookreviews_catalog_request_duration_seconds",
        "Duration of the calls made to the book catalog (Gutendex)",
        ["operation"],
        buckets=CATALOG_BUCKETS,
    ),
    operation=CATALOG_OPERATIONS,
)
catalog_rejections = _from_zero(
    Counter(
        "bookreviews_catalog_rejections",
        "Calls to the book catalog refused without trying, by reason",
        ["reason"],
    ),
    reason=("circuit_open", "busy"),
)
catalog_circuit_open = Gauge(
    "bookreviews_catalog_circuit_open",
    "1 while calls to the book catalog are suspended after repeated failures",
)
catalog_cache_lookups = _from_zero(
    Counter(
        "bookreviews_catalog_cache_lookups",
        "Book lookups in the in-memory catalog cache",
        ["result"],
    ),
    result=("hit", "miss"),
)
reviews_submitted = _from_zero(
    Counter(
        "bookreviews_reviews_submitted",
        "Reviews accepted by the API: created, or answered again for a repeated request",
        ["result"],
    ),
    result=("created", "replayed"),
)
queue_publish_failures = Counter(
    "bookreviews_queue_publish_failures",
    "Messages that could not be published to RabbitMQ",
    ["queue"],
)
enrichments = _from_zero(
    Counter(
        "bookreviews_enrichments",
        "Enrichment messages handled by the worker, by outcome",
        ["outcome"],
    ),
    outcome=("completed", "failed", "skipped", "retried", "gave_up", "parked"),
)
enrichment_duration = Histogram(
    "bookreviews_enrichment_duration_seconds",
    "Time taken to handle an enrichment message",
    buckets=CATALOG_BUCKETS,
)
sweeper_actions = _from_zero(
    Counter(
        "bookreviews_sweeper_actions",
        "Work done by the sweeper: reviews queued again or expired, idempotency keys forgotten",
        ["action"],
    ),
    action=("requeued", "expired", "keys_forgotten"),
)
pending_reviews = Gauge(
    "bookreviews_pending_reviews",
    "Reviews waiting for enrichment, as of the last sweep",
)
oldest_pending_review_age = Gauge(
    "bookreviews_oldest_pending_review_age_seconds",
    "Age of the oldest review waiting for enrichment, as of the last sweep",
)
