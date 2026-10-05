import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from bookreviews.adapters.gutendex import GutendexClient
from bookreviews.core.catalog import (
    Book,
    BookNotFoundError,
    CatalogTimeoutError,
    CatalogUnavailableError,
    PageOutOfRangeError,
    Person,
    SearchResult,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gutendex"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@dataclass
class FakeGutendex:
    status: int = 200
    body: bytes = b"{}"
    error: Exception | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, content=self.body)


@pytest.fixture
def gutendex() -> FakeGutendex:
    return FakeGutendex()


@pytest.fixture
async def client(gutendex: FakeGutendex) -> AsyncIterator[GutendexClient]:
    transport = httpx.MockTransport(gutendex.handle)
    async with GutendexClient("https://gutendex.test", 5, transport=transport) as client:
        yield client


async def test_search_sends_a_normalized_query(
    client: GutendexClient, gutendex: FakeGutendex
) -> None:
    gutendex.body = fixture("search_first_page.json")

    await client.search("Charles DICKENS")

    [request] = gutendex.requests
    assert str(request.url) == "https://gutendex.test/books/?search=charles+dickens"
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"].startswith("bookreviews")


async def test_search_requests_later_pages(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.body = fixture("search_first_page.json")

    await client.search("dickens", page=3)

    [request] = gutendex.requests
    assert str(request.url) == "https://gutendex.test/books/?page=3&search=dickens"


async def test_search_maps_the_results(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.body = fixture("search_first_page.json")

    result = await client.search("dickens")

    assert result.total == 231
    assert result.has_next
    assert [book.id for book in result.books] == [98, 47530]
    first = result.books[0]
    assert first.title == "A Tale of Two Cities"
    assert first.authors == (Person("Dickens, Charles", 1812, 1870),)
    assert first.languages == ("en",)
    assert first.cover_url == "https://www.gutenberg.org/cache/epub/98/pg98.cover.medium.jpg"
    assert first.download_count == 50704


async def test_search_last_page(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.body = fixture("search_last_page.json")

    result = await client.search("pride prejudice")

    assert result.total == 6
    assert not result.has_next
    assert result.books[1].authors == (
        Person("Austen, Jane", 1775, 1817),
        Person("MacKaye, Steele, Mrs.", 1845, 1924),
    )


async def test_search_without_matches(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.body = b'{"count": 0, "next": null, "previous": null, "results": []}'

    result = await client.search("zzqxjvkwy")

    assert result == SearchResult(total=0, books=(), has_next=False)


async def test_search_page_out_of_range(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.status, gutendex.body = 404, b'{"detail": "Invalid page."}'

    with pytest.raises(PageOutOfRangeError):
        await client.search("pride prejudice", page=2)


async def test_get_book(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.body = fixture("book.json")

    book = await client.get_book(1342)

    [request] = gutendex.requests
    assert str(request.url) == "https://gutendex.test/books/1342/"
    assert book == Book(
        id=1342,
        title="Pride and Prejudice",
        authors=(Person("Austen, Jane", 1775, 1817),),
        subjects=(
            "Courtship -- Fiction",
            "Domestic fiction",
            "England -- Fiction",
            "Love stories",
            "Sisters -- Fiction",
            "Social classes -- Fiction",
            "Young women -- Fiction",
        ),
        bookshelves=(
            "Best Books Ever Listings",
            "Category: British Literature",
            "Category: Classics of Literature",
            "Category: Novels",
            "Category: Romance",
            "Harvard Classics",
        ),
        languages=("en",),
        summaries=('"Pride and Prejudice" by Jane Austen is a novel published in 1813.',),
        cover_url="https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
        download_count=190246,
    )


async def test_get_book_without_cover(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.body = b'{"id": 1, "title": "Untitled", "formats": {"text/plain": "https://x/1.txt"}}'

    book = await client.get_book(1)

    assert book == Book(id=1, title="Untitled")


async def test_get_unknown_book(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.status, gutendex.body = 404, b'{"detail": "No Book matches the given query."}'

    with pytest.raises(BookNotFoundError) as excinfo:
        await client.get_book(999999999)

    assert excinfo.value.book_id == 999999999


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (503, b"upstream down", "returned status 503"),
        (200, b'{"id": ', "returned an invalid body"),
        (200, b'{"title": "no id"}', "returned an invalid body"),
    ],
)
async def test_unusable_responses(
    client: GutendexClient, gutendex: FakeGutendex, status: int, body: bytes, message: str
) -> None:
    gutendex.status, gutendex.body = status, body

    with pytest.raises(CatalogUnavailableError, match=message) as excinfo:
        await client.get_book(1342)

    assert not isinstance(excinfo.value, CatalogTimeoutError)


async def test_timeout(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.error = httpx.ReadTimeout("timed out")

    with pytest.raises(CatalogTimeoutError, match="timed out"):
        await client.search("dickens")


async def test_connection_error(client: GutendexClient, gutendex: FakeGutendex) -> None:
    gutendex.error = httpx.ConnectError("connection refused")

    with pytest.raises(CatalogUnavailableError, match="connection refused") as excinfo:
        await client.search("dickens")

    assert not isinstance(excinfo.value, CatalogTimeoutError)


async def test_close() -> None:
    client = GutendexClient("https://gutendex.test", 5)
    assert not client.is_closed

    await client.aclose()

    assert client.is_closed


@pytest.mark.skipif(
    not os.environ.get("GUTENDEX_LIVE_TEST"),
    reason="set GUTENDEX_LIVE_TEST=1 to run against https://gutendex.com",
)
async def test_live_gutendex() -> None:
    async with GutendexClient("https://gutendex.com", 120) as client:
        book = await client.get_book(1342)
        result = await client.search("pride prejudice")
        with pytest.raises(BookNotFoundError):
            await client.get_book(999999999)

    assert book.title == "Pride and Prejudice"
    assert book.cover_url
    assert 1342 in [b.id for b in result.books]
