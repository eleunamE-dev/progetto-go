import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from bookreviews import books, reviews
from bookreviews.catalog import CachedCatalog
from bookreviews.config import Settings
from bookreviews.database import SqlReviewRepository, create_engine, create_sessions, ping
from bookreviews.gutendex import GutendexClient
from bookreviews.middleware import RequestContextMiddleware
from bookreviews.problems import ProblemDetails, register_problem_handlers

logger = logging.getLogger("bookreviews.health")


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url.get_secret_value())
        try:
            async with GutendexClient(
                str(settings.gutendex_base_url), settings.gutendex_timeout
            ) as gutendex:
                app.state.engine = engine
                app.state.catalog = CachedCatalog(
                    gutendex, ttl=settings.catalog_cache_ttl, max_books=settings.catalog_cache_size
                )
                app.state.reviews = SqlReviewRepository(create_sessions(engine))
                yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Book reviews", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
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
