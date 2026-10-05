import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    CursorResult,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bookreviews.adapters.database.models import BookRow, IdempotencyKeyRow, ReviewRow
from bookreviews.core.catalog import Book
from bookreviews.core.reviews import (
    Condition,
    IdempotencyKey,
    IdempotencyKeyTakenError,
    Review,
    ReviewStatus,
)


class SqlReviewRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(self, review: Review, idempotency_key: IdempotencyKey | None = None) -> None:
        async with self._sessions.begin() as session:
            session.add(ReviewRow.from_domain(review))
            if idempotency_key is None:
                return
            await session.flush()
            session.add(
                IdempotencyKeyRow(
                    owner=idempotency_key.owner,
                    key=idempotency_key.value,
                    fingerprint=idempotency_key.fingerprint,
                    review_id=review.id,
                    created_at=review.created_at,
                )
            )
            try:
                await session.flush()
            except IntegrityError as exc:
                raise IdempotencyKeyTakenError(idempotency_key.value) from exc

    async def get(self, review_id: uuid.UUID) -> Review | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(ReviewRow, BookRow)
                .outerjoin(BookRow, BookRow.id == ReviewRow.book_id)
                .where(ReviewRow.id == review_id)
            )
            found = result.first()
            return None if found is None else found[0].to_domain(found[1])

    async def find_by_idempotency_key(self, owner: str, key: str) -> tuple[Review, str] | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(ReviewRow, BookRow, IdempotencyKeyRow.fingerprint)
                .join(IdempotencyKeyRow, IdempotencyKeyRow.review_id == ReviewRow.id)
                .outerjoin(BookRow, BookRow.id == ReviewRow.book_id)
                .where(IdempotencyKeyRow.owner == owner, IdempotencyKeyRow.key == key)
            )
            found = result.first()
            return None if found is None else (found[0].to_domain(found[1]), found[2])

    async def update(  # noqa: PLR0913 - write fields and atomic preconditions
        self,
        review_id: uuid.UUID,
        *,
        content: str,
        score: int,
        updated_at: datetime,
        expected_version: int | None = None,
        condition: Condition | None = None,
    ) -> Review | None:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None or (expected_version is not None and row.version != expected_version):
                return None
            book = await session.get(BookRow, row.book_id, with_for_update=True)
            if condition is not None and not condition(row.to_domain(book)):
                return None
            row.content = content
            row.score = score
            row.updated_at = updated_at
            row.version += 1
            return row.to_domain(book)

    async def delete(
        self,
        review_id: uuid.UUID,
        expected_version: int | None = None,
        *,
        condition: Condition | None = None,
    ) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None or (expected_version is not None and row.version != expected_version):
                return False
            if condition is not None:
                book = await session.get(BookRow, row.book_id, with_for_update=True)
                if not condition(row.to_domain(book)):
                    return False
            await session.delete(row)
            return True

    async def complete(
        self,
        review_id: uuid.UUID,
        book: Book,
        at: datetime,
        expected_status: ReviewStatus = ReviewStatus.PENDING,
    ) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None or row.status is not expected_status:
                return False
            values = BookRow.values(book, at)
            upsert = mysql_insert(BookRow).values(values)
            await session.execute(
                upsert.on_duplicate_key_update(
                    {name: upsert.inserted[name] for name in values if name != "id"}
                )
            )
            row.status = ReviewStatus.COMPLETED
            row.processed_at = at
            row.version += 1
            return True

    async def fail(self, review_id: uuid.UUID, at: datetime) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None or row.status is not ReviewStatus.PENDING:
                return False
            row.status = ReviewStatus.FAILED
            row.processed_at = at
            row.version += 1
            return True

    async def stale_pending(self, *, queued_before: datetime, limit: int) -> list[uuid.UUID]:
        async with self._sessions() as session:
            ids = await session.scalars(
                select(ReviewRow.id)
                .where(
                    ReviewRow.status == ReviewStatus.PENDING, ReviewRow.queued_at < queued_before
                )
                .order_by(ReviewRow.queued_at)
                .limit(limit)
            )
            return list(ids)

    async def failed_reviews(
        self, *, created_after: datetime | None, created_before: datetime | None, limit: int
    ) -> list[uuid.UUID]:
        query = select(ReviewRow.id).where(ReviewRow.status == ReviewStatus.FAILED)
        if created_after is not None:
            query = query.where(ReviewRow.created_at >= created_after)
        if created_before is not None:
            query = query.where(ReviewRow.created_at < created_before)
        async with self._sessions() as session:
            ids = await session.scalars(query.order_by(ReviewRow.created_at).limit(limit))
            return list(ids)

    async def expire_pending(self, *, created_before: datetime, at: datetime) -> int:
        async with self._sessions.begin() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ReviewRow)
                    .where(
                        ReviewRow.status == ReviewStatus.PENDING,
                        ReviewRow.created_at < created_before,
                    )
                    .values(
                        status=ReviewStatus.FAILED,
                        processed_at=at,
                        version=ReviewRow.version + 1,
                    )
                ),
            )
            return result.rowcount

    async def forget_idempotency_keys(self, *, created_before: datetime) -> int:
        async with self._sessions.begin() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(IdempotencyKeyRow).where(IdempotencyKeyRow.created_at < created_before)
                ),
            )
            return result.rowcount

    async def pending_summary(self) -> tuple[int, datetime | None]:
        async with self._sessions() as session:
            result = await session.execute(
                select(func.count(), func.min(ReviewRow.created_at)).where(
                    ReviewRow.status == ReviewStatus.PENDING
                )
            )
            count, oldest = result.one()
            return count, oldest

    async def mark_queued(self, review_ids: Sequence[uuid.UUID], at: datetime) -> None:
        if not review_ids:
            return
        async with self._sessions.begin() as session:
            await session.execute(
                update(ReviewRow).where(ReviewRow.id.in_(review_ids)).values(queued_at=at)
            )
