from prometheus_client import Counter, Gauge, Histogram, disable_created_metrics

disable_created_metrics()

REQUEST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
CATALOG_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 45, 60)

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
catalog_requests = Counter(
    "bookreviews_catalog_requests",
    "Calls made to the book catalog (Gutendex), by outcome",
    ["operation", "outcome"],
)
catalog_request_duration = Histogram(
    "bookreviews_catalog_request_duration_seconds",
    "Duration of the calls made to the book catalog (Gutendex)",
    ["operation"],
    buckets=CATALOG_BUCKETS,
)
catalog_rejections = Counter(
    "bookreviews_catalog_rejections",
    "Calls to the book catalog refused without trying, by reason",
    ["reason"],
)
catalog_circuit_open = Gauge(
    "bookreviews_catalog_circuit_open",
    "1 from the opening of the circuit until a call to the book catalog succeeds again",
)
catalog_cache_lookups = Counter(
    "bookreviews_catalog_cache_lookups",
    "Book lookups in the in-memory catalog cache",
    ["result"],
)
reviews_submitted = Counter(
    "bookreviews_reviews_submitted",
    "Reviews accepted by the API: created, or answered again for a repeated request",
    ["result"],
)
queue_publish_failures = Counter(
    "bookreviews_queue_publish_failures",
    "Messages that could not be published to RabbitMQ",
    ["queue"],
)
enrichments = Counter(
    "bookreviews_enrichments",
    "Enrichment messages handled by the worker, by outcome",
    ["outcome"],
)
enrichment_duration = Histogram(
    "bookreviews_enrichment_duration_seconds",
    "Time taken to handle an enrichment message",
    buckets=CATALOG_BUCKETS,
)
sweeper_actions = Counter(
    "bookreviews_sweeper_actions",
    "Work done by the sweeper: reviews queued again or expired, idempotency keys forgotten",
    ["action"],
)
pending_reviews = Gauge(
    "bookreviews_pending_reviews",
    "Reviews waiting for enrichment, as of the last sweep",
)
oldest_pending_review_age = Gauge(
    "bookreviews_oldest_pending_review_age_seconds",
    "Age of the oldest review waiting for enrichment, as of the last sweep",
)
