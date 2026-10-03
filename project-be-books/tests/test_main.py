from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from bookreviews import main, worker
from bookreviews.config import Settings


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


def test_run_migrations_upgrades_the_configured_database(monkeypatch: pytest.MonkeyPatch) -> None:
    upgraded: list[str] = []
    monkeypatch.setattr(main, "upgrade_database", upgraded.append)
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://app:secret@db:3306/reviews")

    main.run_migrations()

    assert upgraded == ["mysql+aiomysql://app:secret@db:3306/reviews"]


def test_run_worker_starts_the_worker_with_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Settings] = []

    async def fake_main(settings: Settings) -> None:
        started.append(settings)

    monkeypatch.setattr(worker, "main", fake_main)
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)
    monkeypatch.setenv("WORKER_CONCURRENCY", "2")

    main.run_worker()

    [settings] = started
    assert settings.worker_concurrency == 2
