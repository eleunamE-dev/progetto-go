import json
from pathlib import Path

import pytest

from bookreviews.cli import openapi

COMMITTED = Path(__file__).parents[3] / "docs" / "openapi.json"


def test_the_committed_openapi_document_is_up_to_date(tmp_path: Path) -> None:
    exported = tmp_path / "openapi.json"

    openapi.export(exported)

    assert json.loads(exported.read_text(encoding="utf-8")) == json.loads(
        COMMITTED.read_text(encoding="utf-8")
    ), "the API changed: run `make openapi` and commit docs/openapi.json"


def test_main_writes_to_the_given_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "nested" / "openapi.json"
    monkeypatch.setattr("sys.argv", ["bookreviews-openapi", str(target)])

    openapi.main()

    assert json.loads(target.read_text(encoding="utf-8"))["info"]["title"] == "Book reviews"
