import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from e2e.checks import Checks, wait_until

PROJECT = Path(__file__).resolve().parents[1]
COMPOSE = (
    "docker",
    "compose",
    "--project-name",
    "bookreviews-e2e",
    "--file",
    "docker-compose.yaml",
    "--file",
    "docker-compose.observability.yaml",
    "--file",
    "e2e/compose.yaml",
)
API_URL = "http://localhost:8080"
NOT_QUEUED = "review saved but not queued, the sweeper will queue it"
API_KEY = "local-dev-key"
OTHER_API_KEY = "e2e-other-key"
ROOT = ("root", "rootpassword")
APP = ("app", "app-password")
VALID = {"id": 1342, "review": "A classic.", "score": 9}
REVIEW_FIELDS = {"id", "title", "authors", "languages", "cover_url", "download_count"}
DASHBOARD_METRICS = (
    "bookreviews_http_requests_total",
    "bookreviews_reviews_submitted_total",
    "bookreviews_enrichments_total",
    "bookreviews_pending_reviews",
    "process_resident_memory_bytes",
)

type Json = dict[str, Any]


def run(*command: str) -> str:
    finished = subprocess.run(
        command,
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if finished.returncode:
        raise RuntimeError(
            f"{' '.join(command)} failed:\n{finished.stdout[-1500:]}\n{finished.stderr[-1500:]}"
        )
    return finished.stdout + finished.stderr


def compose(*arguments: str) -> str:
    return run(*COMPOSE, *arguments)


def environment(**variables: str) -> list[str]:
    return [argument for name, value in variables.items() for argument in ("-e", f"{name}={value}")]


def sql(statement: str, account: tuple[str, str] = ROOT) -> str:
    user, secret = account
    return compose(
        "exec", "-T", "db", "mariadb", f"-u{user}", f"-p{secret}", "-N", "-e", statement
    ).strip()


def _json_object(line: str) -> Json | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def json_lines(text: str) -> list[Json]:
    return [record for line in text.splitlines() if (record := _json_object(line)) is not None]


def logs(service: str) -> list[Json]:
    return json_lines(compose("logs", "--no-log-prefix", service))


def container_logs(name: str) -> list[Json]:
    return json_lines(run("docker", "logs", name))


def messages(service: str, message: str) -> list[Json]:
    return [line for line in logs(service) if line.get("msg") == message]


def is_problem(response: httpx.Response) -> bool:
    content_type: str | None = response.headers.get("content-type")
    return content_type == "application/problem+json"


def fields(response: httpx.Response) -> list[str]:
    return [error["field"] for error in response.json().get("errors", [])]


def responds(client: httpx.Client, path: str) -> bool:
    try:
        return client.get(path).status_code == 200
    except httpx.TransportError:
        return False


def rfc3339(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def shorten(values: Json) -> Json:
    return {
        k: v if len(str(v)) < 20 else f"{str(v)[:5]}... ({len(str(v))})" for k, v in values.items()
    }


class Stack:
    def __init__(self, checks: Checks) -> None:
        self.checks = checks
        self.client = httpx.Client(base_url=API_URL, timeout=120, headers={"X-API-Key": API_KEY})
        self.anonymous = httpx.Client(base_url=API_URL, timeout=120)

    def submit(self, **changes: object) -> httpx.Response:
        return self.client.post("/review", json=VALID | changes)

    def status_of(self, location: str) -> int:
        return self.client.get(location).status_code

    def not_queued(self, review_id: str) -> bool:
        return any(line.get("review_id") == review_id for line in messages("api", NOT_QUEUED))

    def completes(self, location: str, timeout: float) -> bool:
        return wait_until(lambda: self.status_of(location) == 200, timeout, interval=1) is not None

    def conventions(self) -> None:
        check = self.checks.check
        r = self.client.get("/healthz")
        check("GET /healthz: 200", r.status_code == 200 and r.json() == {"status": "ok"}, r.text)
        r = self.client.get("/readyz")
        check("GET /readyz: 200 with the database up", r.status_code == 200, r.text)
        r = self.client.get("/docs")
        check("Swagger UI is served at /docs", "swagger" in r.text.lower(), r.status_code)
        committed = json.loads((PROJECT / "docs" / "openapi.json").read_text(encoding="utf-8"))
        r = self.client.get("/openapi.json")
        check("the served OpenAPI document equals docs/openapi.json", r.json() == committed)
        r = self.client.get("/healthz", headers={"X-Request-ID": "e2e-trace-1"})
        check("the caller's X-Request-ID is echoed", r.headers.get("x-request-id") == "e2e-trace-1")
        r = self.client.get("/healthz", headers={"X-Request-ID": 'bad "id"'})
        generated = r.headers.get("x-request-id", "")
        check("an unsafe X-Request-ID is replaced", len(generated) == 32, generated)
        r = self.client.get("/nope")
        check(
            "unknown route: 404 problem",
            r.status_code == 404 and is_problem(r) and r.json()["instance"] == "/nope",
            r.text,
        )
        r = self.client.delete("/healthz")
        check(
            "wrong method: 405 problem with Allow",
            r.status_code == 405 and is_problem(r) and r.headers.get("allow") == "GET",
            r.text,
        )

    def authentication(self) -> None:
        check = self.checks.check
        r = self.anonymous.post("/review", json=VALID)
        check(
            "POST without X-API-Key: 401 problem with WWW-Authenticate: ApiKey",
            r.status_code == 401
            and is_problem(r)
            and r.headers.get("www-authenticate") == "ApiKey",
            r.text,
        )
        r = self.anonymous.post("/review", json=VALID, headers={"X-API-Key": "not-a-key"})
        check("POST with a wrong key: 401", r.status_code == 401, r.text)
        r = self.anonymous.post("/review", json={"id": "not a number"})
        check("authentication comes before validation: 401, not 422", r.status_code == 401, r.text)
        owned = self.submit(review="Owned by the local-dev client.")
        check("POST with the local-dev key: 202", owned.status_code == 202, owned.text)
        location = owned.headers["location"]
        r = self.anonymous.get(location)
        check("reading a review needs no key", r.status_code in {200, 202}, r.text)
        r = self.anonymous.put(location, json={"review": "Hijacked.", "score": 1})
        check("PUT without a key: 401", r.status_code == 401, r.text)
        other = {"X-API-Key": OTHER_API_KEY}
        r = self.client.put(location, json={"review": "Hijacked.", "score": 1}, headers=other)
        check("PUT by another client: 403 problem", r.status_code == 403 and is_problem(r), r.text)
        r = self.client.delete(location, headers=other)
        check("DELETE by another client: 403", r.status_code == 403, r.text)
        unchanged = self.client.get(location).json().get("review")
        check("...and the review is untouched", unchanged == "Owned by the local-dev client.")
        r = self.client.put(location, json={"review": "Edited by its owner.", "score": 8})
        check("PUT by the owner: 200", r.status_code == 200, r.text)
        check(
            "the access log names the client",
            any(line.get("client") == "local-dev" for line in messages("api", "http request")),
        )

    def hardening(self) -> None:
        check = self.checks.check
        migrate = compose("ps", "--all", "--quiet", "migrate").strip()
        exit_code = run("docker", "inspect", "--format", "{{.State.ExitCode}}", migrate).strip()
        variables = run(
            "docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", migrate
        )
        check(
            "the migrations ran as the migrator account and exited cleanly",
            exit_code == "0" and "//migrator:" in variables,
            exit_code,
        )
        waited = compose("exec", "-T", "api", "bookreviews-migrate", "--wait", "--timeout", "10")
        check(
            "the schema is at the latest migration", "database schema up to date" in waited, waited
        )
        try:
            sql("CREATE TABLE bookreviews.e2e_probe (id INT)", APP)
            denied = "CREATE TABLE succeeded"
        except RuntimeError as exc:
            denied = str(exc)
        check("the app's account cannot change the schema", "denied" in denied.lower(), denied)
        r = self.client.post(
            "/review", content=b"x" * 70_000, headers={"Content-Type": "application/json"}
        )
        check("a 70 kB body: 413 problem", r.status_code == 413 and is_problem(r), r.text)
        r = self.client.get("/healthz")
        expected = {
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
            "referrer-policy": "no-referrer",
            "cache-control": "no-store",
        }
        headers = {name: r.headers.get(name) for name in expected}
        check("security headers on API responses", headers == expected, headers)
        r = self.client.get("/docs")
        check(
            "the docs page has no CSP, so Swagger UI can load its scripts",
            r.status_code == 200 and "content-security-policy" not in r.headers,
        )
        r = self.anonymous.options(
            "/review",
            headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"},
        )
        check("no CORS unless configured", "access-control-allow-origin" not in r.headers)

    def safe_writes(self) -> None:
        check = self.checks.check
        key = str(uuid.uuid4())
        twice = VALID | {"review": "Sent twice by a client that timed out."}
        first = self.client.post("/review", json=twice, headers={"Idempotency-Key": key})
        again = self.client.post("/review", json=twice, headers={"Idempotency-Key": key})
        check(
            "a POST repeated with the same Idempotency-Key returns the same review",
            first.status_code == again.status_code == 202
            and first.json()["id"] == again.json()["id"],
            (first.text[:200], again.text[:200]),
        )
        changed = VALID | {"review": "Something else."}
        r = self.client.post("/review", json=changed, headers={"Idempotency-Key": key})
        check(
            "the same key with another body: 422 on header.Idempotency-Key",
            r.status_code == 422 and fields(r) == ["header.Idempotency-Key"],
            r.text,
        )
        stored = sql(
            "SELECT COUNT(*) FROM bookreviews.reviews r JOIN bookreviews.idempotency_keys k "
            f"ON k.review_id = r.id WHERE k.idempotency_key = '{key}'"
        )
        check("one review and one key stored for the two requests", stored == "1", stored)
        location = first.headers["location"]
        created_tag = first.headers.get("etag", "")
        check("POST returns an ETag", re.fullmatch(r'"[0-9a-f]{32}"', created_tag), created_tag)
        check("the worker completes the review", self.completes(location, 60))
        current_tag = self.client.get(location).headers.get("etag", "")
        check(
            "the ETag changes when the worker adds the book", current_tag not in {"", created_tag}
        )
        r = self.client.get(location, headers={"If-None-Match": current_tag})
        check(
            "GET with the current ETag in If-None-Match: 304 without a body",
            r.status_code == 304 and r.content == b"" and r.headers.get("etag") == current_tag,
            r.status_code,
        )
        edit = {"review": "Edited from a stale copy.", "score": 5}
        r = self.client.put(location, json=edit, headers={"If-Match": created_tag})
        check("PUT with a stale ETag: 412 problem", r.status_code == 412 and is_problem(r), r.text)
        r = self.client.put(location, json=edit, headers={"If-Match": current_tag})
        edited_tag = r.headers.get("etag", "")
        check(
            "PUT with the current ETag: 200 and a new ETag",
            r.status_code == 200 and edited_tag not in {"", current_tag},
            r.text,
        )
        r = self.client.delete(location, headers={"If-Match": current_tag})
        check("DELETE with a stale ETag: 412", r.status_code == 412, r.text)
        r = self.client.delete(location, headers={"If-Match": edited_tag})
        check("DELETE with the current ETag: 204", r.status_code == 204, r.text)
        left = sql(
            f"SELECT COUNT(*) FROM bookreviews.idempotency_keys WHERE idempotency_key = '{key}'"
        )
        check("the key goes away with its review", left == "0", left)
        r = self.client.post("/review", json=twice, headers={"Idempotency-Key": key})
        check(
            "once the review is deleted the key is free again",
            r.status_code == 202 and r.json()["id"] != first.json()["id"],
            r.text[:200],
        )

    def timed_search(self, **params: str | int) -> tuple[httpx.Response, float]:
        start = time.monotonic()
        response = self.client.get("/book/search", params=params)
        return response, time.monotonic() - start

    def search(self) -> None:
        check = self.checks.check
        r, elapsed = self.timed_search(q="  Pride   PREJUDICE ")
        found = r.status_code == 200 and any(b["id"] == 1342 for b in r.json()["results"])
        check(f"search 'Pride PREJUDICE' finds book 1342 ({elapsed:.2f}s)", found, r.text[:300])
        if found:
            check(
                "results expose id, title, authors, languages, cover_url, download_count",
                all(set(book) == REVIEW_FIELDS for book in r.json()["results"]),
            )
            check("pagination fields", set(r.json()) == {"count", "page", "next_page", "results"})
        r, elapsed = self.timed_search(q="pride prejudice", page=2)
        if r.status_code == 504:
            self.checks.skip("page past the end", f"Gutendex timed out after {elapsed:.0f}s")
        else:
            check(
                f"page past the end: 404 problem ({elapsed:.2f}s)",
                r.status_code == 404
                and is_problem(r)
                and r.json()["detail"] == "page 2 does not exist for this search",
                r.text,
            )
        r, elapsed = self.timed_search(q="dickens")
        if r.status_code == 504:
            self.checks.skip("search 'dickens'", f"Gutendex timed out after {elapsed:.0f}s")
        else:
            check(
                f"search 'dickens': 200 with a next page ({elapsed:.2f}s)",
                r.status_code == 200
                and r.json()["next_page"] == 2
                and len(r.json()["results"]) == 32,
                r.text[:200],
            )
        invalid: list[tuple[Json, str]] = [
            ({}, "query.q"),
            ({"q": "   "}, "query.q"),
            ({"q": "a" * 201}, "query.q"),
            ({"q": "x", "page": 0}, "query.page"),
            ({"q": "x", "page": "two"}, "query.page"),
        ]
        for params, field in invalid:
            r = self.client.get("/book/search", params=params)
            check(
                f"search {shorten(params)}: 422 on {field}",
                r.status_code == 422 and is_problem(r) and fields(r) == [field],
                r.text,
            )

    def reviews(self) -> None:
        check = self.checks.check
        r = self.client.post("/review", json=VALID, headers={"X-Request-ID": "e2e-post-1"})
        created = r.json()
        location = r.headers.get("location", "")
        check(
            "POST /review: 202 with Location",
            r.status_code == 202 and location == f"/review/{created['id']}",
            r.text,
        )
        check(
            "the new review is pending, without book data",
            created["status"] == "pending" and created["book"] is None,
        )
        check("the review ID is a UUIDv7", uuid.UUID(created["id"]).version == 7)
        check("the worker completes it: GET 200", self.completes(location, 90))
        completed = self.client.get(location)
        book = completed.json()["book"] or {}
        check(
            "the completed review carries the book data",
            completed.json()["status"] == "completed"
            and book.get("title") == "Pride and Prejudice"
            and book.get("cover_url")
            and book.get("authors")
            and book.get("subjects")
            and book.get("languages") == ["en"],
            completed.text[:300],
        )
        check("no Retry-After once completed", "retry-after" not in completed.headers)
        traced = wait_until(
            lambda: [
                line
                for line in messages("worker", "review processed")
                if line.get("request_id") == "e2e-post-1"
            ],
            10,
        )
        check("the worker's log line carries the request ID of the POST", traced)
        r = self.client.put(
            location, json={"review": "  Even better the second time. ", "score": 10}
        )
        updated = r.json() if r.status_code == 200 else {}
        check(
            "PUT: 200, text trimmed and score updated",
            updated.get("review") == "Even better the second time." and updated.get("score") == 10,
            r.text,
        )
        check(
            "PUT keeps the status and the book data",
            updated.get("status") == "completed" and updated.get("book"),
        )
        check(
            "PUT moves updated_at forward",
            updated.get("updated_at", "") > updated.get("created_at", "~"),
        )
        invalid: list[tuple[Json, str]] = [
            ({"review": "Fine."}, "body.score"),
            ({"score": 5}, "body.review"),
            ({"review": "Fine.", "score": 5, "id": 84}, "body.id"),
        ]
        for payload, field in invalid:
            r = self.client.put(location, json=payload)
            check(f"PUT {payload}: 422 on {field}", fields(r) == [field], r.text)
        r = self.client.get("/review/not-a-uuid")
        check("GET a malformed ID: 422 on path.review_id", fields(r) == ["path.review_id"], r.text)
        missing = uuid.uuid7()
        r = self.client.get(f"/review/{missing}")
        check(
            "GET an unknown review: 404 problem",
            r.status_code == 404 and r.json()["detail"] == f"no review with id {missing}",
            r.text,
        )
        r = self.client.delete(location)
        check("DELETE: 204 with an empty body", r.status_code == 204 and r.content == b"", r.text)
        check("GET after DELETE: 404", self.status_of(location) == 404)
        check("DELETE again: 404", self.client.delete(location).status_code == 404)
        r = self.client.put(location, json={"review": "Gone.", "score": 1})
        check("PUT after DELETE: 404", r.status_code == 404)

    def validation(self) -> None:
        check = self.checks.check
        r = self.submit(id="1342")
        check(
            "POST with the book ID as a numeric string: 202",
            r.status_code == 202 and r.json()["book_id"] == 1342,
            r.text,
        )
        invalid: list[tuple[Json, str]] = [
            ({"id": None}, "body.id"),
            ({"id": 0}, "body.id"),
            ({"id": ""}, "body.id"),
            ({"score": 0}, "body.score"),
            ({"score": 11}, "body.score"),
            ({"score": 6.5}, "body.score"),
            ({"review": "ok"}, "body.review"),
            ({"review": "x" * 5001}, "body.review"),
            ({"review": "bell\u0007"}, "body.review"),
            ({"rating": 5}, "body.rating"),
        ]
        for change, field in invalid:
            r = self.submit(**change)
            check(
                f"POST {shorten(change)}: 422 on {field}",
                r.status_code == 422 and is_problem(r) and fields(r) == [field],
                r.text,
            )
        r = self.client.post("/review", json={})
        check(
            "POST {}: 422 listing id, review and score",
            sorted(fields(r)) == ["body.id", "body.review", "body.score"],
            r.text,
        )
        r = self.client.post(
            "/review", content=b"not json", headers={"content-type": "application/json"}
        )
        check("POST a malformed JSON body: 422 problem", r.status_code == 422 and is_problem(r))
        r = self.submit(review="x" * 5000)
        check("POST a 5000-character review (the limit): 202", r.status_code == 202, r.text[:200])
        text = "Riga uno.\n\tRiga due, con emoji \U0001f4da e accenti àèìòù — ünïcödé."
        r = self.submit(review=text)
        check(
            "POST with line breaks, tab, emoji and accents: 202, text unchanged",
            r.status_code == 202 and r.json()["review"] == text,
            r.text,
        )
        if r.status_code == 202:
            stored = self.client.get(r.headers["location"]).json()["review"]
            check("the same text comes back from the database", stored == text)
        start = time.monotonic()
        r = self.submit(id=99999999)
        elapsed = time.monotonic() - start
        if r.status_code == 504:
            self.checks.skip("unknown book", f"Gutendex timed out after {elapsed:.0f}s")
        else:
            check(
                f"POST for a book not in the catalog: 422 on body.id ({elapsed:.2f}s)",
                r.status_code == 422
                and r.json()["errors"]
                == [{"field": "body.id", "message": "no book with this id in the catalog"}],
                r.text,
            )

    def worker_stopped(self) -> None:
        check = self.checks.check
        compose("stop", "worker")
        try:
            held = self.submit(review="Written while the worker is down.").headers["location"]
            time.sleep(2)
            r = self.client.get(held)
            check(
                "with the worker stopped the review stays pending: 202 with Retry-After: 5",
                r.status_code == 202 and r.headers.get("retry-after") == "5",
                r.text,
            )
        finally:
            compose("start", "worker")
        check("once the worker restarts, the review is completed", self.completes(held, 60))

    def rabbitmq_down(self) -> None:
        check = self.checks.check
        compose("stop", "rabbitmq")
        try:
            start = time.monotonic()
            r = self.submit(review="Written while RabbitMQ is down.")
            elapsed = time.monotonic() - start
            check(f"POST still answers 202 ({elapsed:.2f}s)", r.status_code == 202, r.text)
            orphan_id, orphan = r.json()["id"], r.headers["location"]
            warned = wait_until(lambda: self.not_queued(orphan_id), 10)
            check("the API logs that the review could not be queued", warned)
            check("the review is pending while RabbitMQ is down", self.status_of(orphan) == 202)
            retries = len(messages("worker", "RabbitMQ not reachable, retrying"))
            compose("restart", "--no-deps", "worker")
            waiting = wait_until(
                lambda: len(messages("worker", "RabbitMQ not reachable, retrying")) > retries, 30
            )
            state = compose("ps", "--format", "{{.State}}", "worker")
            check(
                "a worker started while RabbitMQ is down waits for it",
                waiting and "running" in state,
                state,
            )
        finally:
            compose("up", "--detach", "--wait", "rabbitmq")
        check(
            "RabbitMQ back: the worker connects and completes the review",
            self.completes(orphan, 180),
        )

        def published() -> str | None:
            r = self.submit(review="Written after RabbitMQ came back.")
            time.sleep(1)
            return None if self.not_queued(r.json()["id"]) else r.headers["location"]

        location = wait_until(published, 30, interval=2)
        check("the API publishes again by itself, without a restart", location)
        check(
            "...and the review it published is completed",
            location is not None and self.completes(location, 60),
        )

    def gutendex_down_for_the_worker(self) -> None:
        check = self.checks.check
        compose("stop", "worker")
        compose(
            "run",
            "--detach",
            "--no-deps",
            "--name",
            "e2e-bad-worker",
            *environment(
                GUTENDEX_BASE_URL="http://127.0.0.1:9",
                ENRICHMENT_MAX_ATTEMPTS="2",
                SWEEP_AFTER="600",
            ),
            "worker",
        )
        try:
            r = self.submit(review="Enriched after an outage.")
            stuck_id, stuck = r.json()["id"], r.headers["location"]

            def failures() -> list[Json]:
                return [
                    line
                    for line in container_logs("e2e-bad-worker")
                    if line.get("review_id") == stuck_id
                    and line.get("msg", "").startswith("enrichment failed")
                ]

            wait_until(lambda: len(failures()) == 2, 120, interval=1)
            attempts = [(line["attempt"], line["level"]) for line in failures()]
            check(
                "attempt 1 fails and is retried, attempt 2 gives up",
                attempts == [(1, "WARNING"), (2, "ERROR")],
                attempts,
            )
            times = [datetime.fromisoformat(line["time"]) for line in failures()]
            if len(times) == 2:
                gap = (times[1] - times[0]).total_seconds()
                check(f"the retry came after the 30 s delay ({gap:.1f}s)", 29 <= gap <= 40, gap)
            check(
                "the review is still pending after the last attempt", self.status_of(stuck) == 202
            )
        finally:
            run("docker", "rm", "--force", "e2e-bad-worker")
            compose("start", "worker")
        check(
            "the regular worker's sweeper picks the review up and completes it",
            self.completes(stuck, 90),
        )

    def circuit_breaker(self) -> None:
        check = self.checks.check
        compose(
            "run",
            "--detach",
            "--no-deps",
            "--name",
            "e2e-bad-api",
            "--publish",
            "127.0.0.1:8081:8080",
            *environment(
                GUTENDEX_BASE_URL="http://127.0.0.1:9",
                GUTENDEX_FAILURE_THRESHOLD="3",
                GUTENDEX_RESET_TIMEOUT="5",
            ),
            "api",
        )
        try:
            bad = httpx.Client(base_url="http://localhost:8081", timeout=30)
            check(
                "a second API instance with an unreachable Gutendex starts",
                wait_until(lambda: responds(bad, "/readyz"), 60),
            )
            codes = [bad.get("/book/search", params={"q": "austen"}).status_code for _ in range(3)]
            check("the first 3 searches reach Gutendex and fail: 502", codes == [502] * 3, codes)
            start = time.monotonic()
            r = bad.get("/book/search", params={"q": "austen"})
            elapsed = time.monotonic() - start
            check(
                f"then the circuit is open: 503 with Retry-After, at once ({elapsed:.3f}s)",
                r.status_code == 503
                and is_problem(r)
                and 1 <= int(r.headers.get("retry-after", "0")) <= 5
                and elapsed < 0.5,
                (r.status_code, r.headers.get("retry-after"), r.text),
            )
            r = bad.post("/review", json=VALID, headers={"X-API-Key": API_KEY})
            check(
                "POST /review with the circuit open: 503 with Retry-After",
                r.status_code == 503 and "retry-after" in r.headers,
                r.text,
            )
            opened = [
                line
                for line in container_logs("e2e-bad-api")
                if line.get("msg") == "catalog circuit opened"
            ]
            check(
                "the API logs 'catalog circuit opened' once, as a warning",
                len(opened) == 1 and opened[0]["level"] == "WARNING" and opened[0]["failures"] == 3,
                opened,
            )
            time.sleep(5.5)
            r = bad.get("/book/search", params={"q": "austen"})
            check("after the reset timeout one probe reaches Gutendex: 502", r.status_code == 502)
            r = bad.get("/book/search", params={"q": "austen"})
            check("the failed probe opens the circuit again: 503", r.status_code == 503)
            r = self.client.get("/book/search", params={"q": "pride prejudice"})
            check("the main API instance is not affected: 200", r.status_code == 200, r.text[:200])
        finally:
            run("docker", "rm", "--force", "e2e-bad-api")

    def parked_messages(self) -> None:
        check = self.checks.check
        rabbit = httpx.Client(
            base_url="http://localhost:15672/api", auth=("user", "password"), timeout=10
        )
        r = rabbit.post(
            "/exchanges/%2F/amq.default/publish",
            json={
                "properties": {},
                "routing_key": "review.enrichment",
                "payload": "not json",
                "payload_encoding": "string",
            },
        )
        check("a malformed message is published", r.json().get("routed") is True, r.text)

        def parked() -> list[Json]:
            got = rabbit.post(
                "/queues/%2F/review.enrichment.parked/get",
                json={"count": 1, "ackmode": "ack_requeue_false", "encoding": "auto"},
            )
            return got.json() if got.status_code == 200 else []

        message = wait_until(parked, 20) or [{}]
        reason = message[0].get("properties", {}).get("headers", {}).get("x-parked-reason", "")
        check(
            "the worker moves it to review.enrichment.parked with the reason",
            message[0].get("payload") == "not json"
            and reason.startswith("not an enrichment request"),
            message,
        )
        logged = messages("worker", "malformed message parked")
        check("the worker logs it as an error", logged and logged[-1]["level"] == "ERROR")
        queue = rabbit.get("/queues/%2F/review.enrichment").json()
        check("the main queue is empty again", queue.get("messages_ready", 0) == 0, queue)

    def database_down(self) -> None:
        check = self.checks.check
        probe = self.submit(review="Read while the database is down.").headers["location"]
        compose("stop", "db")
        try:
            r = self.client.get("/readyz")
            check("readyz: 503 problem", r.status_code == 503 and is_problem(r), r.text)
            check("healthz stays 200 (liveness only)", self.status_of("/healthz") == 200)
            r = self.client.get(probe)
            check(
                "reading a review: 503 with Retry-After: 5 and no internals",
                r.status_code == 503
                and r.headers.get("retry-after") == "5"
                and "aiomysql" not in r.text,
                r.text,
            )
            r = self.submit(review="Written while the database is down.")
            check(
                "submitting a review: 503 with Retry-After: 5",
                r.status_code == 503 and r.headers.get("retry-after") == "5",
                r.text,
            )
            logged = messages("api", "database unavailable")
            check(
                "the API logs 'database unavailable' with the driver's error",
                logged and "OperationalError" in logged[-1].get("error", ""),
                logged[-1:],
            )
        finally:
            compose("up", "--detach", "--wait", "db")
        check(
            "database back: readyz 200 again",
            wait_until(lambda: self.status_of("/readyz") == 200, 60),
        )
        check("database back: the review is readable again", self.status_of(probe) in {200, 202})

    def retry_failed(self) -> None:
        check = self.checks.check
        compose("stop", "worker")
        try:
            r = self.submit(review="Failed after a long Gutendex outage.")
            review_id, location = r.json()["id"], r.headers["location"]
            since = r.json()["created_at"]
            sql(f"UPDATE bookreviews.reviews SET status = 'failed' WHERE id = '{review_id}'")
            r = self.client.get(location)
            check("a failed review answers 200 with its status", r.json()["status"] == "failed")
            admin = ("exec", "-T", "api", "bookreviews-admin", "retry-failed", "--since", since)
            listed = compose(*admin, "--dry-run")
            check(
                "bookreviews-admin retry-failed --dry-run lists it",
                f"{review_id}\n1 failed reviews would be retried" in listed,
                listed,
            )
            summary = compose(*admin)
            check(
                "bookreviews-admin retry-failed enriches it",
                "1 failed reviews retried: 1 completed" in summary,
                summary,
            )
            r = self.client.get(location)
            check(
                "...and the review is completed with the book data",
                r.json()["status"] == "completed"
                and r.json()["book"]["title"] == "Pride and Prejudice",
                r.text[:300],
            )
        finally:
            compose("start", "worker")

    def load(self) -> None:
        check = self.checks.check

        def post(number: int) -> httpx.Response:
            body = VALID | {"review": f"Burst review number {number}."}
            return httpx.post(
                f"{API_URL}/review", json=body, headers={"X-API-Key": API_KEY}, timeout=60
            )

        with ThreadPoolExecutor(max_workers=10) as pool:
            burst = list(pool.map(post, range(30)))
        codes = [r.status_code for r in burst]
        check("30 concurrent POSTs: all 202", codes == [202] * 30, codes)
        locations = [r.headers["location"] for r in burst]
        check(
            "all 30 completed by the worker",
            wait_until(lambda: all(self.status_of(loc) == 200 for loc in locations), 120, 1),
        )
        check(
            "each review kept its own text",
            all(
                self.client.get(loc).json()["review"] == f"Burst review number {number}."
                for number, loc in enumerate(locations)
            ),
        )

    def metrics(self) -> None:
        check = self.checks.check
        r = httpx.get("http://localhost:9100/metrics", timeout=10)
        check(
            "the API serves Prometheus metrics on port 9100, by route template",
            'route="/review/{review_id}"' in r.text
            and "bookreviews_reviews_submitted_total" in r.text,
            r.text[:300],
        )
        check("metrics are not served on the public port", self.status_of("/metrics") == 404)
        r = httpx.get("http://localhost:9101/healthz", timeout=10)
        check("the worker answers its health check", r.json() == {"status": "ok"}, r.text)
        r = httpx.get("http://localhost:9101/metrics", timeout=10)
        check(
            "the worker counts completed enrichments",
            re.search(r'bookreviews_enrichments_total\{outcome="completed"\} [1-9]', r.text),
            r.text[:300],
        )
        check(
            "the worker exports the backlog gauges",
            "bookreviews_pending_reviews " in r.text
            and "bookreviews_oldest_pending_review_age_seconds " in r.text,
        )
        prometheus = httpx.Client(base_url="http://localhost:9090/api/v1", timeout=10)

        def scraped() -> bool:
            targets = prometheus.get("/targets").json()["data"]["activeTargets"]
            healthy = {t["labels"]["job"] for t in targets if t["health"] == "up"}
            return healthy >= {"bookreviews-api", "bookreviews-worker"}

        check("Prometheus scrapes the API and the worker", wait_until(scraped, 90, interval=2))
        groups = prometheus.get("/rules").json()["data"]["groups"]
        check("Prometheus loaded the 8 alert rules", sum(len(g["rules"]) for g in groups) == 8)
        alerts = prometheus.get("/alerts").json()["data"]["alerts"]
        firing = [a["labels"]["alertname"] for a in alerts if a["state"] == "firing"]
        check("no alert is firing on the healthy stack", not firing, firing)
        dashboard = json.loads(
            (PROJECT / "deploy/observability/grafana/bookreviews.json").read_text(encoding="utf-8")
        )
        queries = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
        answers = {
            query: prometheus.get(
                "/query", params={"query": query.replace("$__rate_interval", "2m")}
            ).json()
            for query in queries
        }
        invalid = [query for query, answer in answers.items() if answer.get("status") != "success"]
        check(f"all {len(queries)} dashboard queries are valid PromQL", not invalid, invalid)
        with_data = [q for q, answer in answers.items() if answer.get("data", {}).get("result")]
        check(
            "the main dashboard queries have data",
            all(any(metric in query for query in with_data) for metric in DASHBOARD_METRICS),
            [query for query in queries if query not in with_data],
        )
        r = httpx.get("http://localhost:3000/api/dashboards/uid/bookreviews", timeout=10)
        check(
            "Grafana provisioned the dashboard",
            r.status_code == 200 and r.json()["dashboard"]["title"] == "Book reviews",
            r.text[:200],
        )

    def traces(self) -> None:
        check = self.checks.check
        jaeger = httpx.Client(base_url="http://localhost:16686/api/v3", timeout=20)
        services = {"bookreviews-api", "bookreviews-worker"}

        def reporting() -> bool:
            return services <= set(jaeger.get("/services").json().get("services") or [])

        def service_of(resource: Json) -> str:
            attributes = resource["resource"]["attributes"]
            name: str = next(
                a["value"]["stringValue"] for a in attributes if a["key"] == "service.name"
            )
            return name

        def joined_trace() -> tuple[str, list[Json]] | None:
            now = datetime.now(UTC)
            found = jaeger.get(
                "/traces",
                params={
                    "query.service_name": "bookreviews-api",
                    "query.operation_name": "POST /review",
                    "query.start_time_min": rfc3339(now - timedelta(hours=1)),
                    "query.start_time_max": rfc3339(now + timedelta(minutes=1)),
                    "query.search_depth": 50,
                },
            ).json()
            trace_ids = {
                span["traceId"]
                for resource in found.get("result", {}).get("resourceSpans", [])
                for scope in resource["scopeSpans"]
                for span in scope.get("spans", [])
            }
            for trace_id in trace_ids:
                resources = jaeger.get(f"/traces/{trace_id}").json()["result"]["resourceSpans"]
                if services <= {service_of(resource) for resource in resources}:
                    return trace_id, resources
            return None

        check("Jaeger received traces from the API and the worker", wait_until(reporting, 60, 2))
        joined = wait_until(joined_trace, 60, interval=2)
        check("a POST /review trace continues in the worker", joined)
        if joined is None:
            return
        trace_id, resources = joined
        spans = [
            (service_of(resource), span)
            for resource in resources
            for scope in resource["scopeSpans"]
            for span in scope.get("spans", [])
        ]
        kinds = {span.get("kind") for _, span in spans}
        check("the trace has server, producer and consumer spans", {2, 4, 5} <= kinds, kinds)
        statements = {
            (service, span["name"])
            for service, span in spans
            if any(a["key"] == "db.statement" for a in span.get("attributes", []))
        }
        check(
            "the trace has the API's INSERT and the worker's UPDATE",
            {("bookreviews-api", "INSERT"), ("bookreviews-worker", "UPDATE")} <= statements,
            statements,
        )
        check(
            "the worker's log lines carry the same trace ID",
            any(
                line.get("trace_id") == trace_id for line in messages("worker", "review processed")
            ),
            trace_id,
        )

    def shutdown(self) -> None:
        check = self.checks.check
        stopped = len(messages("worker", "worker stopped"))
        compose("stop", "worker", "api")
        check(
            "the worker logs a clean stop", len(messages("worker", "worker stopped")) == stopped + 1
        )
        check(
            "the API logs a clean shutdown",
            messages("api", "Application shutdown complete."),
        )
        errors = Counter(
            line["msg"]
            for service in ("api", "worker")
            for line in logs(service)
            if line.get("level") == "ERROR"
        )
        print("\nERROR log lines, by message:")
        for message, count in sorted(errors.items()):
            print(f"  {count:3} x {message}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start a fresh Docker Compose stack, check it end to end, then remove it."
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the stack running after the checks"
    )
    arguments = parser.parse_args(argv)
    checks = Checks()
    stack = Stack(checks)
    print("Starting the stack", flush=True)
    try:
        compose("up", "--build", "--detach", "--wait")
        checks.run(
            [
                ("Health, documentation, conventions", stack.conventions),
                ("Authentication and ownership", stack.authentication),
                ("Hardening: database accounts, limits, headers", stack.hardening),
                ("Safe writes: Idempotency-Key, ETag, If-Match, If-None-Match", stack.safe_writes),
                ("Book search", stack.search),
                ("Reviews: create, read, update, delete", stack.reviews),
                ("Reviews: validation", stack.validation),
                ("Resilience: worker stopped", stack.worker_stopped),
                ("Resilience: RabbitMQ down", stack.rabbitmq_down),
                (
                    "Resilience: Gutendex unreachable for the worker",
                    stack.gutendex_down_for_the_worker,
                ),
                ("Resilience: Gutendex unreachable for the API", stack.circuit_breaker),
                ("Resilience: a malformed message is parked", stack.parked_messages),
                ("Resilience: database down", stack.database_down),
                ("Operations: retrying failed reviews", stack.retry_failed),
                ("Load: 30 concurrent reviews", stack.load),
                ("Observability: metrics, dashboard, alerts", stack.metrics),
                ("Observability: traces", stack.traces),
                ("Graceful shutdown", stack.shutdown),
            ]
        )
    finally:
        if not arguments.keep:
            compose("down", "--volumes", "--remove-orphans")
    return checks.report()


if __name__ == "__main__":
    sys.exit(main())
