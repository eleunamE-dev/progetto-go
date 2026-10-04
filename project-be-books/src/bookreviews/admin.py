import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bookreviews.catalog import CatalogUnavailableError
from bookreviews.config import Settings
from bookreviews.database import SqlReviewRepository, create_sessions
from bookreviews.enrichment import Outcome, ReviewEnricher
from bookreviews.gutendex import GutendexClient
from bookreviews.logs import configure_logging
from bookreviews.wiring import build_catalog, build_engine

RETRY_CONCURRENCY = 4


@dataclass(slots=True)
class RetryReport:
    completed: list[uuid.UUID] = field(default_factory=list)
    still_failed: list[uuid.UUID] = field(default_factory=list)
    unavailable: list[uuid.UUID] = field(default_factory=list)
    skipped: list[uuid.UUID] = field(default_factory=list)

    def summary(self) -> str:
        total = (
            len(self.completed) + len(self.still_failed) + len(self.unavailable) + len(self.skipped)
        )
        return (
            f"{total} failed reviews retried: {len(self.completed)} completed, "
            f"{len(self.still_failed)} still failed (book not in the catalog), "
            f"{len(self.unavailable)} not retried (catalog unavailable, run again later), "
            f"{len(self.skipped)} skipped (changed meanwhile)"
        )


async def retry_reviews(
    enricher: ReviewEnricher, review_ids: Sequence[uuid.UUID], concurrency: int
) -> RetryReport:
    report = RetryReport()
    slots = asyncio.Semaphore(concurrency)

    async def retry(review_id: uuid.UUID) -> None:
        async with slots:
            try:
                outcome = await enricher.retry(review_id)
            except CatalogUnavailableError:
                report.unavailable.append(review_id)
                return
        match outcome:
            case Outcome.COMPLETED:
                report.completed.append(review_id)
            case Outcome.FAILED:
                report.still_failed.append(review_id)
            case Outcome.SKIPPED:
                report.skipped.append(review_id)

    async with asyncio.TaskGroup() as tasks:
        for review_id in review_ids:
            tasks.create_task(retry(review_id))
    return report


def moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


async def retry_failed(settings: Settings, arguments: argparse.Namespace) -> str:
    engine = build_engine(settings)
    try:
        repository = SqlReviewRepository(create_sessions(engine))
        review_ids = await repository.failed_reviews(
            created_after=arguments.since, created_before=arguments.until, limit=arguments.limit
        )
        if arguments.dry_run:
            listed = "".join(f"{review_id}\n" for review_id in review_ids)
            return f"{listed}{len(review_ids)} failed reviews would be retried"
        async with GutendexClient(
            str(settings.gutendex_base_url), settings.gutendex_timeout
        ) as gutendex:
            enricher = ReviewEnricher(repository, build_catalog(gutendex, settings))
            report = await retry_reviews(enricher, review_ids, arguments.concurrency)
        return report.summary()
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bookreviews-admin", description="Maintenance tasks for the book review service."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    retry = commands.add_parser(
        "retry-failed",
        help="enrich failed reviews again, for instance after a long Gutendex outage",
        description="Enrich failed reviews again. A review whose book is still missing from the "
        "catalog stays failed; one that meets an unavailable catalog is left for a later run.",
    )
    retry.add_argument(
        "--since", type=moment, help="only reviews created at or after this ISO 8601 time (UTC)"
    )
    retry.add_argument("--until", type=moment, help="only reviews created before this time")
    retry.add_argument("--limit", type=positive, default=1000, help="at most this many reviews")
    retry.add_argument(
        "--concurrency",
        type=positive,
        default=RETRY_CONCURRENCY,
        help="reviews retried in parallel",
    )
    retry.add_argument(
        "--dry-run", action="store_true", help="list the reviews without changing them"
    )
    arguments = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level, sys.stderr)
    sys.stdout.write(asyncio.run(retry_failed(settings, arguments)) + "\n")
