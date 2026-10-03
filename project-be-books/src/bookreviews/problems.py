import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from bookreviews.catalog import CatalogTimeoutError, CatalogUnavailableError
from bookreviews.logs import request_id_var
from bookreviews.review_service import ReviewNotFoundError

PROBLEM_JSON = "application/problem+json"

logger = logging.getLogger("bookreviews.catalog")


class FieldError(BaseModel):
    field: str
    message: str


class ProblemDetails(BaseModel):
    title: str
    status: int
    detail: str | None = None
    instance: str
    request_id: str | None = None
    errors: list[FieldError] | None = None


VALIDATION_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNPROCESSABLE_CONTENT: {
        "model": ProblemDetails,
        "description": "The request is not valid; `errors` lists the invalid fields",
    },
}

CATALOG_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.BAD_GATEWAY: {"model": ProblemDetails, "description": "The book catalog failed"},
    HTTPStatus.GATEWAY_TIMEOUT: {
        "model": ProblemDetails,
        "description": "The book catalog did not answer in time",
    },
}


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


async def _catalog_unavailable(request: Request, exc: CatalogUnavailableError) -> JSONResponse:
    logger.error(
        "book catalog request failed", extra={"error": str(exc), "cause": repr(exc.__cause__)}
    )
    if isinstance(exc, CatalogTimeoutError):
        return problem_response(
            HTTPStatus.GATEWAY_TIMEOUT,
            request.url.path,
            "the book catalog did not answer in time, try again later",
        )
    return problem_response(
        HTTPStatus.BAD_GATEWAY,
        request.url.path,
        "the book catalog is not available, try again later",
    )


async def _review_not_found(request: Request, exc: ReviewNotFoundError) -> JSONResponse:
    return problem_response(
        HTTPStatus.NOT_FOUND, request.url.path, f"no review with id {exc.review_id}"
    )


def register_problem_handlers(app: FastAPI) -> None:
    app.exception_handler(StarletteHTTPException)(_http_exception)
    app.exception_handler(RequestValidationError)(_validation_error)
    app.exception_handler(CatalogUnavailableError)(_catalog_unavailable)
    app.exception_handler(ReviewNotFoundError)(_review_not_found)
