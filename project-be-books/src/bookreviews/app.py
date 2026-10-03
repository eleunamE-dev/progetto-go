from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bookreviews import books
from bookreviews.config import Settings
from bookreviews.gutendex import GutendexClient
from bookreviews.middleware import RequestContextMiddleware
from bookreviews.problems import register_problem_handlers


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with GutendexClient(
            str(settings.gutendex_base_url), settings.gutendex_timeout
        ) as catalog:
            app.state.catalog = catalog
            yield

    app = FastAPI(title="Book reviews", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    register_problem_handlers(app)
    app.include_router(books.router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
