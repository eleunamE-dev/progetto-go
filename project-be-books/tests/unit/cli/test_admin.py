import argparse
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from bookreviews.cli import admin
from bookreviews.config import Settings
from bookreviews.core.catalog import Book, CatalogUnavailableError
from bookreviews.core.enrichment import ReviewEnricher
from bookreviews.core.reviews import Review, ReviewStatus
from tests.fakes import FakeCatalog, FakeReviewRepository

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
BOOK = Book(id=1342, title="Pride and Prejudice")


def failed_review(book_id: int = 1342) -> Review:
    return Review(
        id=uuid.uuid7(),
        book_id=book_id,
        content="A classic.",
        score=9,
        status=ReviewStatus.FAILED,
        created_at=NOW,
        updated_at=NOW,
        owner="tests",
    )


async def test_retrying_reports_what_happened_to_each_review() -> None:
    repository = FakeReviewRepository()
    found, missing, changed = failed_review(), failed_review(book_id=999), failed_review()
    for review in (found, missing, changed):
        await repository.add(review)
    repository.reviews[changed.id] = replace(changed, status=ReviewStatus.COMPLETED)
    enricher = ReviewEnricher(repository, FakeCatalog(books={1342: BOOK}))

    report = await admin.retry_reviews(enricher, [found.id, missing.id, changed.id], 2)

    assert report == admin.RetryReport(
        completed=[found.id], still_failed=[missing.id], skipped=[changed.id]
    )
    assert report.summary() == (
        "3 failed reviews retried: 1 completed, 1 still failed (book not in the catalog), "
        "0 not retried (catalog unavailable, run again later), 1 skipped (changed meanwhile)"
    )
    assert repository.reviews[found.id].status is ReviewStatus.COMPLETED


async def test_reviews_meeting_an_unavailable_catalog_are_left_for_later() -> None:
    repository = FakeReviewRepository()
    review = failed_review()
    await repository.add(review)
    catalog = FakeCatalog(error=CatalogUnavailableError("Gutendex GET /books/1342/ timed out"))

    report = await admin.retry_reviews(ReviewEnricher(repository, catalog), [review.id], 4)

    assert report == admin.RetryReport(unavailable=[review.id])
    assert repository.reviews[review.id] == review


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-01", datetime(2026, 10, 1, tzinfo=UTC)),
        ("2026-10-01T08:30:00", datetime(2026, 10, 1, 8, 30, tzinfo=UTC)),
        (
            "2026-10-01T10:30:00+02:00",
            datetime(2026, 10, 1, 10, 30, tzinfo=timezone(timedelta(hours=2))),
        ),
    ],
)
def test_times_without_a_zone_are_utc(value: str, expected: datetime) -> None:
    assert admin.moment(value) == expected


def parsed(monkeypatch: pytest.MonkeyPatch, *argv: str) -> argparse.Namespace:
    calls: list[argparse.Namespace] = []

    async def retry_failed(settings: Settings, arguments: argparse.Namespace) -> str:
        calls.append(arguments)
        return ""

    monkeypatch.setattr(admin, "retry_failed", retry_failed)
    monkeypatch.setattr(admin, "configure_logging", lambda *_: None)
    admin.main(["retry-failed", *argv])
    [arguments] = calls
    return arguments


def test_the_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[Settings, argparse.Namespace]] = []
    logging: list[tuple[object, ...]] = []

    async def retry_failed(settings: Settings, arguments: argparse.Namespace) -> str:
        calls.append((settings, arguments))
        return "2 failed reviews would be retried"

    monkeypatch.setattr(admin, "retry_failed", retry_failed)
    monkeypatch.setattr(admin, "configure_logging", lambda *args: logging.append(args))
    monkeypatch.setenv("LOG_LEVEL", "debug")

    since, until = ["--since", "2026-10-01"], ["--until", "2026-10-02"]
    admin.main(["retry-failed", *since, *until, "--limit", "50", "--dry-run"])

    [(settings, arguments)] = calls
    assert settings.log_level == "DEBUG"
    assert logging == [("DEBUG", sys.stderr)]
    assert arguments.since == datetime(2026, 10, 1, tzinfo=UTC)
    assert arguments.until == datetime(2026, 10, 2, tzinfo=UTC)
    assert arguments.limit == 50
    assert arguments.concurrency == admin.RETRY_CONCURRENCY
    assert arguments.dry_run
    assert capsys.readouterr().out == "2 failed reviews would be retried\n"


def test_the_defaults_of_retry_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = parsed(monkeypatch)

    assert (arguments.since, arguments.until) == (None, None)
    assert arguments.limit == 1000
    assert arguments.concurrency == admin.RETRY_CONCURRENCY
    assert not arguments.dry_run


def test_counts_of_one_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = parsed(monkeypatch, "--limit", "1", "--concurrency", "1")

    assert (arguments.limit, arguments.concurrency) == (1, 1)


def test_the_summary_counts_every_review() -> None:
    ids = [uuid.uuid7() for _ in range(5)]
    report = admin.RetryReport(
        completed=ids[:1], still_failed=ids[1:2], unavailable=ids[2:4], skipped=ids[4:]
    )

    assert report.summary().startswith("5 failed reviews retried: 1 completed")


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        admin.main([])

    assert "retry-failed" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--limit", "--concurrency"])
def test_counts_must_be_positive(option: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        admin.main(["retry-failed", option, "0"])

    assert "must be at least 1, got 0" in capsys.readouterr().err
