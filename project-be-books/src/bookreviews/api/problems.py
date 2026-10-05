import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import exc as sqlalchemy_errors
from starlette.exceptions import HTTPException as StarletteHTTPException

from bookreviews.core.catalog import (
    CatalogBusyError,
    CatalogCircuitOpenError,
    CatalogTimeoutError,
    CatalogUnavailableError,
)
from bookreviews.core.reviews import (
    IdempotencyKeyReusedError,
    PreconditionFailedError,
    ReviewForbiddenError,
    ReviewNotFoundError,
)
from bookreviews.observability.logs import request_id_var

PROBLEM_JSON = "application/problem+json"

DATABASE_RETRY_AFTER = 5

logger = logging.getLogger("bookreviews.catalog")
database_logger = logging.getLogger("bookreviews.database")


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

AUTHENTICATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED: {
        "model": ProblemDetails,
        "description": "The X-API-Key header is missing or not valid",
        "headers": {
            "WWW-Authenticate": {
                "description": "Always `ApiKey`",
                "schema": {"type": "string"},
            }
        },
    },
}

OWNERSHIP_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.FORBIDDEN: {
        "model": ProblemDetails,
        "description": "The review belongs to another client",
    },
}

PRECONDITION_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.PRECONDITION_FAILED: {
        "model": ProblemDetails,
        "description": "The review no longer has the entity tag given in If-Match",
    },
}

BODY_TOO_LARGE_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.CONTENT_TOO_LARGE: {
        "model": ProblemDetails,
        "description": "The request body is larger than the limit",
    },
}

SERVICE_UNAVAILABLE_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.SERVICE_UNAVAILABLE: {
        "model": ProblemDetails,
        "description": "A backing service is temporarily unavailable",
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before trying again",
                "schema": {"type": "integer"},
            }
        },
    },
}

CATALOG_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.BAD_GATEWAY: {"model": ProblemDetails, "description": "The book catalog failed"},
    **SERVICE_UNAVAILABLE_RESPONSES,
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
    if isinstance(exc, CatalogCircuitOpenError | CatalogBusyError):
        logger.info("book catalog request rejected", extra={"error": str(exc)})
        retry_after = exc.retry_after if isinstance(exc, CatalogCircuitOpenError) else 1
        return problem_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            request.url.path,
            "the book catalog is temporarily unavailable, try again later",
            headers={"Retry-After": str(int(retry_after))},
        )
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


async def _review_forbidden(request: Request, exc: ReviewForbiddenError) -> JSONResponse:
    return problem_response(
        HTTPStatus.FORBIDDEN,
        request.url.path,
        f"review {exc.review_id} belongs to another client",
    )


async def _precondition_failed(request: Request, exc: PreconditionFailedError) -> JSONResponse:
    return problem_response(
        HTTPStatus.PRECONDITION_FAILED,
        request.url.path,
        f"review {exc.review_id} has changed, read it again to get its current ETag",
    )


async def _idempotency_key_reused(request: Request, exc: IdempotencyKeyReusedError) -> JSONResponse:
    return problem_response(
        HTTPStatus.UNPROCESSABLE_CONTENT,
        request.url.path,
        "the request is not valid",
        errors=[
            {
                "field": "header.Idempotency-Key",
                "message": f"{exc.key} was already used for a different request",
            }
        ],
    )


async def _database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    cause = getattr(exc, "orig", None) or exc
    database_logger.error(
        "database unavailable", extra={"error": f"{type(cause).__name__}: {cause}"}
    )
    return problem_response(
        HTTPStatus.SERVICE_UNAVAILABLE,
        request.url.path,
        "the database is not available, try again later",
        headers={"Retry-After": str(DATABASE_RETRY_AFTER)},
    )


def register_problem_handlers(app: FastAPI) -> None:
    app.exception_handler(StarletteHTTPException)(_http_exception)
    app.exception_handler(RequestValidationError)(_validation_error)
    app.exception_handler(CatalogUnavailableError)(_catalog_unavailable)
    app.exception_handler(ReviewNotFoundError)(_review_not_found)
    app.exception_handler(ReviewForbiddenError)(_review_forbidden)
    app.exception_handler(PreconditionFailedError)(_precondition_failed)
    app.exception_handler(IdempotencyKeyReusedError)(_idempotency_key_reused)
    for error in (
        sqlalchemy_errors.OperationalError,
        sqlalchemy_errors.InterfaceError,
        sqlalchemy_errors.TimeoutError,
    ):
        app.exception_handler(error)(_database_unavailable)
