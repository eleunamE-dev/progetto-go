import argparse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from bookreviews.adapters.database.repository import SqlReviewRepository
from bookreviews.cli import admin
from bookreviews.config import Settings
from bookreviews.core.catalog import Book, BookCatalog
from bookreviews.core.reviews import Review, ReviewStatus
from tests.fakes import FakeCatalog

pytestmark = pytest.mark.integration

BOOK = Book(id=1342, title="Pride and Prejudice")


def failed_review(book_id: int, created_at: datetime) -> Review:
    return Review(
        id=uuid.uuid7(),
        book_id=book_id,
        content="A classic.",
        score=9,
        status=ReviewStatus.FAILED,
        created_at=created_at,
        updated_at=created_at,
        owner="tests",
    )


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(database_url=SecretStr(database_url))


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    catalog = FakeCatalog(books={1342: BOOK})

    @asynccontextmanager
    async def gutendex(*_args: object) -> AsyncIterator[BookCatalog]:
        yield catalog

    monkeypatch.setattr(admin, "GutendexClient", gutendex)
    return catalog


def arguments(**changes: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "since": None,
        "until": None,
        "limit": 1000,
        "concurrency": 4,
        "dry_run": False,
    }
    return argparse.Namespace(**(defaults | changes))


async def test_retry_failed_completes_the_reviews_whose_book_is_back(
    settings: Settings, repository: SqlReviewRepository, catalog: FakeCatalog
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    expired = failed_review(1342, now - timedelta(days=2))
    missing = failed_review(999, now - timedelta(days=2))
    older = failed_review(1342, now - timedelta(days=10))
    for review in (expired, missing, older):
        await repository.add(review)

    summary = await admin.retry_failed(settings, arguments(since=now - timedelta(days=3)))

    assert summary.startswith("2 failed reviews retried: 1 completed, 1 still failed")
    completed = await repository.get(expired.id)
    assert completed is not None
    assert completed.status is ReviewStatus.COMPLETED
    assert completed.book == BOOK
    for untouched in (missing, older):
        assert await repository.get(untouched.id) == untouched


async def test_a_dry_run_only_lists_the_reviews(
    settings: Settings, repository: SqlReviewRepository, catalog: FakeCatalog
) -> None:
    review = failed_review(1342, datetime.now(UTC).replace(microsecond=0))
    await repository.add(review)

    summary = await admin.retry_failed(settings, arguments(dry_run=True))

    assert summary == f"{review.id}\n1 failed reviews would be retried"
    assert catalog.book_requests == []
    assert await repository.get(review.id) == review
