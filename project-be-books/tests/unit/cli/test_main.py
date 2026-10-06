import asyncio
import contextlib
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from bookreviews.cli import main
from bookreviews.config import Settings
from bookreviews.worker import runner

type Tracing = Callable[[Settings, str], contextlib.AbstractContextManager[None]]


def recording(traced: list[tuple[str, Settings]]) -> Tracing:
    @contextlib.contextmanager
    def tracing(settings: Settings, service: str) -> Iterator[None]:
        traced.append((service, settings))
        yield

    return tracing


def test_run_api_starts_uvicorn_with_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    configured_levels: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    traced: list[tuple[str, Settings]] = []
    monkeypatch.setattr(main, "configure_logging", configured_levels.append)
    monkeypatch.setattr(main, "tracing", recording(traced))
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("HTTP_PORT", "9090")

    main.run_api()

    assert configured_levels == ["WARNING"]
    [(service, settings)] = traced
    assert (service, settings.http_port) == ("bookreviews-api", 9090)
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
    steps: list[tuple[str, str, int | None]] = []

    async def wait_for_database(url: str) -> None:
        steps.append(("waited", url, None))

    monkeypatch.setattr(main, "wait_for_database", wait_for_database)
    monkeypatch.setattr(
        main,
        "upgrade_database",
        lambda url, lock_timeout: steps.append(("upgraded", url, lock_timeout)),
    )
    configured_levels: list[str] = []
    monkeypatch.setattr(main, "configure_logging", configured_levels.append)
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://app:secret@db:3306/reviews")
    monkeypatch.setenv("LOG_LEVEL", "error")

    main.run_migrations([])
    main.run_migrations(["--timeout", "30"])

    assert configured_levels == ["ERROR", "ERROR"]

    url = "mysql+aiomysql://app:secret@db:3306/reviews"
    assert steps == [
        ("waited", url, None),
        ("upgraded", url, 600),
        ("waited", url, None),
        ("upgraded", url, 30),
    ]


def test_migrating_gives_up_when_the_database_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_reachable(_url: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "wait_for_database", never_reachable)
    monkeypatch.setattr(
        main,
        "upgrade_database",
        lambda *_args, **_kwargs: pytest.fail("migrated without a database"),
    )
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)

    with pytest.raises(SystemExit, match="still not reachable after 0 s"):
        main.run_migrations(["--timeout", "0.05"])


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

    configured_levels: list[str] = []
    traced: list[tuple[str, Settings]] = []
    monkeypatch.setattr(runner, "main", fake_main)
    monkeypatch.setattr(main, "configure_logging", configured_levels.append)
    monkeypatch.setattr(main, "tracing", recording(traced))
    monkeypatch.setenv("WORKER_CONCURRENCY", "2")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    main.run_worker()

    [settings] = started
    assert settings.worker_concurrency == 2
    assert configured_levels == ["DEBUG"]
    assert traced == [("bookreviews-worker", settings)]


def test_the_worker_stops_quietly_on_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    async def interrupted(_settings: Settings) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "main", interrupted)
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)

    main.run_worker()
