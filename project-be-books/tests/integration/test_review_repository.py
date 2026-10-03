import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bookreviews.database import SqlReviewRepository
from bookreviews.review_service import Review, ReviewStatus

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
        review, content="Better on a second read.", score=10, updated_at=later
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
