import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]


def test_every_alert_links_to_its_section_of_the_runbook() -> None:
    alerts = (PROJECT / "deploy" / "observability" / "alerts.yml").read_text(encoding="utf-8")
    runbook = (PROJECT / "docs" / "runbook.md").read_text(encoding="utf-8")

    names = re.findall(r"- alert: (\w+)", alerts)
    links = re.findall(r"runbook_url: \S+/project-be-books/docs/runbook\.md#(\S+)", alerts)
    sections = re.findall(r"^### (\w+)$", runbook, flags=re.MULTILINE)

    assert links == [name.lower() for name in names]
    assert set(names) <= set(sections)
