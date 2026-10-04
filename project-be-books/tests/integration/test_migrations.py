import asyncio
import threading
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from bookreviews.database import migrations_config, upgrade_database, wait_for_schema
from tests.conftest import LogRecords

pytestmark = pytest.mark.integration


def test_the_models_match_the_migrations(database_url: str) -> None:
    command.check(migrations_config(database_url))


def test_the_migrations_can_be_rolled_back(database_url: str) -> None:
    config = migrations_config(database_url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")


def run_sql(database_url: str, statement: str, **params: object) -> list[Any]:
    async def execute() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), params)
                return list(result.scalars()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def test_reviews_written_before_api_keys_belong_to_anonymous(database_url: str) -> None:
    config = migrations_config(database_url)
    command.downgrade(config, "0002")
    try:
        run_sql(
            database_url,
            "INSERT INTO reviews (id, book_id, content, score, status, created_at, updated_at, "
            "queued_at) VALUES (UUID(), 1342, 'Old.', 7, 'pending', NOW(6), NOW(6), NOW(6))",
        )
    finally:
        command.upgrade(config, "head")
    try:
        assert run_sql(database_url, "SELECT owner FROM reviews") == ["anonymous"]
    finally:
        run_sql(database_url, "DELETE FROM reviews")


def test_one_migration_runs_at_a_time(database_url: str) -> None:
    lock = f"migrations.{make_url(database_url).database}"
    holding = threading.Event()
    release = threading.Event()

    async def hold_the_lock() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT GET_LOCK(:lock, 0)"), {"lock": lock})
                holding.set()
                await asyncio.to_thread(release.wait, 30)
        finally:
            await engine.dispose()

    holder = threading.Thread(target=asyncio.run, args=(hold_the_lock(),))
    holder.start()
    try:
        assert holding.wait(10)
        with pytest.raises(RuntimeError, match="another migration held the lock"):
            upgrade_database(database_url, lock_timeout=1)
    finally:
        release.set()
        holder.join(10)

    upgrade_database(database_url, lock_timeout=1)
    assert run_sql(database_url, "SELECT IS_FREE_LOCK(:lock)", lock=lock) == [1]


def test_waiting_for_the_schema(database_url: str, json_logs: LogRecords) -> None:
    config = migrations_config(database_url)
    command.downgrade(config, "0003")

    async def wait_while_migrating() -> None:
        waiting = asyncio.create_task(wait_for_schema(database_url, interval=0.1))
        await asyncio.sleep(0.5)
        assert not waiting.done()
        await asyncio.to_thread(command.upgrade, config, "head")
        await asyncio.wait_for(waiting, timeout=10)

    asyncio.run(wait_while_migrating())

    messages = [r["msg"] for r in json_logs() if r["logger"] == "bookreviews.database"]
    assert "waiting for the database schema" in messages
    assert messages[-1] == "database schema up to date"


def test_a_release_rolled_back_runs_on_the_newer_schema(
    database_url: str, json_logs: LogRecords
) -> None:
    head = ScriptDirectory.from_config(migrations_config(database_url)).get_current_head()
    run_sql(database_url, "UPDATE alembic_version SET version_num = '9999'")
    try:
        upgrade_database(database_url, lock_timeout=1)
        asyncio.run(asyncio.wait_for(wait_for_schema(database_url, interval=0.1), timeout=10))
        assert run_sql(database_url, "SELECT version_num FROM alembic_version") == ["9999"]
    finally:
        run_sql(database_url, "UPDATE alembic_version SET version_num = :head", head=head)

    assert [r["msg"] for r in json_logs() if r["logger"] == "bookreviews.database"] == [
        "database schema newer than this release, nothing to migrate",
        "database schema newer than this release",
    ]
