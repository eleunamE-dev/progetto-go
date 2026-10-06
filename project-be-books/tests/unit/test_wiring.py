import asyncio
from typing import Any

import pytest
from pydantic import SecretStr

from bookreviews import wiring
from bookreviews.config import Settings
from bookreviews.core.catalog import (
    Book,
    CatalogBusyError,
    CatalogCircuitOpenError,
    CatalogTimeoutError,
)
from tests.fakes import FakeCatalog

BOOKS = {1342: Book(id=1342, title="Pride and Prejudice"), 98: Book(id=98, title="Tale")}


async def test_the_catalog_cache_follows_the_settings() -> None:
    source = FakeCatalog(books=BOOKS)
    catalog = wiring.build_catalog(source, Settings(catalog_cache_ttl=60, catalog_cache_size=1))

    for book_id in (1342, 1342, 98, 1342):
        await catalog.get_book(book_id)

    assert source.book_requests == [1342, 98, 1342]


async def test_the_circuit_breaker_follows_the_settings() -> None:
    source = FakeCatalog(error=CatalogTimeoutError("timed out"))
    settings = Settings(gutendex_failure_threshold=2, gutendex_reset_timeout=45)
    catalog = wiring.build_catalog(source, settings)

    for book_id in (1, 2):
        with pytest.raises(CatalogTimeoutError):
            await catalog.get_book(book_id)
    with pytest.raises(CatalogCircuitOpenError) as excinfo:
        await catalog.get_book(3)

    assert excinfo.value.retry_after == 45


async def test_the_concurrency_limit_follows_the_settings() -> None:
    release = asyncio.Event()

    class SlowCatalog(FakeCatalog):
        async def get_book(self, book_id: int) -> Book:
            await release.wait()
            return await super().get_book(book_id)

    settings = Settings(gutendex_max_concurrency=1, gutendex_queue_timeout=0.05)
    catalog = wiring.build_catalog(SlowCatalog(books=BOOKS), settings)
    running = asyncio.create_task(catalog.get_book(1342))
    await asyncio.sleep(0)

    with pytest.raises(CatalogBusyError):
        await asyncio.wait_for(catalog.get_book(98), timeout=1)

    release.set()
    assert await running == BOOKS[1342]


def test_the_engine_follows_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[str, dict[str, Any]]] = []
    engine = object()

    def create_engine(url: str, **options: Any) -> object:
        created.append((url, options))
        return engine

    monkeypatch.setattr(wiring, "create_engine", create_engine)
    url = "mysql+aiomysql://app:secret@db:3306/reviews"
    settings = Settings(
        database_url=SecretStr(url),
        database_pool_size=3,
        database_max_overflow=4,
        database_pool_timeout=7,
    )

    assert wiring.build_engine(settings) is engine
    assert created == [(url, {"pool_size": 3, "max_overflow": 4, "pool_timeout": 7})]
