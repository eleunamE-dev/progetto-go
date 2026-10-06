# Operations and deployment

Commands in this guide run from the `project-be-books` directory.

## Observability

```bash
make observability
```

This starts the stack together with Prometheus (http://localhost:9090), Grafana
(http://localhost:3000, where the "Book reviews" dashboard opens without logging in) and Jaeger
(http://localhost:16686), and turns tracing on. The Compose override behind it is
[docker-compose.observability.yaml](../docker-compose.observability.yaml).

**Metrics.** The API and the worker serve Prometheus metrics at `/metrics` on port 9100 (published
as 9100 and 9101 by Compose), never on the public port of the API. Besides the process metrics:

| Metric | Labels | |
|---|---|---|
| `bookreviews_http_requests_total` | `method`, `route`, `status` | requests answered; `route` is the template, such as `/review/{review_id}`, so the series stay few, and `unmatched` for paths the API doesn't serve |
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

Apart from the HTTP ones, the labelled counters start at 0 for every known label value when the
process starts. Prometheus computes `rate()` and `increase()` from the difference between two
samples, so a series that appeared directly at 1 would hide its first event: the first parked
message would not trigger its alert.

**Worker health.** The worker answers `GET /healthz` on port 9100 from its event loop, so a worker
whose loop is stuck fails the check. Compose uses it as the worker's healthcheck.

**Alerts.** [deploy/observability/alerts.yml](../deploy/observability/alerts.yml) holds 8 Prometheus
rules:
- the API or the worker down: no instance answers, or none is running, which on Kubernetes leaves
  no target to scrape at all;
- more than 5% of the requests answering 5xx;
- slow review requests;
- calls to Gutendex suspended for 5 minutes;
- reviews pending for more than 30 minutes;
- parked messages;
- RabbitMQ refusing messages.

Unit tests in [alerts.test.yml](../deploy/observability/alerts.test.yml) check when each alert fires and
what it says; CI runs them with `promtool`. Each alert links to its section of the
[runbook](../docs/runbook.md), which says what to check and what to do. On Kubernetes the same rules
ship as a PrometheusRule (see [Deploying to Kubernetes](#deploying-to-kubernetes)), and a test fails
if the two copies differ.

**Dashboard.** [deploy/observability/grafana/bookreviews.json](../deploy/observability/grafana/bookreviews.json)
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

The manifests are in [deploy/kubernetes](../deploy/kubernetes): a Kustomize base, a component and two
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

The [runbook](../docs/runbook.md) covers releases and rollbacks, a section for each alert, and the
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
