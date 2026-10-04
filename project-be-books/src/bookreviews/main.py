import argparse
import asyncio
import contextlib
import sys
from collections.abc import Sequence

import uvicorn

from bookreviews import worker
from bookreviews.app import create_app
from bookreviews.config import Settings
from bookreviews.database import upgrade_database, wait_for_schema
from bookreviews.logs import configure_logging
from bookreviews.telemetry import tracing


def run_api() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    with tracing(settings, "bookreviews-api"):
        uvicorn.run(
            create_app(settings),
            host=settings.http_host,
            port=settings.http_port,
            log_config=None,
            access_log=False,
            server_header=False,
            timeout_graceful_shutdown=settings.http_shutdown_timeout,
        )


def run_migrations(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bookreviews-migrate",
        description="Apply the database migrations, or wait until they have been applied.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="only wait until the schema is up to date, without changing it",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="seconds to wait for the schema, or for another migration to finish",
    )
    arguments = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    url = settings.database_url.get_secret_value()
    if arguments.wait:
        asyncio.run(_wait_for_schema(url, arguments.timeout))
    else:
        upgrade_database(url, lock_timeout=int(arguments.timeout))


async def _wait_for_schema(url: str, seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            await wait_for_schema(url)
    except TimeoutError:
        sys.exit(f"the database schema is still not up to date after {seconds:.0f} s")


def run_worker() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    with tracing(settings, "bookreviews-worker"), contextlib.suppress(KeyboardInterrupt):
        asyncio.run(worker.main(settings))
