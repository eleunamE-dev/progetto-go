from typing import Any

import pytest

from bookreviews.adapters.database import connections
from bookreviews.config import Settings

URL = "mysql+aiomysql://app:secret@db:3306/reviews"


def engine_options(monkeypatch: pytest.MonkeyPatch, **pool: Any) -> dict[str, Any]:
    created: list[dict[str, Any]] = []

    def create_async_engine(url: str, **options: Any) -> object:
        created.append({"url": url, **options})
        return object()

    monkeypatch.setattr(connections, "create_async_engine", create_async_engine)
    connections.create_engine(URL, **pool)
    [options] = created
    return options


def test_the_engine_replaces_stale_connections_and_stops_connecting_after_5_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = engine_options(monkeypatch, pool_size=3, max_overflow=4, pool_timeout=7)

    assert options == {
        "url": URL,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_size": 3,
        "max_overflow": 4,
        "pool_timeout": 7,
        "connect_args": {"connect_timeout": 5},
    }


def test_the_default_pool_is_the_one_of_the_default_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = engine_options(monkeypatch)
    fields = Settings.model_fields

    assert (options["pool_size"], options["max_overflow"], options["pool_timeout"]) == (
        fields["database_pool_size"].default,
        fields["database_max_overflow"].default,
        fields["database_pool_timeout"].default,
    )


def test_sessions_keep_their_objects_loaded_after_a_commit() -> None:
    session = connections.create_sessions(connections.create_engine(URL))()

    assert session.sync_session.expire_on_commit is False
