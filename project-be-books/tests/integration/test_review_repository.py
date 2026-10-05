import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bookreviews.adapters.database.repository import SqlReviewRepository
from bookreviews.core.catalog import Book, Person
from bookreviews.core.reviews import (
    IdempotencyKey,
    IdempotencyKeyTakenError,
    Review,
    ReviewStatus,
)

pytestmark = pytest.mark.integration

CREATED_AT = datetime(2026, 10, 3, 12, 30, 15, 123456, tzinfo=UTC)


def new_review(**changes: Any) -> Review:
    review = Review(
        id=uuid.uuid7(),
        book_id=1342,
        content="A classic.",
        score=9,
        status=ReviewStatus.PENDING,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        owner="tests",
    )
    return replace(review, **changes)


async def test_add_and_get(repository: SqlReviewRepository) -> None:
    review = new_review(content="Una lettura splendida 📚, consigliata!\nDa rileggere.")

    await repository.add(review)

    assert await repository.get(review.id) == review


async def test_get_unknown(repository: SqlReviewRepository) -> None:
    assert await repository.get(uuid.uuid7()) is None


async def test_timestamps_are_stored_in_utc(
    repository: SqlReviewRepository, engine: AsyncEngine
) -> None:
    rome = timezone(timedelta(hours=2))
    review = new_review(created_at=datetime(2026, 10, 3, 14, 30, tzinfo=rome))

    await repository.add(review)

    async with engine.connect() as connection:
        stored = await connection.scalar(
            text("SELECT created_at FROM reviews WHERE id = :id"), {"id": review.id}
        )
    assert stored.tzinfo is None
    assert stored.replace(tzinfo=UTC) == datetime(2026, 10, 3, 12, 30, tzinfo=UTC)
    fetched = await repository.get(review.id)
    assert fetched is not None
    assert fetched.created_at == review.created_at
    assert fetched.created_at.tzinfo == UTC


async def test_update(repository: SqlReviewRepository) -> None:
    review = new_review()
    await repository.add(review)
    later = CREATED_AT + timedelta(minutes=5)

    updated = await repository.update(
        review.id, content="Better on a second read.", score=10, updated_at=later
    )

    assert updated == replace(
        review, content="Better on a second read.", score=10, updated_at=later, version=2
    )
    assert await repository.get(review.id) == updated


async def test_update_unknown(repository: SqlReviewRepository) -> None:
    assert (
        await repository.update(uuid.uuid7(), content="x", score=1, updated_at=CREATED_AT) is None
    )


async def test_delete(repository: SqlReviewRepository) -> None:
    review = new_review()
    await repository.add(review)

    assert await repository.delete(review.id)
    assert await repository.get(review.id) is None
    assert not await repository.delete(review.id)


PRIDE_AND_PREJUDICE = Book(
    id=1342,
    title="Pride and Prejudice",
    authors=(Person("Austen, Jane", 1775, 1817), Person("Anonymous")),
    subjects=("Courtship -- Fiction",),
    bookshelves=("Harvard Classics",),
    languages=("en",),
    summaries=("A novel published in 1813.",),
    cover_url="https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
    download_count=190246,
)


async def test_complete_stores_the_book_data(repository: SqlReviewRepository) -> None:
    review = new_review()
    await repository.add(review)

    assert await repository.complete(review.id, PRIDE_AND_PREJUDICE, CREATED_AT)

    assert await repository.get(review.id) == replace(
        review, status=ReviewStatus.COMPLETED, book=PRIDE_AND_PREJUDICE, version=2
    )


async def test_complete_only_changes_pending_reviews(repository: SqlReviewRepository) -> None:
    review = new_review()
    await repository.add(review)
    await repository.complete(review.id, PRIDE_AND_PREJUDICE, CREATED_AT)

    assert not await repository.complete(review.id, PRIDE_AND_PREJUDICE, CREATED_AT)
    assert not await repository.fail(review.id, CREATED_AT)
    assert await repository.delete(review.id)
    assert not await repository.complete(review.id, PRIDE_AND_PREJUDICE, CREATED_AT)


