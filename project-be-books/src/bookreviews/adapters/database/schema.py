import asyncio
import logging
from importlib.resources import files

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bookreviews.adapters.database.connections import CONNECT_TIMEOUT

MIGRATION_LOCK_TIMEOUT = 600
logger = logging.getLogger("bookreviews.database")


def migrations_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("bookreviews.adapters") / "migrations"))
    config.attributes["database_url"] = database_url
    return config


def upgrade_database(database_url: str, lock_timeout: int = MIGRATION_LOCK_TIMEOUT) -> None:
    config = migrations_config(database_url)
    config.attributes["lock_timeout"] = lock_timeout
    command.upgrade(config, "head")


def newer_than_release(scripts: ScriptDirectory, revision: str | None) -> bool:
    return revision is not None and all(s.revision != revision for s in scripts.walk_revisions())


async def wait_for_schema(database_url: str, interval: float = 2) -> None:
    scripts = ScriptDirectory.from_config(migrations_config(database_url))
    head = scripts.get_current_head()
    engine = create_async_engine(database_url, connect_args={"connect_timeout": CONNECT_TIMEOUT})
    try:
        while (current := await _schema_revision(engine)) != head:
            if newer_than_release(scripts, current):
                logger.warning(
                    "database schema newer than this release",
                    extra={"current": current, "head": head},
                )
                return
            logger.info("waiting for the database schema", extra={"current": current, "head": head})
            await asyncio.sleep(interval)
    finally:
        await engine.dispose()
    logger.info("database schema up to date", extra={"head": head})


async def _schema_revision(engine: AsyncEngine) -> str | None:
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync: MigrationContext.configure(sync).get_current_revision()
            )
    except (SQLAlchemyError, OSError) as exc:
        logger.info("database not reachable yet", extra={"error": f"{type(exc).__name__}: {exc}"})
        return None
