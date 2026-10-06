import json
from pathlib import Path

import pytest

from bookreviews.cli import openapi

COMMITTED = Path(__file__).parents[3] / "docs" / "openapi.json"


def test_the_committed_openapi_document_is_up_to_date(tmp_path: Path) -> None:
    exported = tmp_path / "openapi.json"

    openapi.export(exported)

    assert exported.read_bytes() == COMMITTED.read_bytes(), (
        "the API changed: run `make openapi` and commit docs/openapi.json"
    )


def test_main_writes_to_the_given_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "nested" / "twice" / "openapi.json"
    monkeypatch.setattr("sys.argv", ["bookreviews-openapi", str(target)])

    openapi.main()

    assert json.loads(target.read_text(encoding="utf-8"))["info"]["title"] == "Book reviews"


def test_main_writes_to_docs_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["bookreviews-openapi"])

    openapi.main()

    document = json.loads((tmp_path / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert document["info"]["title"] == "Book reviews"