async def test_complete_keeps_the_text_and_score(repository: SqlReviewRepository) -> None:
    review = new_review()
    await repository.add(review)
    later = CREATED_AT + timedelta(minutes=1)
    await repository.update(review.id, content="Edited meanwhile.", score=7, updated_at=later)

    await repository.complete(review.id, PRIDE_AND_PREJUDICE, later)

    completed = await repository.get(review.id)
    assert completed is not None
    assert (completed.content, completed.score, completed.updated_at) == (
        "Edited meanwhile.",
        7,
        later,
    )


async def test_reviews_of_the_same_book_share_its_latest_data(
    repository: SqlReviewRepository,
) -> None:
    first, second = new_review(), new_review()
    await repository.add(first)
    await repository.add(second)
    refreshed = replace(PRIDE_AND_PREJUDICE, download_count=190300, cover_url=None)

    await repository.complete(first.id, PRIDE_AND_PREJUDICE, CREATED_AT)
    await repository.complete(second.id, refreshed, CREATED_AT + timedelta(hours=1))

    for review_id in (first.id, second.id):
        fetched = await repository.get(review_id)
        assert fetched is not None
        assert fetched.book == refreshed


async def test_complete_can_retry_a_failed_review(repository: SqlReviewRepository) -> None:
    review = new_review(status=ReviewStatus.FAILED)
    await repository.add(review)

    assert not await repository.complete(review.id, PRIDE_AND_PREJUDICE, CREATED_AT)
    assert await repository.complete(
        review.id, PRIDE_AND_PREJUDICE, CREATED_AT, expected_status=ReviewStatus.FAILED
    )

    assert await repository.get(review.id) == replace(
        review, status=ReviewStatus.COMPLETED, book=PRIDE_AND_PREJUDICE, version=2
    )
    assert not await repository.complete(
        review.id, PRIDE_AND_PREJUDICE, CREATED_AT, expected_status=ReviewStatus.FAILED
    )


async def test_fail(repository: SqlReviewRepository) -> None:
    review = new_review()
    await repository.add(review)

    assert await repository.fail(review.id, CREATED_AT)

    assert await repository.get(review.id) == replace(review, status=ReviewStatus.FAILED, version=2)


async def test_stale_pending_reviews(repository: SqlReviewRepository) -> None:
    oldest = new_review(created_at=CREATED_AT - timedelta(hours=2))
    older = new_review(created_at=CREATED_AT - timedelta(hours=1))
    recent = new_review(created_at=CREATED_AT)
    completed = new_review(created_at=CREATED_AT - timedelta(hours=3))
    for review in (recent, older, completed, oldest):
        await repository.add(review)
    await repository.complete(completed.id, PRIDE_AND_PREJUDICE, CREATED_AT)
    cutoff = CREATED_AT - timedelta(minutes=10)

    assert await repository.stale_pending(queued_before=cutoff, limit=10) == [oldest.id, older.id]
    assert await repository.stale_pending(queued_before=cutoff, limit=1) == [oldest.id]

    await repository.mark_queued([oldest.id], CREATED_AT)

    assert await repository.stale_pending(queued_before=cutoff, limit=10) == [older.id]
    await repository.mark_queued([], CREATED_AT)


async def test_expire_pending_reviews(repository: SqlReviewRepository) -> None:
    abandoned = new_review(created_at=CREATED_AT - timedelta(days=2))
    recent = new_review(created_at=CREATED_AT)
    completed = new_review(created_at=CREATED_AT - timedelta(days=3))
    for review in (abandoned, recent, completed):
        await repository.add(review)
    await repository.complete(completed.id, PRIDE_AND_PREJUDICE, CREATED_AT)
    cutoff = CREATED_AT - timedelta(days=1)

    assert await repository.expire_pending(created_before=cutoff, at=CREATED_AT) == 1

    assert await repository.get(abandoned.id) == replace(
        abandoned, status=ReviewStatus.FAILED, version=2
    )
    assert await repository.get(recent.id) == recent
    assert await repository.get(completed.id) == replace(
        completed, status=ReviewStatus.COMPLETED, book=PRIDE_AND_PREJUDICE, version=2
    )
    assert await repository.expire_pending(created_before=cutoff, at=CREATED_AT) == 0


