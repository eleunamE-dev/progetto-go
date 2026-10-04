from pathlib import Path
from typing import Any

import yaml

PROJECT = Path(__file__).resolve().parents[1]
KUBERNETES = PROJECT / "deploy" / "kubernetes"


def documents(path: Path) -> list[Any]:
    return list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


def test_kubernetes_loads_the_alert_rules_tested_with_promtool() -> None:
    [rule] = documents(KUBERNETES / "components" / "monitoring" / "prometheusrule.yaml")
    [alerts] = documents(PROJECT / "deploy" / "observability" / "alerts.yml")

    assert rule["spec"] == alerts


def test_kind_runs_the_mariadb_and_rabbitmq_versions_of_docker_compose() -> None:
    [compose] = documents(PROJECT / "docker-compose.yaml")
    overlay = KUBERNETES / "overlays" / "local"
    workloads = [
        document
        for name in ("mariadb.yaml", "rabbitmq.yaml")
        for document in documents(overlay / name)
        if document["kind"] in {"Deployment", "StatefulSet"}
    ]

    images = {
        container["image"]
        for workload in workloads
        for container in workload["spec"]["template"]["spec"]["containers"]
    }

    assert images == {compose["services"]["db"]["image"], compose["services"]["rabbitmq"]["image"]}
