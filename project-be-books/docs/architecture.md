# Architecture and design decisions

Commands in this guide run from the `project-be-books` directory.

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
  retry-failed` gives those reviews another chance, see [Operations](operations.md#operations).
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
- **Waiting for the database.** Before migrating, `bookreviews-migrate` waits until MariaDB accepts
  connections, for up to 10 minutes as well, and logs `database not reachable yet` meanwhile. On a
  fresh cluster the Job starts together with the database.
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
  conditional write detect concurrent review changes. The repository also rechecks `If-Match`
  while holding locks on the review and its shared book, so a metadata refresh cannot bypass
  the precondition. `GET /review/{id}` sends `Cache-Control: no-cache`, so caches may keep a review but
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
  [Authentication](../README.md#authentication). Reads stay public, like the reviews themselves.
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
