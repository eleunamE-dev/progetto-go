import time
from collections.abc import Awaitable, Callable

from bookreviews.core.catalog import (
    Book,
    BookCatalog,
    CatalogError,
    CatalogTimeoutError,
    CatalogUnavailableError,
    SearchResult,
)
from bookreviews.observability import metrics


class MeasuredCatalog:
    def __init__(self, inner: BookCatalog) -> None:
        self._inner = inner

    async def search(self, query: str, page: int = 1) -> SearchResult:
        return await self._measure("search", lambda: self._inner.search(query, page))

    async def get_book(self, book_id: int) -> Book:
        return await self._measure("get_book", lambda: self._inner.get_book(book_id))

    async def _measure[T](self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        outcome = "error"
        started = time.perf_counter()
        try:
            result = await call()
            outcome = "ok"
        except CatalogTimeoutError:
            outcome = "timeout"
            raise
        except CatalogUnavailableError:
            outcome = "unavailable"
            raise
        except CatalogError:
            outcome = "not_found"
            raise
        finally:
            metrics.catalog_requests.labels(operation, outcome).inc()
            metrics.catalog_request_duration.labels(operation).observe(
                time.perf_counter() - started
            )
        return result
