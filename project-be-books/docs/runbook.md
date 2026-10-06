# Runbook

How to release, roll back and look after the book review service in production, and what to do
when an alert fires. The commands assume the production overlay in the `bookreviews` namespace;
with Docker Compose, `docker compose exec worker …` replaces `kubectl exec deploy/bookreviews-worker …`.
Besides `kubectl`, releases need `kustomize` and the log queries and key changes need `jq`.

- [At a glance](#at-a-glance)
- [Releasing](#releasing)
- [Rolling back](#rolling-back)
- [Alerts](#alerts)
- [Routine tasks](#routine-tasks)
- [Useful queries](#useful-queries)

## At a glance

| Part | Kubernetes | Notes |
|---|---|---|
| API | Deployment `bookreviews-api`, 2 to 10 pods (autoscaler on CPU) | public routes through the Gateway; `/metrics` on port 9100 |
| Worker | Deployment `bookreviews-worker`, 3 pods | enriches reviews from RabbitMQ; `/healthz` and `/metrics` on port 9100 |
| Migrations | Job `bookreviews-migrate`, init container `wait-for-schema` in every pod | the Job uses the `migrator` account, the pods the `app` account |
| MariaDB, RabbitMQ | outside the cluster | the connection URLs are in the Secret `bookreviews-secrets` |
| Monitoring | PodMonitors `bookreviews-api` and `bookreviews-worker`, PrometheusRule `bookreviews` | for the Prometheus Operator; the targets get the jobs `bookreviews-api` and `bookreviews-worker` that the alerts use |
| Gutendex | https://gutendex.com | slow (15–60 s when its CDN has nothing cached) and quick to answer 503 |

Where to look:
- **Grafana**, dashboard "Book reviews": traffic and errors, Gutendex calls and the circuit
  breaker, the enrichment backlog, memory and CPU.
- **Logs** are JSON, one object per line. Each line carries `request_id`, plus `client` for
  authenticated requests, and `trace_id` when tracing is on. The worker's lines about a review carry
  the `request_id` of the request that created it.
- **Traces**, when an OpenTelemetry collector is configured: a review is a single trace, from the
  `POST` to the worker's update.

What clients see when something breaks:
- The database is down: reads and writes answer 503 with `Retry-After`; search still works.
- Gutendex is down: searches, and submissions of books not in the cache, answer 502/503/504;
  submitted reviews stay `pending` until Gutendex is back.
- RabbitMQ is down: submissions still answer 202, and the sweeper queues them once RabbitMQ is
  back.

## Releasing

CI publishes every commit on `main` as `ghcr.io/eleuname-dev/progetto-go/bookreviews:sha-<commit>`.

1. Pin the tag in [overlays/production/kustomization.yaml](../deploy/kubernetes/overlays/production/kustomization.yaml),
   by hand or with:

   ```bash
   cd deploy/kubernetes/overlays/production
   kustomize edit set image ghcr.io/eleuname-dev/progetto-go/bookreviews:sha-<commit>
   ```

2. Apply it. A Job can't be changed once created, so the previous migration Job goes first:

   ```bash
   kubectl -n bookreviews delete job bookreviews-migrate --ignore-not-found
   kubectl apply -k deploy/kubernetes/overlays/production
   ```

3. Follow the rollout:

   ```bash
   kubectl -n bookreviews rollout status deployment/bookreviews-api --timeout=10m
   kubectl -n bookreviews rollout status deployment/bookreviews-worker --timeout=10m
   ```

   New pods wait in their `wait-for-schema` init container until the Job has migrated the
   database. The old pods keep serving meanwhile. If the rollout doesn't progress, look at the Job:
   `kubectl -n bookreviews logs job/bookreviews-migrate`.

4. Watch the dashboard for ten minutes: error ratio, latency, enrichment outcomes.

**Migrations must not break the release that is still running.** During a rollout, and after a
rollback, the previous release runs against the new schema. So a migration may add tables, indexes,
nullable columns or columns with a default. Renaming or dropping waits for a later release, once no
running code uses the old name.

## Rolling back

Go back to the previous image:

```bash
kubectl -n bookreviews rollout undo deployment/bookreviews-api
kubectl -n bookreviews rollout undo deployment/bookreviews-worker
```

Then pin the previous tag in the production overlay as well, so the next `apply` doesn't bring the
faulty release back.

- **The schema stays.** The database keeps the newer schema, which the previous release can use
  (see above). Its pods recognise a revision they don't know as newer: they log `database schema
  newer than this release` and start, and its migration Job logs `nothing to migrate` and succeeds.
- **No schema downgrades.** Never downgrade the schema of a database in use: a downgrade drops
  what the newer release added, data included. The downgrade scripts exist for development.
- **Lost data** is restored from the database backups, see [Database](#database).

## Alerts

The rules are in [deploy/observability/alerts.yml](../deploy/observability/alerts.yml); each one
links to its section below.

### BookReviewsApiDown

For 2 minutes no API pod has answered Prometheus, or there has been none: the API is down. A
single pod failing while others serve doesn't raise it; the Deployment replaces that pod.

```bash
kubectl -n bookreviews get deployment,pods -l app.kubernetes.io/component=api
kubectl -n bookreviews describe pod <pod>
kubectl -n bookreviews logs <pod> -c api --previous
```

- **No pods, or `Pending`:** the Deployment was scaled to zero, or the nodes have no room for the
  pods (`describe pod` shows why the scheduler can't place them).
- **Stuck in `Init`:** the pod is waiting for the schema. Read the logs of the init container
  (`-c wait-for-schema`) and of the migration Job; a Job that failed has to be fixed and run again
  (delete it and apply the overlay).
- **`CrashLoopBackOff` right after a release:** usually the configuration. For example, an
  `API_KEYS` that isn't valid JSON stops the API at start-up with `error parsing value for field
  "api_keys"`. Fix the Secret or [roll back](#rolling-back).
- **`OOMKilled`:** raise the memory limit in `base/api.yaml`. The API uses about 90 MiB.
- **Pods running and ready:** Prometheus can't reach them. Check that it runs in the `monitoring`
  namespace (the network policy only lets that namespace in) and that it selects the PodMonitors:
  their targets show up in Prometheus under Status → Targets.

### BookReviewsWorkerDown

For 2 minutes no worker pod has answered Prometheus, or there has been none. While no worker runs,
reviews are accepted but stay `pending`; nothing is lost, since the messages wait in RabbitMQ and
the sweeper queues again whatever is left behind.

The checks are those of [BookReviewsApiDown](#bookreviewsapidown), with
`-l app.kubernetes.io/component=worker` and `-c worker`. One more case: a worker that can't reach
RabbitMQ at start-up logs `RabbitMQ not reachable, retrying` and keeps trying, waiting longer each
time, up to 30 s. It stays up meanwhile, so the cause is RabbitMQ or its URL rather than the worker.

### BookReviewsHighErrorRate

More than 5% of the requests answered 5xx for 10 minutes. First see which routes and statuses:

```promql
sum by (route, status) (rate(bookreviews_http_requests_total{status=~"5.."}[5m]))
```

- **503 on `/review` routes:** the database. The API logs `database unavailable` with the driver's
  error. Check MariaDB itself, then the network policies, then the connection count against
  `max_connections` (see [Scaling](#scaling)).
- **502, 503 or 504 on `/book/search` and `POST /review`:** Gutendex, see
  [BookReviewsCatalogSuspended](#bookreviewscatalogsuspended).
- **500:** a bug. The API logs `unhandled error` with the stack trace; the `trace_id` on the line
  leads to the trace. If it started with a release, [roll back](#rolling-back).

### BookReviewsSlowRequests

The 95th percentile of a `/review` route has been above 2 s for 10 minutes.

- **`POST /review` only:** the book of the review wasn't in the cache, so the API asked Gutendex,
  which can take up to a minute. Look at `bookreviews_catalog_request_duration_seconds` and the
  cache hit ratio on the dashboard. There is little to do about Gutendex itself; the circuit breaker
  takes over if it starts failing.
- **Every route:** the database or the pods. Look for slow queries on MariaDB, for requests that
  waited too long for a connection (`database unavailable` lines mentioning `QueuePool limit`), and
  at CPU: when the autoscaler is at 10 pods, raise `maxReplicas`.

### BookReviewsCatalogSuspended

For 5 minutes the circuit breaker has refused calls to Gutendex: Gutendex failed 5 times in a
row, and every probe since has failed too. Searches and submissions of books not in the cache
answer 503, and enrichments wait. The alert counts refused calls rather than the state of the
circuit: when nobody calls, the circuit stays open until the next call, and nobody is affected.

```bash
curl -sS -o /dev/null -w '%{http_code} in %{time_total} s\n' https://gutendex.com/books/1342/
kubectl -n bookreviews exec deploy/bookreviews-api -c api -- python -c \
  "import httpx; print(httpx.get('https://gutendex.com/books/1342/', follow_redirects=True, timeout=60))"
```

- **Gutendex is down for everyone:** nothing to fix on this side. Calls resume by themselves: a probe
  goes through every 30 s and closes the circuit when it succeeds. Pending reviews are enriched
  then.
- **Gutendex answers from your machine but not from the pod:** look at the egress network policy,
  the DNS, and any proxy or firewall between the cluster and the Internet.
- **The outage lasts more than 24 hours:** reviews pending since before it are marked `failed`.
  Once Gutendex is back, [retry them](#retrying-failed-reviews).

### BookReviewsEnrichmentBacklog

The oldest pending review was submitted more than 30 minutes ago: reviews are not being enriched.

1. **Workers:** are they running? See [BookReviewsWorkerDown](#bookreviewsworkerdown).
2. **Gutendex:** is the circuit open? See
   [BookReviewsCatalogSuspended](#bookreviewscatalogsuspended). If so, the backlog clears itself
   when Gutendex is back.
3. **RabbitMQ:** do messages reach the worker? Check the `review.enrichment` queue in the management
   UI: messages ready with no consumers point to the workers, a queue that stays empty points to
   publishing (see [BookReviewsQueuePublishFailures](#bookreviewsqueuepublishfailures)).
4. **Outcomes:** the `bookreviews_enrichments_total` panel tells whether the workers complete,
   retry or give up.

Adding workers helps only when they are the bottleneck. Each worker processes 4 messages at once
(`WORKER_CONCURRENCY`), so each one adds up to 4 parallel calls to Gutendex, which starts answering
503 under parallel load:

```bash
kubectl -n bookreviews scale deployment/bookreviews-worker --replicas=5
```

Bring the replicas back to the overlay's value afterwards (the next `apply` does it too).

### BookReviewsParkedMessages

The worker moved messages it couldn't read to the `review.enrichment.parked` queue. The API never
publishes such messages, so something else published to `review.enrichment`: another application,
someone by hand, or a release that changed the message format.

Look at them without removing them, either in the management UI (Queues → `review.enrichment.parked`
→ Get messages, with "Nack message requeue true") or through the management API:

```bash
curl -sS -u <user>:<password> -H 'content-type: application/json' \
  -d '{"count": 10, "ackmode": "ack_requeue_true", "encoding": "auto"}' \
  https://<rabbitmq-management>/api/queues/%2F/review.enrichment.parked/get
```

The `x-parked-reason` header says what was wrong. Find the producer and fix it, then purge the
queue. No review is lost meanwhile: the sweeper queues again every review that is still pending.

### BookReviewsQueuePublishFailures

The API can't publish to RabbitMQ. Reviews are still saved and accepted (the API logs `review saved
but not queued, the sweeper will queue it`), and the sweeper queues them once RabbitMQ is back, so
enrichment is delayed, not lost.

- **RabbitMQ is down or unreachable:** check RabbitMQ, then the egress network policy (ports 5672 and
  5671).
- **RabbitMQ blocks publishers:** a memory or disk alarm makes it refuse messages
  (`rabbitmq-diagnostics alarms`). Free memory or disk on RabbitMQ.
- **Credentials:** the user in `RABBITMQ_URL` was changed or deleted (see
  [Rotating credentials](#rotating-credentials)).

## Routine tasks

### Adding a client or rotating its key

Generate a key with the image of the release (after `docker login ghcr.io`, since the image is
private), or with `uv run bookreviews-api-key web-shop` in a checkout:

```bash
docker run --rm ghcr.io/eleuname-dev/progetto-go/bookreviews:main bookreviews-api-key web-shop
```

Hand the key to the client and add the digest to `API_KEYS`, a JSON object from client names to
digests. A client may have several digests during a rotation: `{"web-shop": ["<old>", "<new>"]}`.

```bash
kubectl -n bookreviews get secret bookreviews-secrets -o jsonpath='{.data.API_KEYS}' | base64 -d > api-keys.json
# edit api-keys.json
kubectl -n bookreviews patch secret bookreviews-secrets --type merge \
  -p "{\"stringData\": {\"API_KEYS\": $(jq -Rs . < api-keys.json)}}"
kubectl -n bookreviews rollout restart deployment/bookreviews-api
rm api-keys.json
```

The API reads the Secret at start-up, hence the restart, which replaces the pods one at a time
without downtime. To finish a rotation, or to revoke a client, remove the old digest the same way.
A client's name must not change, because its reviews belong to that name.

### Rotating credentials

Changing a password in place breaks every pod that still has the old one, until it restarts.
Rotate the account rather than its password:

1. Create a new account with the same rights as the current one. On MariaDB,
   `SHOW GRANTS FOR 'app'@'%'` lists them; on RabbitMQ, `rabbitmqctl list_user_permissions <user>`.
2. Put the new account in `DATABASE_URL` or `RABBITMQ_URL` in `bookreviews-secrets`, and restart:
   `kubectl -n bookreviews rollout restart deployment/bookreviews-api deployment/bookreviews-worker`.
3. Once the rollout is done, drop the old account.

The `migrator` account, in the `bookreviews-migrator` Secret, is used only while the migration Job
runs; the next release picks up the new one.

### Retrying failed reviews

A review still pending 24 hours after it was submitted is marked `failed`, so a long Gutendex
outage leaves failed reviews behind. Once Gutendex is back, enrich them again from a worker pod,
which has the database and Gutendex settings:

```bash
kubectl -n bookreviews exec deploy/bookreviews-worker -c worker -- \
  bookreviews-admin retry-failed --since 2026-10-01 --dry-run
kubectl -n bookreviews exec deploy/bookreviews-worker -c worker -- \
  bookreviews-admin retry-failed --since 2026-10-01
```

```text
12 failed reviews retried: 11 completed, 1 still failed (book not in the catalog), 0 not retried (catalog unavailable, run again later), 0 skipped (changed meanwhile)
```

- `--since` and `--until` take ISO 8601 times (UTC unless they say otherwise) and select reviews by
  submission time.
- `--limit` caps the reviews of a run (1000 by default), `--concurrency` the parallel calls to
  Gutendex (4).
- Reviews that meet an unavailable Gutendex stay failed and are counted as not retried: run the
  command again later. Running it twice is harmless, since it only touches reviews that are still
  failed.

### Scaling

- **API:** the autoscaler keeps 2 to 10 pods at 70% CPU. Change the bounds in `base/api.yaml`; for a
  known peak, raise the minimum for a while:
  `kubectl -n bookreviews patch hpa bookreviews-api -p '{"spec": {"minReplicas": 4}}'`.
- **Worker:** 3 replicas in production, each processing `WORKER_CONCURRENCY` (4) messages at once.
  See [BookReviewsEnrichmentBacklog](#bookreviewsenrichmentbacklog) before adding more.
- **Database connections:** each process keeps up to `DATABASE_POOL_SIZE` + `DATABASE_MAX_OVERFLOW`
  connections (5 + 10). At full scale, 10 API pods and 3 workers can open 195, more than MariaDB's
  default `max_connections` of 151. Raise it, or lower `DATABASE_MAX_OVERFLOW`, before raising
  the number of pods.

### Database

- **Backups** are not the service's job. Use the point-in-time recovery of the managed database,
  or a nightly `mariadb-dump --single-transaction bookreviews` if there is none.
- **MariaDB upgrades:** Dependabot proposes new MariaDB images for Compose, and CI runs the whole
  test suite against them. Upgrade the production server the way its provider documents, after the
  tests pass.

## Useful queries

Logs, with `jq`:

```bash
# Errors of the last hour, across API and worker
kubectl -n bookreviews logs -l app.kubernetes.io/name=bookreviews --since=1h --tail=-1 \
  | jq -cR 'fromjson? | select(.level == "ERROR")'
# Everything about one request, from the API and from the worker
kubectl -n bookreviews logs -l app.kubernetes.io/name=bookreviews --since=24h --tail=-1 \
  | jq -cR 'fromjson? | select(.request_id == "<request id>")'
```

PromQL:

```promql
# Error ratio by route
sum by (route) (rate(bookreviews_http_requests_total{status=~"5.."}[5m]))
  / sum by (route) (rate(bookreviews_http_requests_total[5m]))
# Gutendex calls by outcome
sum by (operation, outcome) (rate(bookreviews_catalog_requests_total[5m]))
# Enrichment outcomes
sum by (outcome) (increase(bookreviews_enrichments_total[1h]))
```

SQL, as `app` or a read-only account:

```sql
-- Reviews by status
SELECT status, COUNT(*) FROM reviews GROUP BY status;
-- The oldest pending reviews
SELECT id, book_id, created_at, queued_at FROM reviews
WHERE status = 'pending' ORDER BY created_at LIMIT 20;
-- Reviews that failed in the last week
SELECT id, book_id, created_at FROM reviews
WHERE status = 'failed' AND created_at > NOW() - INTERVAL 7 DAY ORDER BY created_at;
```