async def test_failed_reviews(repository: SqlReviewRepository) -> None:
    oldest = new_review(created_at=CREATED_AT - timedelta(days=3), status=ReviewStatus.FAILED)
    older = new_review(created_at=CREATED_AT - timedelta(days=2), status=ReviewStatus.FAILED)
    recent = new_review(created_at=CREATED_AT, status=ReviewStatus.FAILED)
    pending = new_review(created_at=CREATED_AT - timedelta(days=2))
    for review in (recent, pending, older, oldest):
        await repository.add(review)
    since, until = CREATED_AT - timedelta(days=2), CREATED_AT

    assert await repository.failed_reviews(created_after=None, created_before=None, limit=10) == [
        oldest.id,
        older.id,
        recent.id,
    ]
    assert await repository.failed_reviews(created_after=None, created_before=None, limit=2) == [
        oldest.id,
        older.id,
    ]
    assert await repository.failed_reviews(created_after=since, created_before=None, limit=10) == [
        older.id,
        recent.id,
    ]
    assert await repository.failed_reviews(created_after=since, created_before=until, limit=10) == [
        older.id
    ]


async def test_every_change_bumps_the_version(repository: SqlReviewRepository) -> None:
    review = new_review()
    await repository.add(review)

    updated = await repository.update(review.id, content="Changed.", score=1, updated_at=CREATED_AT)
    await repository.complete(review.id, PRIDE_AND_PREJUDICE, CREATED_AT)

    assert review.version == 1
    assert updated is not None
    assert updated.version == 2
    completed = await repository.get(review.id)
    assert completed is not None
    assert completed.version == 3


async def test_conditional_update_and_delete(repository: SqlReviewRepository) -> None:
    review = new_review()
    await repository.add(review)

    stale = await repository.update(
        review.id, content="Changed.", score=1, updated_at=CREATED_AT, expected_version=2
    )
    updated = await repository.update(
        review.id, content="Changed.", score=1, updated_at=CREATED_AT, expected_version=1
    )

    assert stale is None
    assert updated is not None
    assert updated.version == 2
    assert not await repository.delete(review.id, expected_version=1)
    assert await repository.delete(review.id, expected_version=2)
    assert await repository.get(review.id) is None


async def test_idempotency_keys(repository: SqlReviewRepository) -> None:
    review = new_review()
    await repository.add(review, IdempotencyKey("tests", "Key-1", "a" * 64))
    duplicate = new_review()

    with pytest.raises(IdempotencyKeyTakenError):
        await repository.add(duplicate, IdempotencyKey("tests", "Key-1", "b" * 64))

    assert await repository.get(duplicate.id) is None
    assert await repository.find_by_idempotency_key("tests", "Key-1") == (review, "a" * 64)
    assert await repository.find_by_idempotency_key("tests", "key-1") is None
    assert await repository.find_by_idempotency_key("other-app", "Key-1") is None
    await repository.delete(review.id)
    assert await repository.find_by_idempotency_key("tests", "Key-1") is None


async def test_forget_idempotency_keys(repository: SqlReviewRepository) -> None:
    old = new_review(created_at=CREATED_AT - timedelta(days=2))
    recent = new_review()
    await repository.add(old, IdempotencyKey("tests", "old", "a" * 64))
    await repository.add(recent, IdempotencyKey("tests", "recent", "b" * 64))

    forgotten = await repository.forget_idempotency_keys(
        created_before=CREATED_AT - timedelta(days=1)
    )

    assert forgotten == 1
    assert await repository.find_by_idempotency_key("tests", "old") is None
    assert await repository.find_by_idempotency_key("tests", "recent") == (recent, "b" * 64)
    assert await repository.get(old.id) == old
