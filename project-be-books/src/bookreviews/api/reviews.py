import uuid
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Response
from fastapi.exceptions import RequestValidationError

from bookreviews.api.auth import Client
from bookreviews.api.conditional import (
    ENTITY_TAG_HEADERS,
    IfMatch,
    IfNoneMatch,
    entity_tag,
    precondition,
    tag_matches,
)
from bookreviews.api.dependencies import Service
from bookreviews.api.problems import (
    AUTHENTICATION_RESPONSES,
    BODY_TOO_LARGE_RESPONSES,
    CATALOG_ERROR_RESPONSES,
    OWNERSHIP_RESPONSES,
    PRECONDITION_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSES,
    VALIDATION_ERROR_RESPONSES,
    ProblemDetails,
)
from bookreviews.api.schemas import (
    IdempotencyKeyHeader,
    ReviewChanges,
    ReviewResponse,
    ReviewSubmission,
)
from bookreviews.core.catalog import BookNotFoundError
from bookreviews.core.reviews import ReviewStatus

RETRY_AFTER_SECONDS = 5
REVALIDATE = "no-cache"
router = APIRouter(prefix="/review", tags=["reviews"])
REVIEW_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.NOT_FOUND: {"model": ProblemDetails, "description": "No such review"},
    **VALIDATION_ERROR_RESPONSES,
    **SERVICE_UNAVAILABLE_RESPONSES,
}


@router.post(
    "",
    status_code=HTTPStatus.ACCEPTED,
    responses={
        HTTPStatus.ACCEPTED: {"headers": ENTITY_TAG_HEADERS},
        **AUTHENTICATION_RESPONSES,
        **BODY_TOO_LARGE_RESPONSES,
        **VALIDATION_ERROR_RESPONSES,
        **CATALOG_ERROR_RESPONSES,
    },
)
async def submit_review(
    client: Client,
    submission: ReviewSubmission,
    service: Service,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> ReviewResponse:
    try:
        review = await service.submit(
            submission.id,
            submission.review,
            submission.score,
            owner=client,
            idempotency_key=None if idempotency_key is None else idempotency_key.strip('"'),
        )
    except BookNotFoundError:
        raise RequestValidationError(
            [
                {
                    "type": "book_not_found",
                    "loc": ("body", "id"),
                    "msg": "no book with this id in the catalog",
                    "input": submission.id,
                }
            ]
        ) from None
    response.headers["Location"] = f"/review/{review.id}"
    response.headers["ETag"] = entity_tag(review)
    return ReviewResponse.from_review(review)


@router.get(
    "/{review_id}",
    response_model=ReviewResponse,
    responses={
        HTTPStatus.OK: {"headers": ENTITY_TAG_HEADERS},
        HTTPStatus.ACCEPTED: {
            "model": ReviewResponse,
            "description": "The review is saved and still being processed",
            "headers": ENTITY_TAG_HEADERS,
        },
        HTTPStatus.NOT_MODIFIED: {
            "description": "The review still has one of the entity tags in If-None-Match"
        },
        **REVIEW_ERROR_RESPONSES,
    },
)
async def get_review(
    review_id: uuid.UUID, service: Service, response: Response, if_none_match: IfNoneMatch = None
) -> ReviewResponse | Response:
    review = await service.get(review_id)
    tag = entity_tag(review)
    headers = {"ETag": tag, "Cache-Control": REVALIDATE}
    if review.status is ReviewStatus.PENDING:
        headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    if if_none_match is not None and tag_matches(if_none_match, tag, weak=True):
        return Response(status_code=HTTPStatus.NOT_MODIFIED, headers=headers)
    response.headers.update(headers)
    if review.status is ReviewStatus.PENDING:
        response.status_code = HTTPStatus.ACCEPTED
    return ReviewResponse.from_review(review)


@router.put(
    "/{review_id}",
    responses={
        HTTPStatus.OK: {"headers": ENTITY_TAG_HEADERS},
        **AUTHENTICATION_RESPONSES,
        **OWNERSHIP_RESPONSES,
        **PRECONDITION_RESPONSES,
        **BODY_TOO_LARGE_RESPONSES,
        **REVIEW_ERROR_RESPONSES,
    },
)
async def update_review(
    client: Client,
    review_id: uuid.UUID,
    changes: ReviewChanges,
    service: Service,
    *,
    response: Response,
    if_match: IfMatch = None,
) -> ReviewResponse:
    review = await service.update(
        review_id, changes.review, changes.score, client, condition=precondition(if_match)
    )
    response.headers["ETag"] = entity_tag(review)
    return ReviewResponse.from_review(review)


@router.delete(
    "/{review_id}",
    status_code=HTTPStatus.NO_CONTENT,
    responses={
        **AUTHENTICATION_RESPONSES,
        **OWNERSHIP_RESPONSES,
        **PRECONDITION_RESPONSES,
        **REVIEW_ERROR_RESPONSES,
    },
)
async def delete_review(
    client: Client, review_id: uuid.UUID, service: Service, if_match: IfMatch = None
) -> None:
    await service.delete(review_id, client, condition=precondition(if_match))
