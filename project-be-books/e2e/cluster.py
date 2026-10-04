import base64
import json
import ssl
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import httpx

from e2e.checks import Checks, wait_until

NAMESPACE = "bookreviews"
GATEWAY_NAMESPACE = "envoy-gateway-system"
GATEWAY_SERVICE = "gateway.envoyproxy.io/owning-gateway-name=bookreviews"
HOST = "bookreviews.localtest.me"
HTTP_PORT = 18080
HTTPS_PORT = 18443
METRICS_PORT = 19100
PROMETHEUS_PORT = 19090
API_KEY = "local-dev-key"
API_PODS = "app.kubernetes.io/component=api"
WORKER_PODS = "app.kubernetes.io/component=worker"
IN_API = ("-n", NAMESPACE, "exec", "deploy/bookreviews-api", "-c", "api", "--")
UNKNOWN_REVIEW = "/review/00000000-0000-7000-8000-000000000000"
RATE_LIMIT_HEADERS = {"x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}
RUNBOOK = "https://github.com/eleunamE-dev/progetto-go/blob/main/project-be-books/docs/runbook.md#"
SCRAPED = {"bookreviews-api": 2, "bookreviews-worker": 2}
CONNECT = (
    "import socket, sys; "
    "socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=10); print('ok')"
)

type Json = dict[str, Any]


def attempt(*arguments: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def kubectl(*arguments: str, timeout: float = 180) -> str:
    finished = attempt(*arguments, timeout=timeout)
    if finished.returncode:
        raise RuntimeError(f"kubectl {' '.join(arguments)} failed: {finished.stderr[-800:]}")
    return finished.stdout


def get(*arguments: str) -> Json:
    found: Json = json.loads(kubectl("-n", NAMESPACE, "get", *arguments, "-o", "json"))
    return found


def running(selector: str) -> list[Json]:
    pods: list[Json] = get("pods", "-l", selector, "--field-selector=status.phase=Running")["items"]
    return [pod for pod in pods if "deletionTimestamp" not in pod["metadata"]]


def ready(pod: Json) -> bool:
    conditions = pod["status"].get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def name_of(pod: Json) -> str:
    name: str = pod["metadata"]["name"]
    return name


def logs_of(pod: Json, container: str) -> str:
    return kubectl("-n", NAMESPACE, "logs", name_of(pod), "-c", container)


def rollout_status(deployment: str, seconds: int) -> subprocess.CompletedProcess[str]:
    target = f"deployment/{deployment}"
    return attempt(
        "-n", NAMESPACE, "rollout", "status", target, f"--timeout={seconds}s", timeout=seconds + 30
    )


def mariadb(statement: str) -> str:
    client = ("mariadb", "-uroot", "-prootpassword", "-N", "-e", statement)
    return kubectl("-n", NAMESPACE, "exec", "mariadb-0", "--", *client).strip()


def gateway_certificate() -> ssl.SSLContext:
    path = "jsonpath={.data.tls\\.crt}"
    encoded = kubectl("-n", NAMESPACE, "get", "secret", "bookreviews-tls", "-o", path)
    return ssl.create_default_context(cadata=base64.b64decode(encoded).decode())


@contextmanager
def port_forward(namespace: str, target: str, *ports: str) -> Iterator[None]:
    process = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", target, *ports],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        process.terminate()
        process.wait(timeout=10)


def responds(client: httpx.Client, path: str) -> bool:
    try:
        return client.get(path).status_code < 500
    except httpx.TransportError:
        return False


def submit(gateway: httpx.Client, review: str) -> httpx.Response:
    return gateway.post(
        "/review",
        json={"id": 1342, "review": review, "score": 9},
        headers={"X-API-Key": API_KEY, "Idempotency-Key": f"kind-{time.time_ns()}"},
    )


def completes(gateway: httpx.Client, location: str, timeout: float) -> bool:
    def completed() -> bool:
        return gateway.get(location).status_code == 200

    return wait_until(completed, timeout, interval=2) is not None


class Cluster:
    def __init__(self, checks: Checks) -> None:
        self.checks = checks
        found = kubectl(
            "-n", GATEWAY_NAMESPACE, "get", "service", "-l", GATEWAY_SERVICE, "-o", "json"
        )
        service = json.loads(found)["items"][0]
        self.gateway_service = f"service/{service['metadata']['name']}"
        self.gateway_address: str = service["spec"]["clusterIP"]
        self.certificate = gateway_certificate()
        self.review = ""

    @contextmanager
    def gateway(self) -> Iterator[httpx.Client]:
        ports = (f"{HTTP_PORT}:80", f"{HTTPS_PORT}:443")
        with port_forward(GATEWAY_NAMESPACE, self.gateway_service, *ports):
            client = httpx.Client(
                base_url=f"https://{HOST}:{HTTPS_PORT}", verify=self.certificate, timeout=90
            )
            wait_until(lambda: responds(client, UNKNOWN_REVIEW), 60)
            yield client

    def workloads(self) -> None:
        check = self.checks.check
        api, workers = running(API_PODS), running(WORKER_PODS)
        check("2 API pods ready (the autoscaler's minimum)", sum(map(ready, api)) == 2)
        check("2 worker pods ready", sum(map(ready, workers)) == 2)
        services = running("app.kubernetes.io/name in (mariadb, rabbitmq)")
        check("MariaDB and RabbitMQ ready", len(services) == 2 and all(map(ready, services)))
        job = get("job", "bookreviews-migrate")
        check("the migration Job completed", job["status"].get("succeeded") == 1, job["status"])
        current = kubectl(*IN_API, "bookreviews-migrate", "--wait", "--timeout", "10")
        check("the schema is at the latest migration", "database schema up to date" in current)
        grants = mariadb("SHOW GRANTS FOR 'app'@'%'")
        check(
            "the app account only reads and writes rows",
            "SELECT, INSERT, UPDATE, DELETE" in grants and "ALL PRIVILEGES" not in grants,
            grants,
        )
        init = api[0]["status"].get("initContainerStatuses", [{}])[0]
        check(
            "API pods waited for the schema before starting",
            init.get("name") == "wait-for-schema"
            and init.get("state", {}).get("terminated", {}).get("exitCode") == 0,
            init,
        )
        waited = logs_of(api[0], "wait-for-schema")
        check("...and found it up to date", "database schema up to date" in waited, waited[-300:])
        nodes = {pod["spec"].get("nodeName") for pod in api}
        check("the two API pods run on different nodes", len(nodes) == 2, nodes)

    def monitoring(self) -> None:
        check = self.checks.check
        with port_forward("monitoring", "service/prometheus-operated", f"{PROMETHEUS_PORT}:9090"):
            prometheus = httpx.Client(base_url=f"http://127.0.0.1:{PROMETHEUS_PORT}", timeout=10)
            wait_until(lambda: responds(prometheus, "/-/ready"), 60)

            def jobs() -> Counter[str]:
                targets = prometheus.get("/api/v1/targets").json()["data"]["activeTargets"]
                return Counter(t["labels"]["job"] for t in targets if t["health"] == "up")

            check(
                "Prometheus scrapes the API and worker pods, under the jobs the alerts use",
                wait_until(lambda: jobs() == SCRAPED, 120, 5),
                jobs(),
            )
            groups = prometheus.get("/api/v1/rules").json()["data"]["groups"]
            rules = [rule for group in groups for rule in group["rules"]]
            check(
                "Prometheus loaded the 8 alert rules",
                len(rules) == 8 and all(rule["health"] == "ok" for rule in rules),
                [(rule["name"], rule["health"]) for rule in rules],
            )
            check(
                "...each linking to its section of the runbook",
                all(
                    rule["annotations"].get("runbook_url") == RUNBOOK + rule["name"].lower()
                    for rule in rules
                ),
            )
            alerts = prometheus.get("/api/v1/alerts").json()["data"]["alerts"]
            firing = [a["labels"]["alertname"] for a in alerts if a["state"] == "firing"]
            check("no alert is firing", not firing, firing)

    def pod_security(self) -> None:
        check = self.checks.check
        user = kubectl(*IN_API, "id", "-u").strip()
        check("the API runs as user 10001", user == "10001", user)
        written = attempt(*IN_API, "sh", "-c", "touch /app/probe")
        check(
            "the root filesystem is read-only",
            written.returncode != 0 and "Read-only" in written.stderr,
            written.stderr[-200:],
        )
        token = attempt(*IN_API, "ls", "/var/run/secrets/kubernetes.io")
        check("no service account token is mounted", token.returncode != 0, token.stdout)
        security = running(API_PODS)[0]["spec"]["containers"][0]["securityContext"]
        check(
            "no privilege escalation and no capabilities",
            security.get("allowPrivilegeEscalation") is False
            and security.get("capabilities", {}).get("drop") == ["ALL"],
            security,
        )

    def network_policies(self) -> None:
        check = self.checks.check
        probe = ("--image=busybox:1.37", "--", "wget", "-q", "-T", "5", "-O-")
        url = f"http://bookreviews-api.{NAMESPACE}/healthz"
        outside = attempt("run", "np-probe-outside", "--rm", "-i", "--restart=Never", *probe, url)
        check(
            "a pod in another namespace cannot reach the API",
            outside.returncode != 0 and '"ok"' not in outside.stdout,
            outside.stdout[-200:] + outside.stderr[-200:],
        )
        blocked = attempt(*IN_API, "python", "-c", CONNECT, "1.1.1.1", "80")
        check(
            "the API cannot open arbitrary outbound connections (port 80)",
            blocked.returncode != 0,
            blocked.stderr[-200:],
        )
        allowed = attempt(*IN_API, "python", "-c", CONNECT, "gutendex.com", "443")
        check(
            "...but can reach Gutendex over HTTPS",
            allowed.stdout.strip() == "ok",
            allowed.stderr[-200:],
        )

    def routes(self) -> None:
        with self.gateway() as gateway:
            self.routes_through(gateway)

    def routes_through(self, gateway: httpx.Client) -> None:
        check = self.checks.check
        r = httpx.get(f"http://{HOST}:{HTTP_PORT}/book/search?q=austen", timeout=10)
        check(
            "plain HTTP is redirected to HTTPS (301)",
            r.status_code == 301 and r.headers.get("location", "").startswith("https://"),
            (r.status_code, r.headers.get("location")),
        )
        r = gateway.get(UNKNOWN_REVIEW)
        check(
            "HTTPS through the gateway, with its certificate verified",
            r.status_code == 404,
            r.text[:200],
        )
        check(
            "HSTS is added by the gateway",
            r.headers.get("strict-transport-security", "").startswith("max-age=31536000"),
            dict(r.headers),
        )
        check(
            "the API's own security headers come through",
            r.headers.get("x-content-type-options") == "nosniff",
        )
        check(
            "health and metrics paths are not routed",
            gateway.get("/healthz").status_code == 404,
        )
        r = gateway.get("/book/search", params={"q": "pride prejudice"})
        if r.status_code == 504:
            self.checks.skip("search through the gateway", "Gutendex timed out")
        else:
            check("search through the gateway: 200", r.json().get("count", 0) > 0, r.text[:200])
        posted = submit(gateway, "Reviewed on Kubernetes.")
        check("POST /review through the gateway: 202", posted.status_code == 202, posted.text)
        self.review = posted.headers.get("location", "")
        check("the worker enriched the review", completes(gateway, self.review, 120))
        book = gateway.get(self.review).json().get("book") or {}
        check("...with the book data", book.get("title") == "Pride and Prejudice", book)

    def rate_limits(self) -> None:
        check = self.checks.check

        with self.gateway() as gateway, ThreadPoolExecutor(max_workers=40) as pool:
            responses = list(pool.map(lambda _: gateway.post("/review", json={}), range(120)))
        codes = [response.status_code for response in responses]
        limited = [response for response in responses if response.status_code == 429]
        check(
            f"bursts of writes are rate limited ({codes.count(429)} x 429, "
            f"{codes.count(401)} x 401)",
            limited and 401 in codes,
            sorted(set(codes)),
        )
        headers = dict(limited[0].headers) if limited else {}
        check(
            "limited requests carry the X-RateLimit headers",
            set(headers) >= RATE_LIMIT_HEADERS,
            headers,
        )

    def rabbitmq_at_start_up(self) -> None:
        check = self.checks.check
        kubectl("-n", NAMESPACE, "scale", "deployment/rabbitmq", "--replicas=0")
        try:
            gone = wait_until(lambda: not running("app.kubernetes.io/name=rabbitmq"), 120, 2)
            check("RabbitMQ is scaled to zero", gone)
            kubectl("-n", NAMESPACE, "rollout", "restart", "deployment/bookreviews-worker")
            rollout = rollout_status("bookreviews-worker", 180)
            check(
                "workers restarted while RabbitMQ is down start anyway",
                rollout.returncode == 0,
                rollout.stdout[-300:] + rollout.stderr[-300:],
            )

            def waiting() -> bool:
                logs = [logs_of(pod, "worker") for pod in running(WORKER_PODS)]
                return len(logs) == 2 and all("RabbitMQ not reachable" in log for log in logs)

            check("...and wait for it", wait_until(waiting, 60, 3))
        finally:
            kubectl("-n", NAMESPACE, "scale", "deployment/rabbitmq", "--replicas=1")
            rollout_status("rabbitmq", 180)

        def connected() -> bool:
            pods = running(WORKER_PODS)
            return len(pods) == 2 and all("worker started" in logs_of(p, "worker") for p in pods)

        check("once RabbitMQ is back, every worker connects", wait_until(connected, 120, 5))
        time.sleep(10)
        with self.gateway() as gateway:
            posted = submit(gateway, "Reviewed after RabbitMQ came back.")
            enriched = completes(gateway, posted.headers.get("location", ""), 120)
        check("...and a new review is enriched", enriched, posted.text[:200])

    def rollout(self) -> None:
        check = self.checks.check
        url = f"https://{HOST}{self.review}"
        request = (
            f"curl -sk -o /dev/null -w '%{{http_code}}' --max-time 10 "
            f"--resolve {HOST}:443:{self.gateway_address} {url}"
        )
        loop = (
            "end=$(( $(date +%s) + 75 )); ok=0; bad=0; "
            f"while [ $(date +%s) -lt $end ]; do code=$({request}); "
            'if [ "$code" = 200 ]; then ok=$((ok+1)); '
            'else bad=$((bad+1)); echo "failed with $code"; fi; sleep 0.1; done; '
            'echo "ok=$ok bad=$bad"'
        )
        image = "--image=curlimages/curl:8.16.0"
        kubectl("delete", "pod", "rollout-probe", "--ignore-not-found")
        kubectl(
            "run", "rollout-probe", "--restart=Never", image, "--command", "--", "sh", "-c", loop
        )
        try:
            kubectl("wait", "pod/rollout-probe", "--for=condition=Ready", "--timeout=120s")
            time.sleep(5)
            kubectl("-n", NAMESPACE, "rollout", "restart", "deployment/bookreviews-api")
            rollout_status("bookreviews-api", 300)
            finished = "--for=jsonpath={.status.phase}=Succeeded"
            kubectl("wait", "pod/rollout-probe", finished, "--timeout=180s", timeout=200)
            output = kubectl("logs", "rollout-probe").strip().splitlines()
        finally:
            kubectl("delete", "pod", "rollout-probe", "--ignore-not-found")
        totals = dict(part.split("=") for part in output[-1].split()) if output else {}
        check(
            f"no failed request while both API pods were replaced "
            f"({totals.get('ok', '?')} requests in 75 s)",
            totals.get("bad") == "0" and int(totals.get("ok", "0")) > 200,
            output[-5:],
        )

    def capacity(self) -> None:
        check = self.checks.check
        pod = running(API_PODS)[0]
        annotations = pod["metadata"].get("annotations", {})
        check(
            "pods carry the Prometheus scrape annotations",
            annotations.get("prometheus.io/scrape") == "true"
            and annotations.get("prometheus.io/port") == "9100",
            annotations,
        )
        with port_forward(NAMESPACE, f"pod/{name_of(pod)}", f"{METRICS_PORT}:9100"):
            metrics = httpx.Client(base_url=f"http://127.0.0.1:{METRICS_PORT}", timeout=5)
            served = wait_until(lambda: responds(metrics, "/metrics"), 30)
            text = metrics.get("/metrics").text if served else ""
        check("the API pod serves /metrics on port 9100", "bookreviews_http_requests_total" in text)
        hpa = get("hpa", "bookreviews-api")
        check(
            "the autoscaler keeps the API between 2 and 10 pods",
            hpa["spec"]["minReplicas"] == 2
            and hpa["spec"]["maxReplicas"] == 10
            and hpa["status"].get("currentReplicas") == 2,
            hpa["status"],
        )
        for name in ("bookreviews-api", "bookreviews-worker"):
            budget = get("pdb", name)["status"]
            check(
                f"{name}: the disruption budget lets one pod go at a time",
                budget.get("disruptionsAllowed") == 1,
                budget,
            )


def main() -> int:
    checks = Checks()
    cluster = Cluster(checks)
    checks.run(
        [
            ("Workloads", cluster.workloads),
            ("Monitoring", cluster.monitoring),
            ("Pod security", cluster.pod_security),
            ("Network policies", cluster.network_policies),
            ("Gateway", cluster.routes),
            ("Rate limits", cluster.rate_limits),
            ("RabbitMQ unavailable when the workers start", cluster.rabbitmq_at_start_up),
            ("Zero-downtime rollout", cluster.rollout),
            ("Metrics, autoscaling, disruption budgets", cluster.capacity),
        ]
    )
    return checks.report()


if __name__ == "__main__":
    sys.exit(main())
