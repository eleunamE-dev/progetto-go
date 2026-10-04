import asyncio
import contextlib

import uvicorn

from bookreviews import worker
from bookreviews.app import create_app
from bookreviews.config import Settings
from bookreviews.database import upgrade_database
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


def run_migrations() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    upgrade_database(settings.database_url.get_secret_value())


def run_worker() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    with tracing(settings, "bookreviews-worker"), contextlib.suppress(KeyboardInterrupt):
        asyncio.run(worker.main(settings))
