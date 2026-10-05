import sys

import pytest

from bookreviews.api.auth import ApiKeys, key_digest
from bookreviews.cli import api_key


def test_the_command_prints_a_new_key_and_its_entry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["bookreviews-api-key", "web-app"])

    api_key.main()

    lines = capsys.readouterr().out.splitlines()
    key = lines[1].strip()
    assert key.startswith("bkr_")
    assert lines[3].strip() == f'"web-app": "{key_digest(key)}"'
    assert ApiKeys({"web-app": [key_digest(key)]}).client_for(key) == "web-app"


@pytest.mark.parametrize("arguments", [[], ["Web App"], ["-web"], ["web", "app"], ["a" * 64]])
def test_the_command_needs_one_valid_client_name(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["bookreviews-api-key", *arguments])

    with pytest.raises(SystemExit, match="usage: bookreviews-api-key CLIENT"):
        api_key.main()
