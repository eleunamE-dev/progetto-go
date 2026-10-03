import httpx
import pytest
from fastapi import FastAPI, Request

from bookreviews.app import create_app
from bookreviews.books import get_catalog
from bookreviews.catalog import (
    Book,
    CatalogTimeoutError,
    CatalogUnavailableError,
    PageOutOfRangeError,
    Person,
    SearchResult,
)
from bookreviews.config import Settings
from tests.conftest import LogRecords
from tests.fakes import FakeCatalog

TALE_OF_TWO_CITIES = Book(
    id=98,
    title="A Tale of Two Cities",
    authors=(Person("Dickens, Charles", 1812, 1870),),
    subjects=("Historical fiction",),
    languages=("en",),
    cover_url="https://www.gutenberg.org/cache/epub/98/pg98.cover.medium.jpg",
    download_count=50704,
)


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog()


@pytest.fixture
def app(catalog: FakeCatalog) -> FastAPI:
    app = create_app(Settings())
    app.dependency_overrides[get_catalog] = lambda: catalog
    return app


async def test_returns_the_matching_books(client: httpx.AsyncClient, catalog: FakeCatalog) -> None:
    catalog.result = SearchResult(
        total=231, books=(TALE_OF_TWO_CITIES, Book(id=1, title="Untitled")), has_next=True
    )

    response = await client.get("/book/search", params={"q": "dickens"})

    assert response.status_code == 200
    assert response.json() == {
        "count": 231,
        "page": 1,
        "next_page": 2,
        "results": [
            {
                "id": 98,
                "title": "A Tale of Two Cities",
                "authors": ["Dickens, Charles"],
                "languages": ["en"],
                "cover_url": "https://www.gutenberg.org/cache/epub/98/pg98.cover.medium.jpg",
                "download_count": 50704,
            },
            {
                "id": 1,
                "title": "Untitled",
                "authors": [],
                "languages": [],
                "cover_url": None,
                "download_count": 0,
            },
        ],
    }
    assert catalog.searches == [("dickens", 1)]


async def test_last_page_has_no_next_page(client: httpx.AsyncClient, catalog: FakeCatalog) -> None:
    catalog.result = SearchResult(total=70, books=(TALE_OF_TWO_CITIES,), has_next=False)

    response = await client.get("/book/search", params={"q": "dickens", "page": 3})

    assert response.status_code == 200
    assert response.json()["page"] == 3
    assert response.json()["next_page"] is None
    assert catalog.searches == [("dickens", 3)]


async def test_no_matches(client: httpx.AsyncClient) -> None:
    response = await client.get("/book/search", params={"q": "zzqxjvkwy"})

    assert response.status_code == 200
    assert response.json() == {"count": 0, "page": 1, "next_page": None, "results": []}


async def test_collapses_whitespace_in_the_query(
    client: httpx.AsyncClient, catalog: FakeCatalog
) -> None:
    await client.get("/book/search", params={"q": "  Pride \t and   Prejudice "})

    assert catalog.searches == [("Pride and Prejudice", 1)]


async def test_page_out_of_range(client: httpx.AsyncClient, catalog: FakeCatalog) -> None:
    catalog.error = PageOutOfRangeError(9)

    response = await client.get("/book/search", params={"q": "dickens", "page": 9})

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "page 9 does not exist for this search"


@pytest.mark.parametrize(
    ("query_string", "field", "message"),
    [
        ("", "query.q", "Field required"),
        ("q=%20%20", "query.q", "Value error, must contain at least one word"),
        (f"q={'a' * 201}", "query.q", "String should have at most 200 characters"),
        ("q=dickens&page=0", "query.page", "Input should be greater than or equal to 1"),
        (
            "q=dickens&page=two",
            "query.page",
            "Input should be a valid integer, unable to parse string as an integer",
        ),
    ],
)
async def test_rejects_invalid_parameters(
    client: httpx.AsyncClient, catalog: FakeCatalog, query_string: str, field: str, message: str
) -> None:
    response = await client.get(f"/book/search?{query_string}")

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["errors"] == [{"field": field, "message": message}]
    assert catalog.searches == []


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (
            CatalogUnavailableError("Gutendex GET /books/ returned status 503"),
            502,
            "the book catalog is not available, try again later",
        ),
        (
            CatalogTimeoutError("Gutendex GET /books/ timed out"),
            504,
            "the book catalog did not answer in time, try again later",
        ),
    ],
)
async def test_catalog_failures(
    client: httpx.AsyncClient,
    catalog: FakeCatalog,
    json_logs: LogRecords,
    error: Exception,
    status: int,
    detail: str,
) -> None:
    catalog.error = error

    response = await client.get("/book/search", params={"q": "dickens"})

    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == detail
    assert "Gutendex" not in response.text
    [record] = [r for r in json_logs() if r["msg"] == "book catalog request failed"]
    assert record["level"] == "ERROR"
    assert record["error"] == str(error)
    assert record["request_id"] == response.headers["x-request-id"]


async def test_query_is_documented(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")

    operation = response.json()["paths"]["/book/search"]["get"]
    assert {p["name"] for p in operation["parameters"]} == {"q", "page"}
    assert {"200", "404", "422", "502", "504"} <= operation["responses"].keys()


def test_get_catalog_reads_the_application_state() -> None:
    app = FastAPI()
    catalog = FakeCatalog()
    app.state.catalog = catalog

    assert get_catalog(Request({"type": "http", "app": app})) is catalog
