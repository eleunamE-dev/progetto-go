from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from bookreviews import main


def test_run_api_starts_uvicorn_with_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    configured_levels: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setattr(main, "configure_logging", configured_levels.append)
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("HTTP_PORT", "9090")

    main.run_api()

    assert configured_levels == ["WARNING"]
    [(app, kwargs)] = calls
    assert isinstance(app, FastAPI)
    assert kwargs == {
        "host": "0.0.0.0",  # noqa: S104
        "port": 9090,
        "log_config": None,
        "access_log": False,
        "server_header": False,
        "timeout_graceful_shutdown": 15,
    }
