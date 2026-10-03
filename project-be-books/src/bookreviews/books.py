from http import HTTPStatus
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AfterValidator, BaseModel

from bookreviews.catalog import Book, BookCatalog, PageOutOfRangeError
from bookreviews.problems import CATALOG_ERROR_RESPONSES, ProblemDetails

MAX_QUERY_LENGTH = 200

router = APIRouter(tags=["books"])


def get_catalog(request: Request) -> BookCatalog:
    catalog: BookCatalog = request.app.state.catalog
    return catalog


def _collapse_whitespace(value: str) -> str:
    words = value.split()
    if not words:
        raise ValueError("must contain at least one word")
    return " ".join(words)


SearchQuery = Annotated[
    str,
    Query(
        max_length=MAX_QUERY_LENGTH,
        description="Words to look for in book titles and author names.",
    ),
    AfterValidator(_collapse_whitespace),
]


class BookSummary(BaseModel):
    id: int
    title: str
    authors: list[str]
    languages: list[str]
    cover_url: str | None
    download_count: int

    @classmethod
    def from_book(cls, book: Book) -> Self:
        return cls(
            id=book.id,
            title=book.title,
            authors=[author.name for author in book.authors],
            languages=list(book.languages),
            cover_url=book.cover_url,
            download_count=book.download_count,
        )


class SearchResponse(BaseModel):
    count: int
    page: int
    next_page: int | None
    results: list[BookSummary]


@router.get(
    "/book/search",
    responses={
        HTTPStatus.NOT_FOUND: {"model": ProblemDetails, "description": "No such page of results"},
        **CATALOG_ERROR_RESPONSES,
    },
)
async def search_books(
    q: SearchQuery,
    catalog: Annotated[BookCatalog, Depends(get_catalog)],
    page: Annotated[int, Query(ge=1)] = 1,
) -> SearchResponse:
    try:
        result = await catalog.search(q, page)
    except PageOutOfRangeError:
        raise HTTPException(
            HTTPStatus.NOT_FOUND, f"page {page} does not exist for this search"
        ) from None
    return SearchResponse(
        count=result.total,
        page=page,
        next_page=page + 1 if result.has_next else None,
        results=[BookSummary.from_book(book) for book in result.books],
    )
