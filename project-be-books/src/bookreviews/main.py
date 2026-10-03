import uvicorn

from bookreviews.app import create_app
from bookreviews.config import Settings
from bookreviews.logs import configure_logging


def run_api() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=settings.http_shutdown_timeout,
    )
