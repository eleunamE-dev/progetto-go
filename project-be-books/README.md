# Book reviews

A service to search the books of [Project Gutenberg](https://www.gutenberg.org), through the
[Gutendex](https://gutendex.com) API, and review them. A review is accepted right away; a worker
then adds the book's data (cover, authors, subjects, summary…) in the background.

Python 3.14, FastAPI, MariaDB, RabbitMQ. The original assignment is in [ASSIGNMENT.md](ASSIGNMENT.md).

- [Start here](#start-here)
- [Quick start](#quick-start)
- [A tour of the API](#a-tour-of-the-api)
- [Endpoints](#endpoints)
- [Retries and concurrent edits](#retries-and-concurrent-edits)
- [Authentication](#authentication)
- [How it works](#how-it-works)
- [Observability](#observability)
- [Deploying to Kubernetes](#deploying-to-kubernetes)
- [Operations](#operations)
- [Design notes](#design-notes)
- [Configuration](#configuration)
- [Development](#development)

## Start here

The [assignment](ASSIGNMENT.md) asks for five endpoints, data enriched asynchronously through a
public API, an easy way to run the service, and tests. With Docker, checking it takes a few
minutes:

1. **Run it.** `docker compose up --build --wait` starts everything, see [Quick start](#quick-start).
2. **Try it.** [A tour of the API](#a-tour-of-the-api) searches a book, reviews it, waits for the
   enrichment, then changes and deletes the review, with `curl`. Writes need the header
   `X-API-Key: local-dev-key`.
3. **Test it.** `make test-all` runs the unit and integration tests against MariaDB and RabbitMQ;
   `make e2e` checks a whole fresh stack from the outside.

| The assignment asks for | Where it is |
|---|---|
| `GET /book/search?q=` on a public API | Gutendex, through [gutendex.py](src/bookreviews/gutendex.py) and [books.py](src/bookreviews/books.py) |
| `POST /review`, checking the book on the API, the score and the text | [reviews.py](src/bookreviews/reviews.py): 422 naming the wrong field, otherwise 202 |
| a reference to follow the processing | the review's ID, in the `Location` header of the 202 |
| the enriched data saved asynchronously | RabbitMQ and the [worker](src/bookreviews/worker.py), see [How it works](#how-it-works) |
| `GET /review/{id}`: 202 while processing, 200 with the enriched data | [reviews.py](src/bookreviews/reviews.py) |
| `PUT` and `DELETE /review/{id}` | [reviews.py](src/bookreviews/reviews.py) |
| tests, static analysis, coding standards | pytest, ruff, mypy in strict mode, pre-commit hooks, GitHub Actions |

**Beyond the assignment.** Each addition answers a question that a service in production faces:
- *Who may write?* API keys, and reviews that belong to the client that wrote them, see
  [Authentication](#authentication).
- *What if a client sends a request twice, or two clients edit at once?* Idempotency keys and
  entity tags, see [Retries and concurrent edits](#retries-and-concurrent-edits).
- *What if Gutendex is slow or down?* Timeouts, a cache, a circuit breaker, retries and a sweeper,
  see [Gutendex is slow](#gutendex-is-slow) and [Asynchronous enrichment](#asynchronous-enrichment).
- *How do we know it works?* Metrics, traces, alerts and a runbook, see
  [Observability](#observability) and [Operations](#operations).
- *How does it run for real?* Kubernetes manifests, verified on a local cluster, see
  [Deploying to Kubernetes](#deploying-to-kubernetes).

The pull requests of the repository follow the same path, one step at a time, from the first
endpoint to operations.

## Quick start

You only need Docker with Compose.

```bash
docker compose up --build --wait
```

This starts MariaDB, RabbitMQ, a one-shot job that applies the database migrations, the API and
the enrichment worker. Then:

- API: http://localhost:8080, with interactive documentation at http://localhost:8080/docs
- RabbitMQ management: http://localhost:15672 (`user` / `password`)

**Writing needs an API key.** `POST`, `PUT` and `DELETE` on `/review` need the header
`X-API-Key: local-dev-key` (the key of the `local-dev` client); without it the answer is
`401 Unauthorized`. Searching and reading need no key. See [Authentication](#authentication).

The ports are published on 127.0.0.1 only, so the development passwords stay on your machine.
`docker compose down` stops everything; add `-v` to delete the database volume too.

The database accounts are created by [deploy/mariadb/users.sql](deploy/mariadb/users.sql) the first
time the volume is initialised. On a volume created before that script existed, apply it once:

```bash
docker compose exec -T db mariadb -uroot -prootpassword < deploy/mariadb/users.sql
```

## A tour of the API

Find a book:

```bash
curl "localhost:8080/book/search?q=pride%20prejudice"
```

```json
{
  "count": 6,
  "page": 1,
  "next_page": null,
  "results": [
    {
      "id": 1342,
      "title": "Pride and Prejudice",
      "authors": ["Austen, Jane"],
      "languages": ["en"],
      "cover_url": "https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
      "download_count": 190246
    }
  ]
}
```

The first time, this can take up to a minute: Gutendex needs 15–60 s for a search its CDN hasn't
cached yet. After 60 s the API gives up with `504 Gateway Timeout`, but Gutendex finishes the work
and caches the result, so the same search a few minutes later answers at once. See
[Gutendex is slow](#gutendex-is-slow).

Review it, using the `id` of the book. Writing needs an API key; the answer is `202 Accepted` with
the address of the review:

```bash
curl -i -X POST localhost:8080/review \
  -H "X-API-Key: local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"id": 1342, "review": "A classic.", "score": 9}'
```

```http
HTTP/1.1 202 Accepted
location: /review/01a1023f-006d-716b-abee-f6d3e9210156
content-type: application/json

{"id": "01a1023f-006d-716b-abee-f6d3e9210156", "status": "pending", "book_id": 1342,
 "review": "A classic.", "score": 9, "created_at": "2026-10-03T14:50:45.712345Z",
 "updated_at": "2026-10-03T14:50:45.712345Z", "book": null}
```

Read it. While the worker has not processed it yet, the answer is `202` with `Retry-After`; then it
is `200` with the data of the book. That takes about a second when Gutendex has the book cached, up
to a minute when it doesn't:

```bash
curl -i localhost:8080/review/01a1023f-006d-716b-abee-f6d3e9210156
```

```json
{
  "id": "01a1023f-006d-716b-abee-f6d3e9210156",
  "status": "completed",
  "book_id": 1342,
  "review": "A classic.",
  "score": 9,
  "created_at": "2026-10-03T14:50:45.712345Z",
  "updated_at": "2026-10-03T14:50:45.712345Z",
  "book": {
    "id": 1342,
    "title": "Pride and Prejudice",
    "authors": [{"name": "Austen, Jane", "birth_year": 1775, "death_year": 1817}],
    "subjects": ["Courtship -- Fiction", "Domestic fiction", "England -- Fiction", "..."],
    "bookshelves": ["Best Books Ever Listings", "Harvard Classics", "..."],
    "languages": ["en"],
    "summaries": ["\"Pride and Prejudice\" by Jane Austen is a novel published in 1813. ..."],
    "cover_url": "https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
    "download_count": 190246
  }
}
```

The worker is often quicker than a second `curl`, so the `202` can go unseen. To watch it, stop
the worker, submit a review and read it, then start the worker and read it again:

```bash
docker compose stop worker
docker compose start worker
```

Change it, then delete it, with the key of the client that wrote it:

```bash
curl -X PUT localhost:8080/review/01a1023f-006d-716b-abee-f6d3e9210156 \
  -H "X-API-Key: local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"review": "Even better the second time.", "score": 10}'
curl -X DELETE localhost:8080/review/01a1023f-006d-716b-abee-f6d3e9210156 \
  -H "X-API-Key: local-dev-key"
```

## Endpoints

| Endpoint | Success | Errors |
|---|---|---|
| `GET /book/search?q={keywords}&page={n}` | 200 | 422 invalid parameters, 404 page past the end, 502/503/504 Gutendex |
| `POST /review` 🔑 | 202 and `Location` | 401, 413, 422 invalid body, unknown book or reused `Idempotency-Key`, 502/503/504 Gutendex, 503 database |
| `GET /review/{id}` | 202 while `pending`, 200 once `completed` or `failed`, 304 with `If-None-Match` | 404, 422 malformed ID, 503 database |
| `PUT /review/{id}` 🔑 | 200 | 401, 403, 404, 412, 413, 422, 503 database |
| `DELETE /review/{id}` 🔑 | 204 | 401, 403, 404, 412, 422 malformed ID, 503 database |
| `GET /healthz` | 200: the process is up | |
| `GET /readyz` | 200: the database is reachable | 503 |

🔑 needs an `X-API-Key` header, see [Authentication](#authentication). Reading and searching are
public.

A 503 carries `Retry-After`: the database is unreachable, or calls to Gutendex are suspended after
repeated failures or because too many are already in progress. A body larger than 64 KiB gets 413.

**Search.** `q` holds the words to look for in titles and author names (at most 200 characters);
results come 32 per page, the most downloaded first.

**Reviews.**
- `id`: the Gutenberg ID of the book, as a number or a numeric string. The book must exist in the catalog.
- `review`: 3 to 5000 characters once trimmed. Line breaks and tabs are fine; other control characters are rejected.
- `score`: an integer from 1 to 10.
- Unknown fields are rejected.

`PUT` takes `review` and `score` only, because the book of a review cannot change.
A review is `failed` when its book is no longer in the catalog by the time the worker processes it.

**Errors** are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem documents,
`application/problem+json`:

```json
{
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "the request is not valid",
  "instance": "/review",
  "request_id": "c54ea9493df2481cbbc9436be65f2157",
  "errors": [{"field": "body.score", "message": "Input should be less than or equal to 10"}]
}
```

The OpenAPI document is served at `/openapi.json` and kept in [docs/openapi.json](docs/openapi.json);
a test fails when the two differ.

## Retries and concurrent edits

**Retrying a POST.** A client that gets no answer (a timeout, a dropped connection) can't know
whether its review was saved. If it sends an `Idempotency-Key` header, it can safely send the
request again. The key is a value unique to that review, such as a UUID:

```bash
curl -i -X POST localhost:8080/review \
  -H "X-API-Key: local-dev-key" \
  -H "Idempotency-Key: 6f1c7e0e-3c2a-4d8e-9a43-5b1f0d2c7e91" \
  -H "Content-Type: application/json" \
  -d '{"id": 1342, "review": "A classic.", "score": 9}'
```

- **Same key, same body.** The answer is the review created the first time, with the same
  `Location`, instead of a duplicate.
- **Same key, different body.** The answer is 422.
- **Scope and lifetime.** Keys belong to the client, so two clients may pick the same value. They
  are remembered for 24 hours, and only while their review exists.

**Lost updates.** Every review response carries an `ETag`. Sent back in `If-Match` with `PUT` or
`DELETE`, it makes the change conditional: if the review has changed in the meantime, through
another edit or because the worker added the book, the answer is `412 Precondition Failed`, and the
client reads the review again before retrying. Without `If-Match` the change is unconditional.

```bash
curl -i -X PUT localhost:8080/review/01a1023f-006d-716b-abee-f6d3e9210156 \
  -H "X-API-Key: local-dev-key" \
  -H 'If-Match: "3f7a9c2e5b8d4f6a1c0e9b7d5a3f1e2c"' \
  -H "Content-Type: application/json" \
  -d '{"review": "Even better the second time.", "score": 10}'
```

**Polling.** A client waiting for the book data can send the `ETag` it has in `If-None-Match`. The
answer is `304 Not Modified`, without a body, until the review changes.

## Authentication

The service is meant to be called by other applications. Each one is a client with a name and an
API key, sent in the `X-API-Key` header to create, change or delete reviews.

- **Ownership.** A review belongs to the client that wrote it: another client gets 403 when it tries
  to change or delete it. Reviews written before API keys existed belong to `anonymous`, a client
  that exists only if someone configures it.
- **Keys at rest.** The service stores only the SHA-256 digest of each key, in `API_KEYS`, and
  compares digests in constant time. A key is a 256-bit random value, so a fast hash is enough.
- **New keys.** `bookreviews-api-key NAME` prints a new key for the client `NAME`, to hand over, and
  the entry to add to `API_KEYS`:

  ```bash
  docker compose run --rm --no-deps api bookreviews-api-key web-shop
  ```

  ```text
  API key for web-shop; hand it to the client, it is not stored anywhere:
    bkr_3V0zvq8G6cJ1pKf9wQ2xYbN7dLmA4sTeR5uHiOjKlZc
  Entry to add to API_KEYS:
    "web-shop": "1f0b4b5c8e3d2a7f9c6e5d4b3a2f1e0d9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f"
  ```

  `API_KEYS` is a JSON object from client names to digests, for example
  `{"web-shop": "1f0b…", "mobile-app": "9a8b…"}`.
- **Rotation.** A client may have several keys, `{"web-shop": ["1f0b…", "77c2…"]}`. To rotate a
  key, add the new digest next to the old one, let the client switch, then remove the old digest.
  The client's name doesn't change, so it keeps its reviews.
- **Logs.** Every log line of an authenticated request carries the client's name.

## How it works

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant G as Gutendex
    participant D as MariaDB
    participant Q as RabbitMQ
    participant W as Worker
    C->>A: POST /review
    A->>G: does the book exist? (unless already cached)
    A->>D: save the review as pending
    A->>Q: publish the review ID
    A-->>C: 202 Accepted, Location: /review/{id}
    Q->>W: deliver the review ID
    W->>G: get the book's data
    W->>D: store the book, mark the review completed
    C->>A: GET /review/{id}
    A-->>C: 200 with the book's data
```

```
src/bookreviews/
├── app.py              FastAPI application: lifespan, routers, health endpoints
├── books.py            GET /book/search
├── reviews.py          /review endpoints and their schemas
├── review_service.py   review model and rules
├── catalog.py          book types, BookCatalog protocol, in-memory cache, circuit breaker
├── gutendex.py         Gutendex client
├── wiring.py           database engine and catalog built from the settings
├── auth.py             API keys: X-API-Key check, key generator (bookreviews-api-key)
├── database.py         SQLAlchemy models and repository
├── migrations/         Alembic migrations
├── queue.py            RabbitMQ topology and publisher
├── enrichment.py       enrichment, message handling, sweeper
├── worker.py           worker process
├── admin.py            maintenance commands (bookreviews-admin)
├── problems.py         RFC 9457 error responses
├── middleware.py       request ID, access log, body limit, security headers
├── logs.py             JSON logging, with the trace ID when there is one
├── metrics.py          Prometheus metrics
├── ops.py              /metrics and /healthz on their own port
├── telemetry.py        OpenTelemetry tracing
├── config.py           settings from environment variables
├── openapi.py          OpenAPI export
└── main.py             console scripts: bookreviews-api, -worker, -migrate
```

The API and the worker run from the same image; each command is a console script of the package.

## Observability

```bash
make observability
```

This starts the stack together with Prometheus (http://localhost:9090), Grafana
(http://localhost:3000, where the "Book reviews" dashboard opens without logging in) and Jaeger
(http://localhost:16686), and turns tracing on. The Compose override behind it is
[docker-compose.observability.yaml](docker-compose.observability.yaml).

**Metrics.** The API and the worker serve Prometheus metrics at `/metrics` on port 9100 (published
as 9100 and 9101 by Compose), never on the public port of the API. Besides the process metrics:

| Metric | Labels | |
|---|---|---|
| `bookreviews_http_requests_total` | `method`, `route`, `status` | requests answered; `route` is the template, such as `/review/{review_id}`, so the series stay few |
| `bookreviews_http_request_duration_seconds` | `method`, `route` | histogram |
| `bookreviews_catalog_requests_total` | `operation`, `outcome` | calls to Gutendex: `ok`, `not_found`, `timeout`, `unavailable`, `error` |
| `bookreviews_catalog_request_duration_seconds` | `operation` | histogram, up to 60 s |
| `bookreviews_catalog_rejections_total` | `reason` | calls refused by the circuit breaker (`circuit_open`) or the concurrency limit (`busy`) |
| `bookreviews_catalog_circuit_open` | | 1 while calls to Gutendex are suspended |
| `bookreviews_catalog_cache_lookups_total` | `result` | `hit`, `miss` |
| `bookreviews_reviews_submitted_total` | `result` | `created`, or `replayed` for a repeated `Idempotency-Key` |
| `bookreviews_queue_publish_failures_total` | `queue` | messages RabbitMQ didn't accept |
| `bookreviews_enrichments_total` | `outcome` | `completed`, `failed`, `skipped`, `retried`, `gave_up`, `parked` |
| `bookreviews_enrichment_duration_seconds` | | histogram |
| `bookreviews_sweeper_actions_total` | `action` | `requeued`, `expired`, `keys_forgotten` |
| `bookreviews_pending_reviews`, `bookreviews_oldest_pending_review_age_seconds` | | the backlog, measured by the worker at every sweep |

**Worker health.** The worker answers `GET /healthz` on port 9100 from its event loop, so a worker
whose loop is stuck fails the check. Compose uses it as the worker's healthcheck.

**Alerts.** [deploy/observability/alerts.yml](deploy/observability/alerts.yml) holds 8 Prometheus
rules:
- the API or the worker down: no instance answers, or none is running, which on Kubernetes leaves
  no target to scrape at all;
- more than 5% of the requests answering 5xx;
- slow review requests;
- calls to Gutendex suspended for 5 minutes;
- reviews pending for more than 30 minutes;
- parked messages;
- RabbitMQ refusing messages.

Unit tests in [alerts.test.yml](deploy/observability/alerts.test.yml) check when each alert fires and
what it says; CI runs them with `promtool`. Each alert links to its section of the
[runbook](docs/runbook.md), which says what to check and what to do. On Kubernetes the same rules
ship as a PrometheusRule (see [Deploying to Kubernetes](#deploying-to-kubernetes)), and a test fails
if the two copies differ.

**Dashboard.** [deploy/observability/grafana/bookreviews.json](deploy/observability/grafana/bookreviews.json)
has four rows:
- **API:** traffic by status, error ratio, latency by route, submissions;
- **Gutendex:** calls and their durations, circuit state, cache hit ratio, refusals;
- **Enrichment:** outcomes, backlog, sweeper, publish failures;
- **Processes:** memory and CPU.

**Tracing.** Setting `OTEL_EXPORTER_OTLP_ENDPOINT` (OTLP over HTTP) turns tracing on; the other
standard `OTEL_*` variables apply, such as `OTEL_SERVICE_NAME` and `OTEL_TRACES_SAMPLER`.
- **One trace per review.** A review is followed in a single trace: the HTTP request, the database
  statements, the publish to RabbitMQ, then the worker's processing. The trace context travels in
  the message headers next to the request ID.
- **Where the spans come from.** The HTTP spans come from FastAPI's own telemetry. The httpx,
  SQLAlchemy and aio-pika spans come from the OpenTelemetry instrumentations. The SQLAlchemy one
  declares support only up to 2.0, so an integration test checks that it traces queries on 2.1.
- **Logs and traces.** Every log line written inside a span carries `trace_id` and `span_id`, so
  logs and traces can be joined.

## Deploying to Kubernetes

The manifests are in [deploy/kubernetes](deploy/kubernetes): a Kustomize base, a component and two
overlays.

| Directory | Contents |
|---|---|
| `base/` | API and worker Deployments, migration Job, Service, autoscaler, disruption budgets, network policies, routes and rate limits |
| `components/monitoring/` | for the Prometheus Operator: a PodMonitor each for the API and the worker, and the alert rules as a PrometheusRule |
| `overlays/local/` | for a kind cluster: MariaDB and RabbitMQ inside the cluster, a Gateway with a self-signed certificate, development secrets |
| `overlays/production/` | the image from GHCR, the public host on an existing Gateway, traces sent to an OpenTelemetry collector |

**Images.** On every push to `main`, CI publishes `ghcr.io/eleuname-dev/progetto-go/bookreviews`,
tagged `main` and `sha-<commit>`, with its SBOM and build provenance attached. A release pins its
tag in the production overlay with `kustomize edit set image`. The image is private, like the
repository: pods pull it through the `bookreviews` service account, with the `ghcr-pull` secret.

**Deploying.** The cluster needs:
- the Gateway API CRDs, Envoy Gateway and a Gateway for the public host;
- the Prometheus Operator, with a Prometheus in the `monitoring` namespace. If that Prometheus
  selects PodMonitors and rules by label, as kube-prometheus-stack does with `release`, add the
  label in the production overlay.

The namespace comes first, then the secrets, which never go into the repository. The pull secret
holds a GitHub token that can read packages:

```bash
kubectl apply -f deploy/kubernetes/overlays/production/namespace.yaml
kubectl -n bookreviews create secret docker-registry ghcr-pull --docker-server=ghcr.io \
  --docker-username=<github user> --docker-password=<token with read:packages>
kubectl -n bookreviews create secret generic bookreviews-secrets \
  --from-literal=DATABASE_URL='mysql+aiomysql://app:…@db.internal:3306/bookreviews' \
  --from-literal=RABBITMQ_URL='amqps://…' \
  --from-literal=API_KEYS='{"web-shop": "…"}'
kubectl -n bookreviews create secret generic bookreviews-migrator \
  --from-literal=DATABASE_URL='mysql+aiomysql://migrator:…@db.internal:3306/bookreviews'
kubectl -n bookreviews delete job bookreviews-migrate --ignore-not-found
kubectl apply -k deploy/kubernetes/overlays/production
```

A Job can't be changed once created, so each release deletes the previous migration Job first.
Finished Jobs are removed after a day anyway.

**Locally on kind.** `make kind-up` sets up a local cluster:
1. it creates a three-node kind cluster;
2. it installs Envoy Gateway, and the Prometheus Operator with a Prometheus in `monitoring`;
3. it builds the image and loads it into the cluster;
4. it applies the local overlay.

`make kind-verify` then checks the deployment:
- Prometheus scrapes every pod and loads the alerts;
- pod security and network policies;
- TLS and rate limits at the gateway;
- workers started while RabbitMQ is down;
- a rollout of the API without a single failed request.

`make kind-down` deletes the cluster.

How the manifests work:

- **Migrations first.** The `bookreviews-migrate` Job applies the migrations with the `migrator`
  account. API and worker pods start with an init container running `bookreviews-migrate --wait`.
  It only reads the schema version, as `app`, and waits until it is current, so new code never
  runs against an old schema. The previous release keeps running during a rollout, so migrations
  have to stay compatible with it.
- **Probes.** Readiness and liveness use `/healthz`, not `/readyz`. If readiness checked the
  database, an outage would take every pod out of service, search included, while the API can
  still search and answer 503 with `Retry-After` for reviews. The worker's liveness probe is its
  own `/healthz` on the metrics port.
- **Rollouts and shutdown.** Rollouts keep every pod running until its replacement is ready
  (`maxUnavailable: 0`). On shutdown, a 5 s pause lets the gateway stop sending traffic first.
  Requests and messages in progress then get 65 s to finish, since Gutendex can take a minute,
  within an 80 s termination grace period.
- **Capacity.**
  - The API scales on CPU from 2 to 10 pods.
  - The worker runs 2 replicas, 3 in production.
  - Disruption budgets let only one pod of each go down at a time during node drains.
  - Resource requests follow the measured usage, about 90 MiB per process.
- **Security.**
  - Pods run as user 10001, with a read-only root filesystem, no capabilities, no privilege
    escalation, the default seccomp profile and no service account token.
  - The production namespace enforces the `restricted` Pod Security Standard.
  - Network policies deny all traffic by default. They let in only the gateway (to the API) and
    Prometheus from the `monitoring` namespace (to the metrics port). They let out only DNS,
    MariaDB, RabbitMQ, HTTPS (for Gutendex) and OTLP.
- **Gateway API, not Ingress.** The community ingress-nginx controller was
  [retired](https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/) in March 2026, so
  traffic comes in through a Gateway API `HTTPRoute` instead.
  - TLS is terminated at the Gateway, plain HTTP is redirected to HTTPS, and HSTS is added.
  - Requests get 75 s; Envoy's default of 15 s would cut slow Gutendex calls.
- **Rate limits.** An Envoy Gateway `BackendTrafficPolicy` caps each Envoy replica at 100 requests
  per second, of which at most 20 that are not `GET`, which means writes. Beyond that the answer
  is 429, with `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers.
  - The write rule matches "every method except `GET`". Listing `POST`, `PUT` and `DELETE` in one
    rule makes Envoy reject the whole route configuration (duplicate rate-limit descriptors).
  - Limits per client, for instance per API key, need Envoy Gateway's global rate limiting, which
    keeps its counters in Redis.
- **kind.** The local overlay makes Envoy's Service a ClusterIP, through an `EnvoyProxy` resource:
  kind can't hand out load-balancer addresses, and the Gateway only reports itself ready once it
  has an address.

## Operations

The [runbook](docs/runbook.md) covers releases and rollbacks, a section for each alert, and the
routine tasks: adding a client or rotating its key, rotating database and RabbitMQ credentials,
scaling, backups.

- **Rollbacks.** A release that finds the schema at a revision it doesn't know assumes a newer
  release put it there. `bookreviews-migrate` then has nothing to do and `--wait` returns at once,
  so the previous release starts again after a rollback. That is safe because a migration must
  not break the release that is still running.
- **Failed reviews.** A review still pending after 24 hours is marked `failed`, which a long
  Gutendex outage can do to many. Once Gutendex is back, `bookreviews-admin retry-failed` enriches
  them again:

  ```bash
  docker compose exec worker bookreviews-admin retry-failed --since 2026-10-01 --dry-run
  docker compose exec worker bookreviews-admin retry-failed --since 2026-10-01
  ```

  A review whose book is still missing stays failed. One that meets an unavailable Gutendex is left
  for the next run.
- **Start-up order.** The worker doesn't need RabbitMQ to start: it waits for it, retrying with a
  growing delay up to 30 s. The API doesn't need it either; reviews submitted meanwhile are queued
  by the sweeper.

## Design notes

### Gutendex is slow

Measured on 3 October 2026:

- Responses already cached by Gutendex's CDN come back in about 0.1 s; the CDN keeps them for 4 hours.
- Uncached responses take 15–40 s for a book and 30–60 s for a search.
- A handful of parallel requests was enough to get 503s.

The design follows from that:

- **Timeout.** Gutendex calls time out after 60 s (`GUTENDEX_TIMEOUT`), and the client gets a 504
  asking to try again later. Gutendex finishes the request anyway and its CDN caches it, so a retry
  a few minutes later is instant.
- **CDN cache hits.** Search queries are lowercased and their spaces collapsed, so equivalent
  searches share a CDN cache entry.
- **Fast validation.** Books seen in search results stay in memory (TTL and LRU). Reviewing a book
  just found doesn't wait for Gutendex: about 10 ms instead of up to 40 s.
- **Async.** The API is asynchronous end to end: a request waiting on Gutendex holds no thread.
- **Circuit breaker.** After 5 consecutive failures (timeouts, 5xx, connection errors), calls to
  Gutendex are suspended for 30 s and requests that need it get a 503 with `Retry-After` at once,
  instead of piling up for a minute each. Then a single probe goes through: it closes the circuit if
  it succeeds and suspends calls again if it fails. "No such book" is an answer, not a failure, and
  cached books are still served while the circuit is open.
- **Bulkhead.** Each process makes at most 8 calls to Gutendex at a time. A request that waits more
  than 10 s for a free slot gets a 503, so a slow Gutendex can't exhaust the API's connections and
  memory.

### Asynchronous enrichment

- **Why aio-pika.** RabbitMQ is driven with aio-pika rather than Celery. The worker reuses the
  async code of the API (repository, Gutendex client), and acknowledgements, retries and
  dead-lettering stay explicit.
- **Delivery.** Delivery is at least once and processing is idempotent. A message is acknowledged
  after the database commit, and a duplicate finds the review already processed.
- **Retries.** A transient failure (Gutendex timeout or 5xx, database error) sends the message to a
  retry queue. After 30 s it dead-letters back to the main queue. The worker makes up to 5
  attempts, counted from RabbitMQ's `x-death` header.
- **Sweeper.** The reviews table is the source of truth: a sweeper in the worker queues again the
  reviews still pending 10 minutes after they were last queued. That covers RabbitMQ being down when
  the review was submitted (the review is saved and accepted anyway) and reviews whose attempts ran
  out. An outage delays the enrichment; it doesn't lose it.
- **Deadline.** A review still pending 24 hours after it was submitted is marked `failed`, so a book
  that Gutendex can never serve doesn't keep the sweeper busy forever. `bookreviews-admin
  retry-failed` gives those reviews another chance, see [Operations](#operations).
- **Old idempotency keys.** The sweeper also deletes the `Idempotency-Key` records older than 24
  hours; the reviews themselves stay.
- **Malformed messages.** A message that isn't an enrichment request is moved to the
  `review.enrichment.parked` queue, with the reason in its `x-parked-reason` header, to be inspected
  rather than lost.
- **No lost updates.** The worker only changes the status and the book data, so a `PUT` running at
  the same time keeps its text and score.
- **Shutdown.** On SIGTERM the worker stops consuming, lets the messages in progress finish, and
  leaves anything unacknowledged to be delivered again.

### Persistence

- **Access and migrations.** MariaDB is accessed through SQLAlchemy 2 (async, aiomysql). Alembic
  manages the schema, applied by `bookreviews-migrate`, which is the `migrate` service in Compose.
  A test runs `alembic check`, so the models and the migrations cannot drift apart.
- **One migration at a time.** `bookreviews-migrate` holds a MariaDB named lock while it runs, so
  two copies started together (two deploys, a retried job) run one after the other; the second
  finds nothing left to do. It gives up after waiting 10 minutes.
- **Review IDs.** IDs are UUIDv7. They cannot be guessed, the API creates them without a round trip
  to the database, and they are time-ordered, so inserts append to the InnoDB index. They are
  stored in MariaDB's native `UUID` type.
- **Storage formats.** Timestamps are stored in UTC with microseconds, text as utf8mb4.
- **Book data.** It lives in a `books` table shared by the reviews of the same book.
- **Connection pool.** Each process keeps up to 5 connections, plus 10 more under load. A request
  waits at most 10 s for a free one. Connections are checked before use and replaced after 30
  minutes. While the database is unreachable the API answers 503 with `Retry-After`.

### API conventions

- **Asynchronous submission.** `POST` answers 202 with `Location`, and `GET` answers 202 with
  `Retry-After` while the review is pending.
- **Idempotent submission.** The `Idempotency-Key` is stored with the review, in the same
  transaction, together with a SHA-256 fingerprint of the request. A primary key on (client, key)
  makes two concurrent requests with the same key end up with one review: the second finds the
  first and answers with it. A repeated request is answered before Gutendex is called, which also
  spares Gutendex the retries of clients that timed out.
- **Entity tags.** The `ETag` is a hash of the review's JSON representation, book data included,
  so it changes whenever the answer would. A `version` column, bumped by every change, makes the
  conditional write atomic: the update applies only if the version is still the one that matched
  `If-Match`. `GET /review/{id}` sends `Cache-Control: no-cache`, so caches may keep a review but
  must revalidate it.
- **Errors.** Every error is a problem document, including unknown routes and wrong methods.
  Internal details never reach the client.
- **Logs.** Logs are JSON, each line with a request ID: the caller's `X-Request-ID` when it is safe
  to log, otherwise a generated one. The ID travels with the RabbitMQ message, so the worker's lines
  about a review carry the ID of the request that created it.
- **Health.** `/healthz` is a liveness check; `/readyz` also checks the database. RabbitMQ is not
  part of readiness: without it the API still accepts reviews, and the sweeper queues them later.

### Security

- **Clients.** Writes need an API key and reviews belong to the client that wrote them, see
  [Authentication](#authentication). Reads stay public, like the reviews themselves.
- **Least privilege in the database.** Two accounts: `migrator` owns the schema and is used only by
  `bookreviews-migrate`; the API and the worker connect as `app`, which can read and write rows but
  cannot create, alter or drop anything.
- **Request limits.** Bodies over 64 KiB are refused with 413: at once when they declare a larger
  `Content-Length`, as soon as they cross the limit when they are streamed. Review texts are at
  most 5000 characters, so real requests stay far below it.
- **Response headers.** Every response carries `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and, unless the endpoint sets its own,
  `Cache-Control: no-store`. JSON responses also get `Content-Security-Policy: default-src 'none';
  frame-ancestors 'none'`. The HTML of `/docs` is left without one, so Swagger UI can load its
  scripts. HSTS belongs to whatever terminates TLS in front of the service.
- **Browsers.** CORS is off unless `CORS_ALLOW_ORIGINS` lists the origins of the web applications
  that call the API; the interactive documentation can be turned off with `API_DOCS_ENABLED=false`.
- **Secrets.** Connection URLs are kept as secrets and API keys as digests, so neither appears in
  logs or in a printed configuration.
- **Image.** The container runs as an unprivileged user, gets the Debian security updates at build
  time, and has no `pip`: the application never installs anything at run time. CI scans the image
  with Trivy and fails on any fixable vulnerability rated high or critical.
- **Dependencies.** pip-audit checks the locked Python dependencies in CI. Dependabot proposes
  weekly updates of the Python packages, base images, GitHub Actions and pre-commit hooks, and
  waits 7 days after a release before proposing it, to keep clear of short-lived malicious
  releases. It updates MariaDB and RabbitMQ in `docker-compose.yaml` only, so a test fails until
  the kind overlay runs the same versions: an update of either takes both files.

### Left out

- Rate limits per client. The Kubernetes gateway limits requests per Envoy replica, not per API
  key: that needs Envoy Gateway's global rate limiting, backed by Redis. The Compose stack has no
  rate limits.
- An endpoint to list reviews, for instance per book (the database already indexes `book_id`).
- A cache shared by several API instances (e.g. Redis).
- Coordination between the sweepers of several workers (`FOR UPDATE SKIP LOCKED`); today they may
  queue the same review twice, which is harmless.

## Configuration

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
| `API_KEYS` | `{}` | JSON object from client names to key digests, see [Authentication](#authentication); empty means every write is refused |
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

## Development

You need [uv](https://docs.astral.sh/uv/), which also installs Python 3.14, and Docker for the
services and the integration tests. `make` lists the tasks; these are the commands behind them:

| Task | Command |
|---|---|
| Install the dependencies | `uv sync` |
| Start MariaDB and RabbitMQ | `docker compose up --detach --wait db rabbitmq` |
| Apply the migrations | `make migrate` (as the `migrator` account) |
| Run the API / the worker | `uv run bookreviews-api` / `uv run bookreviews-worker` |
| Unit tests | `uv run pytest --cov` |
| All the tests | `make test-all` |
| End-to-end checks on a fresh stack | `make e2e` (`uv run python -m e2e.stack`) |
| Format, lint, types | `make fmt`, `make lint` (ruff, mypy) |
| Git hooks | `make hooks` |
| Export the OpenAPI document | `make openapi` |
| Stack with Prometheus, Grafana and Jaeger | `make observability` |
| Check the Prometheus configuration and test the alerts | `make alerts` |
| Validate the Kubernetes manifests | `make manifests` |
| Local Kubernetes cluster with kind | `make kind-up`, `make kind-verify`, `make kind-down` |

**Tests.**
- The unit tests need nothing else.
- The integration tests run against MariaDB and RabbitMQ when `TEST_DATABASE_URL` and
  `TEST_RABBITMQ_URL` are set, and are skipped otherwise. `make test-all` sets them for the
  Compose services.
- `GUTENDEX_LIVE_TEST=1` adds a contract test against the real Gutendex.
- `make e2e` builds the image and starts the whole stack, Prometheus, Grafana and Jaeger included,
  as a separate Compose project with its own volumes. It runs about 140 checks against it, then
  removes it (`--keep` leaves it running). The checks cover the API, what happens when each
  dependency fails, the operations commands, metrics and traces, and take about six minutes. The
  stack uses the same ports as `make up`, so stop that first. Gutendex is live: the few checks that
  depend on it are skipped when it times out.

**Migrations.** With the database running, `uv run alembic revision --autogenerate -m "what changes"`
writes a new migration from the models; review it before committing.

**Git hooks.** The pre-commit hooks (`make hooks`) run ruff, mypy and a check of `uv.lock` on every
commit, and the tests before every push.

**CI.** GitHub Actions runs on every change to this folder:
- ruff, mypy and pip-audit;
- the Prometheus configuration and the alert rule tests, with `promtool`;
- the whole test suite against the MariaDB and RabbitMQ of [docker-compose.yaml](docker-compose.yaml),
  so updating their images there tests the new versions;
- a smoke test of the Docker image and a Trivy scan of it;
- the Kubernetes overlays, built with Kustomize and validated with kubeconform;
- on `main`, the publication of the image to GHCR.

Dependabot proposes the dependency updates, see [Security](#security).
