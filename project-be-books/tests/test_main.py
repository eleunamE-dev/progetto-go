import asyncio
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
    upgraded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        main, "upgrade_database", lambda url, lock_timeout: upgraded.append((url, lock_timeout))
    )
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://app:secret@db:3306/reviews")

    main.run_migrations([])
    main.run_migrations(["--timeout", "30"])

    assert upgraded == [
        ("mysql+aiomysql://app:secret@db:3306/reviews", 600),
        ("mysql+aiomysql://app:secret@db:3306/reviews", 30),
    ]


def test_run_migrations_can_wait_for_the_schema_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    waited: list[str] = []

    async def wait_for_schema(url: str) -> None:
        waited.append(url)

    monkeypatch.setattr(main, "wait_for_schema", wait_for_schema)
    monkeypatch.setattr(
        main, "upgrade_database", lambda *_args, **_kwargs: pytest.fail("the schema was changed")
    )
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://app:secret@db:3306/reviews")

    main.run_migrations(["--wait"])

    assert waited == ["mysql+aiomysql://app:secret@db:3306/reviews"]


def test_waiting_for_the_schema_gives_up_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_ready(_url: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "wait_for_schema", never_ready)
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)

    with pytest.raises(SystemExit, match="still not up to date after 0 s"):
        main.run_migrations(["--wait", "--timeout", "0.05"])


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
