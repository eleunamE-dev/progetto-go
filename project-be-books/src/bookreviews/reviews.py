import unicodedata
import uuid
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Self

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from bookreviews.books import get_catalog
from bookreviews.catalog import Book, BookCatalog, BookNotFoundError
from bookreviews.problems import CATALOG_ERROR_RESPONSES, ProblemDetails
from bookreviews.review_service import (
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

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    HTTPStatus.NOT_FOUND: {"model": ProblemDetails, "description": "No such review"}
}


@router.post("", status_code=HTTPStatus.ACCEPTED, responses=CATALOG_ERROR_RESPONSES)
async def submit_review(
    submission: ReviewSubmission, service: Service, response: Response
) -> ReviewResponse:
    try:
        review = await service.submit(submission.id, submission.review, submission.score)
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
    return ReviewResponse.from_review(review)


@router.get(
    "/{review_id}",
    responses={
        HTTPStatus.ACCEPTED: {
            "model": ReviewResponse,
            "description": "The review is saved and still being processed",
        },
        **NOT_FOUND_RESPONSE,
    },
)
async def get_review(review_id: uuid.UUID, service: Service, response: Response) -> ReviewResponse:
    review = await service.get(review_id)
    if review.status is ReviewStatus.PENDING:
        response.status_code = HTTPStatus.ACCEPTED
        response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return ReviewResponse.from_review(review)


@router.put("/{review_id}", responses=NOT_FOUND_RESPONSE)
async def update_review(
    review_id: uuid.UUID, changes: ReviewChanges, service: Service
) -> ReviewResponse:
    review = await service.update(review_id, changes.review, changes.score)
    return ReviewResponse.from_review(review)


@router.delete("/{review_id}", status_code=HTTPStatus.NO_CONTENT, responses=NOT_FOUND_RESPONSE)
async def delete_review(review_id: uuid.UUID, service: Service) -> None:
    await service.delete(review_id)
