# Book reviews

A service to search the books of [Project Gutenberg](https://www.gutenberg.org), through the
[Gutendex](https://gutendex.com) API, and review them. A review is accepted right away; a worker
then adds the book's data (cover, authors, subjects, summary…) in the background.

Python 3.14, FastAPI, MariaDB, RabbitMQ. The original assignment is in [ASSIGNMENT.md](ASSIGNMENT.md).

- [Quick start](#quick-start)
- [A tour of the API](#a-tour-of-the-api)
- [Endpoints](#endpoints)
- [Retries and concurrent edits](#retries-and-concurrent-edits)
- [Authentication](#authentication)
- [How it works](#how-it-works)
- [Design notes](#design-notes)
- [Configuration](#configuration)
- [Development](#development)

## Quick start

You only need Docker with Compose.

```bash
docker compose up --build --wait
```

This starts MariaDB, RabbitMQ, a one-shot job that applies the database migrations, the API and
the enrichment worker. Then:

- API: http://localhost:8080, with interactive documentation at http://localhost:8080/docs
- RabbitMQ management: http://localhost:15672 (`user` / `password`)
- API key for writing reviews: `local-dev-key` (client `local-dev`)

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
├── problems.py         RFC 9457 error responses
├── middleware.py       request ID and access log
├── logs.py             JSON logging
├── config.py           settings from environment variables
├── openapi.py          OpenAPI export
└── main.py             console scripts: bookreviews-api, -worker, -migrate
```

The API and the worker run from the same image; each command is a console script of the package.

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
  that Gutendex can never serve doesn't keep the sweeper busy forever.
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
  releases.

### Left out

- Rate limiting: it belongs in front of the service (ingress or API gateway), which sees every
  instance and the client's address.
- An endpoint to list reviews, for instance per book (the database already indexes `book_id`).
- Metrics and tracing (Prometheus, OpenTelemetry).
- A cache shared by several API instances (e.g. Redis).
- Coordination between the sweepers of several workers (`FOR UPDATE SKIP LOCKED`); today they may
  queue the same review twice, which is harmless.

## Configuration

Every setting has a default that works with the Compose services on `localhost`.

| Variable | Default | |
|---|---|---|
| `LOG_LEVEL` | `INFO` | |
| `HTTP_HOST` | `0.0.0.0` | API |
| `HTTP_PORT` | `8080` | API |
| `HTTP_SHUTDOWN_TIMEOUT` | `15` | seconds left to running requests on shutdown |
| `HTTP_MAX_BODY_SIZE` | `65536` | bytes; a larger request body gets 413 |
| `API_KEYS` | `{}` | JSON object from client names to key digests, see [Authentication](#authentication); empty means every write is refused |
| `API_DOCS_ENABLED` | `true` | serve `/docs`, `/redoc` and `/openapi.json` |
| `CORS_ALLOW_ORIGINS` | `[]` | JSON list of the origins allowed to call the API from a browser, e.g. `["https://shop.example.com"]` |
| `DATABASE_URL` | `mysql+aiomysql://app:app-password@localhost:3306/bookreviews` | the migrations need an account that can change the schema |
| `DATABASE_POOL_SIZE` | `5` | connections each process keeps open |
| `DATABASE_MAX_OVERFLOW` | `10` | extra connections opened under load |
| `DATABASE_POOL_TIMEOUT` | `10` | seconds a request waits for a free connection |
| `RABBITMQ_URL` | `amqp://user:password@localhost:5672/` | |
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
| Format, lint, types | `make fmt`, `make lint` (ruff, mypy) |
| Git hooks | `make hooks` |
| Export the OpenAPI document | `make openapi` |

**Tests.**
- The unit tests need nothing else.
- The integration tests run against MariaDB and RabbitMQ when `TEST_DATABASE_URL` and
  `TEST_RABBITMQ_URL` are set, and are skipped otherwise. `make test-all` sets them for the
  Compose services.
- `GUTENDEX_LIVE_TEST=1` adds a contract test against the real Gutendex.

**Migrations.** With the database running, `uv run alembic revision --autogenerate -m "what changes"`
writes a new migration from the models; review it before committing.

**Git hooks.** The pre-commit hooks (`make hooks`) run ruff, mypy and a check of `uv.lock` on every
commit, and the tests before every push.

**CI.** GitHub Actions runs on every change to this folder:
- ruff, mypy and pip-audit;
- the whole test suite against MariaDB and RabbitMQ service containers;
- a smoke test of the Docker image and a Trivy scan of it.

Dependabot proposes the dependency updates, see [Security](#security).
