import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from bookreviews import books, reviews
from bookreviews.auth import API_KEY_HEADER, ApiKeys
from bookreviews.config import Settings
from bookreviews.database import SqlReviewRepository, create_sessions, ping
from bookreviews.gutendex import GutendexClient
from bookreviews.middleware import (
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from bookreviews.problems import ProblemDetails, register_problem_handlers
from bookreviews.queue import RabbitQueue
from bookreviews.wiring import build_catalog, build_engine

logger = logging.getLogger("bookreviews.health")
auth_logger = logging.getLogger("bookreviews.auth")


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not settings.api_keys:
            auth_logger.warning("no API keys configured, every write will be refused")
        engine = build_engine(settings)
        queue = RabbitQueue(settings.rabbitmq_url.get_secret_value())
        try:
            async with GutendexClient(
                str(settings.gutendex_base_url), settings.gutendex_timeout
            ) as gutendex:
                app.state.engine = engine
                app.state.catalog = build_catalog(gutendex, settings)
                app.state.reviews = SqlReviewRepository(create_sessions(engine))
                app.state.queue = queue
                yield
        finally:
            await queue.close()
            await engine.dispose()

    app = FastAPI(
        title="Book reviews",
        version="0.1.0",
        description=(
            "Search Project Gutenberg books and review them. Reviews are accepted right away "
            "and enriched in the background with the data of the book."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.api_docs_enabled else None,
        redoc_url="/redoc" if settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
    )
    app.state.api_keys = ApiKeys(settings.api_keys)
    app.add_middleware(BodySizeLimitMiddleware, max_size=settings.http_max_body_size)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Content-Type", API_KEY_HEADER, REQUEST_ID_HEADER],
            expose_headers=["Location", "Retry-After", REQUEST_ID_HEADER],
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    register_problem_handlers(app)
    app.include_router(books.router)
    app.include_router(reviews.router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/readyz",
        tags=["health"],
        responses={
            HTTPStatus.SERVICE_UNAVAILABLE: {
                "model": ProblemDetails,
                "description": "The database is not reachable",
            }
        },
    )
    async def readyz(request: Request) -> dict[str, str]:
        try:
            await ping(request.app.state.engine)
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("database not reachable", extra={"error": repr(exc)})
            raise HTTPException(
                HTTPStatus.SERVICE_UNAVAILABLE, "the database is not reachable"
            ) from exc
        return {"status": "ok"}

    return app
