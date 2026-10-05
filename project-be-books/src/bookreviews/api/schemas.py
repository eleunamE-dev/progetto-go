import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Self

from fastapi import Header
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)
from pydantic_core import PydanticCustomError

from bookreviews.core.catalog import Book
from bookreviews.core.reviews import Review, ReviewStatus

MIN_REVIEW_LENGTH = 3
MAX_REVIEW_LENGTH = 5000
MIN_SCORE = 1
MAX_SCORE = 10
IDEMPOTENCY_KEY_PATTERN = r'^"?[A-Za-z0-9._:-]{1,255}"?$'
_ALLOWED_CONTROL_CHARACTERS = frozenset("\n\r\t")


def _reject_control_characters(value: str) -> str:
    if any(unicodedata.category(c) == "Cc" and c not in _ALLOWED_CONTROL_CHARACTERS for c in value):
        raise ValueError("must not contain control characters")
    return value


def _reject_booleans_and_decimals(value: object) -> object:
    if isinstance(value, bool | float):
        raise PydanticCustomError(
            "whole_number", "Input should be a whole number or a string of digits"
        )
    return value


ReviewText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=MIN_REVIEW_LENGTH, max_length=MAX_REVIEW_LENGTH
    ),
    AfterValidator(_reject_control_characters),
    Field(description="Text of the review; line breaks and tabs are allowed."),
]
BookId = Annotated[
    int,
    Field(gt=0, description="Gutenberg ID of the book, as returned by /book/search."),
    BeforeValidator(_reject_booleans_and_decimals),
]
Score = Annotated[int, Field(ge=MIN_SCORE, le=MAX_SCORE, strict=True)]


class ReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: BookId
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
