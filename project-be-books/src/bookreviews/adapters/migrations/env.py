import asyncio

from alembic import context
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine

from bookreviews.adapters.database import MIGRATION_LOCK_TIMEOUT, Base, logger, newer_than_release
from bookreviews.config import Settings


def database_url() -> str:
    url = context.config.attributes.get("database_url")
    if isinstance(url, str):
        return url
    return Settings().database_url.get_secret_value()


def run_migrations(connection: Connection) -> None:
    lock = f"migrations.{connection.engine.url.database}"[:64]
    timeout = context.config.attributes.get("lock_timeout", MIGRATION_LOCK_TIMEOUT)
    acquired = connection.scalar(
        text("SELECT GET_LOCK(:lock, :timeout)"), {"lock": lock, "timeout": timeout}
    )
    connection.commit()
    if acquired != 1:
        raise RuntimeError(f"another migration held the lock {lock!r} for more than {timeout} s")
    try:
        context.configure(connection=connection, target_metadata=Base.metadata)
        current = context.get_context().get_current_revision()
        if newer_than_release(context.script, current):
            logger.warning(
                "database schema newer than this release, nothing to migrate",
                extra={"current": current},
            )
            return
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text("SELECT RELEASE_LOCK(:lock)"), {"lock": lock})
        connection.commit()


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async_migrations())
