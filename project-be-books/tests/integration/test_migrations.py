import asyncio
import threading
from typing import Any

import pytest
from alembic import command
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from bookreviews.database import migrations_config, upgrade_database

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
