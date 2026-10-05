from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, ValidationError

from bookreviews.core.catalog import (
    Book,
    BookNotFoundError,
    CatalogTimeoutError,
    CatalogUnavailableError,
    PageOutOfRangeError,
    Person,
    SearchResult,
)

USER_AGENT = "bookreviews (+https://github.com/eleunamE-dev/progetto-go)"
CONNECT_TIMEOUT = 5.0


class _Person(BaseModel):
    name: str
    birth_year: int | None = None
    death_year: int | None = None


class _Book(BaseModel):
    id: int
    title: str
    authors: list[_Person] = []
    summaries: list[str] = []
    subjects: list[str] = []
    bookshelves: list[str] = []
    languages: list[str] = []
    formats: dict[str, str] = {}
    download_count: int = 0

    def to_domain(self) -> Book:
        return Book(
            id=self.id,
            title=self.title,
            authors=tuple(Person(a.name, a.birth_year, a.death_year) for a in self.authors),
            subjects=tuple(self.subjects),
            bookshelves=tuple(self.bookshelves),
            languages=tuple(self.languages),
            summaries=tuple(self.summaries),
            cover_url=self.formats.get("image/jpeg"),
            download_count=self.download_count,
        )


class _BookList(BaseModel):
    count: int
    next: str | None = None
    results: list[_Book]


class _NotFoundError(Exception):
    pass


class GutendexClient:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT)),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    @property
    def is_closed(self) -> bool:
        return self._http.is_closed

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search(self, query: str, page: int = 1) -> SearchResult:
        params = {"page": str(page)} if page > 1 else {}
        params["search"] = query.lower()
        try:
            found = await self._get("books/", _BookList, params)
        except _NotFoundError:
            raise PageOutOfRangeError(page) from None
        return SearchResult(
            total=found.count,
            books=tuple(book.to_domain() for book in found.results),
            has_next=found.next is not None,
        )

    async def get_book(self, book_id: int) -> Book:
        try:
            book = await self._get(f"books/{book_id}/", _Book)
        except _NotFoundError:
            raise BookNotFoundError(book_id) from None
        return book.to_domain()

    async def _get[M: BaseModel](
        self, path: str, model: type[M], params: dict[str, str] | None = None
    ) -> M:
        try:
            response = await self._http.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise CatalogTimeoutError(f"Gutendex GET /{path} timed out") from exc
        except httpx.HTTPError as exc:
            raise CatalogUnavailableError(f"Gutendex GET /{path} failed: {exc!r}") from exc

        if response.status_code == httpx.codes.NOT_FOUND:
            raise _NotFoundError
        if response.status_code != httpx.codes.OK:
            raise CatalogUnavailableError(
                f"Gutendex GET /{path} returned status {response.status_code}"
            )
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise CatalogUnavailableError(f"Gutendex GET /{path} returned an invalid body") from exc
