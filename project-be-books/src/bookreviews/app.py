from fastapi import FastAPI

from bookreviews.middleware import RequestContextMiddleware
from bookreviews.problems import register_problem_handlers


def create_app() -> FastAPI:
    app = FastAPI(title="Book reviews", version="0.1.0")
    app.add_middleware(RequestContextMiddleware)
    register_problem_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
