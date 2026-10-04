import hashlib
import re
import unicodedata
import uuid
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Self

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from bookreviews.auth import Client
from bookreviews.books import get_catalog
from bookreviews.catalog import Book, BookCatalog, BookNotFoundError
from bookreviews.problems import (
    AUTHENTICATION_RESPONSES,
    BODY_TOO_LARGE_RESPONSES,
    CATALOG_ERROR_RESPONSES,
    OWNERSHIP_RESPONSES,
    PRECONDITION_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSES,
    VALIDATION_ERROR_RESPONSES,
    ProblemDetails,
)
from bookreviews.review_service import (
    Condition,
    Review,
    ReviewQueue,
    ReviewRepository,
    ReviewService,
    ReviewStatus,
)

MIN_REVIEW_LENGTH = 3
MAX_REVIEW_LENGTH = 5000
MIN_SCORE = 1
MAX_SCORE = 10
RETRY_AFTER_SECONDS = 5
REVALIDATE = "no-cache"
IDEMPOTENCY_KEY_PATTERN = r'^"?[A-Za-z0-9._:-]{1,255}"?$'

_ENTITY_TAG = re.compile(r'(W/)?("[^"]*")')

_ALLOWED_CONTROL_CHARACTERS = frozenset("\n\r\t")

router = APIRouter(prefix="/review", tags=["reviews"])


def _reject_control_characters(value: str) -> str:
    if any(unicodedata.category(c) == "Cc" and c not in _ALLOWED_CONTROL_CHARACTERS for c in value):
        raise ValueError("must not contain control characters")
    return value


ReviewText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=MIN_REVIEW_LENGTH, max_length=MAX_REVIEW_LENGTH
    ),
    AfterValidator(_reject_control_characters),
    Field(description="Text of the review; line breaks and tabs are allowed."),
]
Score = Annotated[int, Field(ge=MIN_SCORE, le=MAX_SCORE)]


class ReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[
        int, Field(gt=0, description="Gutenberg ID of the book, as returned by /book/search.")
    ]
    review: ReviewText
    score: Score


class ReviewChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review: ReviewText
    score: Score


class AuthorDetails(BaseModel):
    name: str
    birth_year: int | None
    death_year: int | None


class BookDetails(BaseModel):
    id: int
    title: str
    authors: list[AuthorDetails]
    subjects: list[str]
    bookshelves: list[str]
    languages: list[str]
    summaries: list[str]
    cover_url: str | None
    download_count: int

    @classmethod
    def from_book(cls, book: Book) -> Self:
        return cls(
            id=book.id,
            title=book.title,
            authors=[
                AuthorDetails(name=a.name, birth_year=a.birth_year, death_year=a.death_year)
                for a in book.authors
            ],
            subjects=list(book.subjects),
            bookshelves=list(book.bookshelves),
            languages=list(book.languages),
            summaries=list(book.summaries),
            cover_url=book.cover_url,
            download_count=book.download_count,
        )


class ReviewResponse(BaseModel):
    id: uuid.UUID
    status: ReviewStatus
    book_id: int
    review: str
    score: int
    created_at: datetime
    updated_at: datetime
    book: BookDetails | None = Field(
        description="Book data from the catalog, available once the review is completed."
    )

    @classmethod
    def from_review(cls, review: Review) -> Self:
        return cls(
            id=review.id,
            status=review.status,
            book_id=review.book_id,
            review=review.content,
            score=review.score,
            created_at=review.created_at,
            updated_at=review.updated_at,
            book=None if review.book is None else BookDetails.from_book(review.book),
        )


def entity_tag(review: Review) -> str:
    representation = ReviewResponse.from_review(review).model_dump_json()
    return f'"{hashlib.sha256(representation.encode()).hexdigest()[:32]}"'


def tag_matches(header: str, tag: str, *, weak: bool) -> bool:
    if header.strip() == "*":
        return True
    return any(
        listed == tag and (weak or not prefix) for prefix, listed in _ENTITY_TAG.findall(header)
    )


def precondition(if_match: str | None) -> Condition | None:
    if if_match is None:
        return None
    return lambda review: tag_matches(if_match, entity_tag(review), weak=False)


IfMatch = Annotated[
    str | None,
    Header(
        description="Entity tag from an earlier response: the review is changed only if it "
        "still has it, otherwise the answer is 412."
    ),
]
IfNoneMatch = Annotated[
    str | None,
    Header(description="Entity tags the client already has: 304 if the review still has one."),
]
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description="Unique value chosen by the client, such as a UUID. Repeating the request "
        "with the same key returns the review created the first time instead of a new one. "
        "Keys are remembered for a limited time, 24 hours by default.",
    ),
]

ENTITY_TAG_HEADERS: dict[str, Any] = {
    "ETag": {
        "description": "Entity tag of the review, for If-Match and If-None-Match",
        "schema": {"type": "string"},
    }
}


def get_review_repository(request: Request) -> ReviewRepository:
    repository: ReviewRepository = request.app.state.reviews
    return repository


def get_review_queue(request: Request) -> ReviewQueue:
    queue: ReviewQueue = request.app.state.queue
    return queue


def get_review_service(
    repository: Annotated[ReviewRepository, Depends(get_review_repository)],
    catalog: Annotated[BookCatalog, Depends(get_catalog)],
    queue: Annotated[ReviewQueue, Depends(get_review_queue)],
) -> ReviewService:
    return ReviewService(repository, catalog, queue)


Service = Annotated[ReviewService, Depends(get_review_service)]

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
