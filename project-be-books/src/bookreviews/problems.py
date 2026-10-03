from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from bookreviews.logs import request_id_var

PROBLEM_JSON = "application/problem+json"


def problem_response(
    status: int,
    instance: str,
    detail: str | None = None,
    *,
    errors: list[dict[str, str]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"title": HTTPStatus(status).phrase, "status": status}
    if detail:
        body["detail"] = detail
    body["instance"] = instance
    if (request_id := request_id_var.get()) is not None:
        body["request_id"] = request_id
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_JSON)


async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = None if exc.detail == HTTPStatus(exc.status_code).phrase else exc.detail
    return problem_response(exc.status_code, request.url.path, detail, headers=exc.headers)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
        for error in exc.errors()
    ]
    return problem_response(
        HTTPStatus.UNPROCESSABLE_CONTENT,
        request.url.path,
        "the request is not valid",
        errors=errors,
    )


def register_problem_handlers(app: FastAPI) -> None:
    app.exception_handler(StarletteHTTPException)(_http_exception)
    app.exception_handler(RequestValidationError)(_validation_error)
