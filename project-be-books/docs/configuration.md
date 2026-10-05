# Configuration

Commands in this guide run from the `project-be-books` directory.

Every setting has a default that reaches the Compose services from this machine. The defaults say
`127.0.0.1` rather than `localhost`. Compose publishes the ports on IPv4 only, and on Windows a
connection to `localhost` tries IPv6 first: every new connection would wait about 2 seconds.

| Variable | Default | |
|---|---|---|
| `LOG_LEVEL` | `INFO` | |
| `HTTP_HOST` | `0.0.0.0` | API |
| `HTTP_PORT` | `8080` | API |
| `HTTP_SHUTDOWN_TIMEOUT` | `15` | seconds left to running requests on shutdown |
| `HTTP_MAX_BODY_SIZE` | `65536` | bytes; a larger request body gets 413 |
| `METRICS_PORT` | `9100` | port of `/metrics`, and of the worker's `/healthz` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/HTTP endpoint of the trace collector; tracing is off while unset |
| `API_KEYS` | `{}` | JSON object from client names to key digests, see [Authentication](../README.md#authentication); empty means every write is refused |
| `API_DOCS_ENABLED` | `true` | serve `/docs`, `/redoc` and `/openapi.json` |
| `CORS_ALLOW_ORIGINS` | `[]` | JSON list of the origins allowed to call the API from a browser, e.g. `["https://shop.example.com"]` |
| `DATABASE_URL` | `mysql+aiomysql://app:app-password@127.0.0.1:3306/bookreviews` | the migrations need an account that can change the schema |
| `DATABASE_POOL_SIZE` | `5` | connections each process keeps open |
| `DATABASE_MAX_OVERFLOW` | `10` | extra connections opened under load |
| `DATABASE_POOL_TIMEOUT` | `10` | seconds a request waits for a free connection |
| `RABBITMQ_URL` | `amqp://user:password@127.0.0.1:5672/` | |
| `GUTENDEX_BASE_URL` | `https://gutendex.com` | |
| `GUTENDEX_TIMEOUT` | `60` | seconds |
| `GUTENDEX_MAX_CONCURRENCY` | `8` | calls to Gutendex in progress at the same time, per process |
| `GUTENDEX_QUEUE_TIMEOUT` | `10` | seconds a call waits for a free slot before a 503 |
| `GUTENDEX_FAILURE_THRESHOLD` | `5` | consecutive failures that suspend calls to Gutendex |
| `GUTENDEX_RESET_TIMEOUT` | `30` | seconds calls stay suspended before a probe |
| `CATALOG_CACHE_TTL` | `3600` | seconds a book stays in the in-memory cache |
| `CATALOG_CACHE_SIZE` | `10000` | books kept in the in-memory cache |
| `WORKER_CONCURRENCY` | `4` | messages processed at the same time |
| `ENRICHMENT_MAX_ATTEMPTS` | `5` | attempts before leaving a review to the sweeper |
| `ENRICHMENT_DEADLINE` | `86400` | seconds after which a pending review is marked `failed`; must exceed `SWEEP_AFTER` |
| `IDEMPOTENCY_KEY_TTL` | `86400` | seconds an `Idempotency-Key` is remembered (worker) |
| `SWEEP_INTERVAL` | `60` | seconds between two sweeps |
| `SWEEP_AFTER` | `600` | seconds after which a pending review is queued again |
| `WORKER_SHUTDOWN_TIMEOUT` | `15` | seconds left to running messages on shutdown |
