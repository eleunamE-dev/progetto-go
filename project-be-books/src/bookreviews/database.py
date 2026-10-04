import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, Self, cast, override

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    CursorResult,
    Dialect,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    Uuid,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bookreviews.catalog import Book, Person
from bookreviews.review_service import (
    IdempotencyKey,
    IdempotencyKeyTakenError,
    Review,
    ReviewStatus,
)

CONNECT_TIMEOUT = 5
POOL_RECYCLE = 1800
MIGRATION_LOCK_TIMEOUT = 600
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


class UTCDateTime(TypeDecorator[datetime]):
    impl = DATETIME(fsp=6)
    cache_ok = True

    @override
    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.astimezone(UTC).replace(tzinfo=None)

    @override
    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class BookRow(Base):
    __tablename__ = "books"
    __table_args__ = (TABLE_OPTIONS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    subjects: Mapped[list[str]] = mapped_column(JSON)
    bookshelves: Mapped[list[str]] = mapped_column(JSON)
    languages: Mapped[list[str]] = mapped_column(JSON)
    summaries: Mapped[list[str]] = mapped_column(JSON)
    cover_url: Mapped[str | None] = mapped_column(String(2048))
    download_count: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)

    @staticmethod
    def values(book: Book, fetched_at: datetime) -> dict[str, Any]:
        return {
            "id": book.id,
            "title": book.title,
            "authors": [
                {"name": a.name, "birth_year": a.birth_year, "death_year": a.death_year}
                for a in book.authors
            ],
            "subjects": list(book.subjects),
            "bookshelves": list(book.bookshelves),
            "languages": list(book.languages),
            "summaries": list(book.summaries),
            "cover_url": book.cover_url,
            "download_count": book.download_count,
            "fetched_at": fetched_at,
        }

    def to_domain(self) -> Book:
        return Book(
            id=self.id,
            title=self.title,
            authors=tuple(
                Person(a["name"], a.get("birth_year"), a.get("death_year")) for a in self.authors
            ),
            subjects=tuple(self.subjects),
            bookshelves=tuple(self.bookshelves),
            languages=tuple(self.languages),
            summaries=tuple(self.summaries),
            cover_url=self.cover_url,
            download_count=self.download_count,
        )


class ReviewRow(Base):
    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_status_queued_at", "status", "queued_at"), TABLE_OPTIONS)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    book_id: Mapped[int] = mapped_column(Integer, index=True)
    content: Mapped[str] = mapped_column(Text)
    score: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status", values_callable=lambda e: [m.value for m in e])
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    owner: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @classmethod
    def from_domain(cls, review: Review) -> Self:
        return cls(
            id=review.id,
            book_id=review.book_id,
            content=review.content,
            score=review.score,
            status=review.status,
            created_at=review.created_at,
            updated_at=review.updated_at,
            owner=review.owner,
            version=review.version,
            queued_at=review.created_at,
        )

    def to_domain(self, book: BookRow | None) -> Review:
        return Review(
            id=self.id,
            book_id=self.book_id,
            content=self.content,
            score=self.score,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            owner=self.owner,
            version=self.version,
            book=book.to_domain()
            if book is not None and self.status is ReviewStatus.COMPLETED
            else None,
        )


class IdempotencyKeyRow(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_keys_created_at", "created_at"), TABLE_OPTIONS)

    owner: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(
        "idempotency_key", VARCHAR(255, charset="ascii", collation="ascii_bin"), primary_key=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    review_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("reviews.id", ondelete="CASCADE", name="fk_idempotency_keys_review_id")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


def create_engine(
    url: str, *, pool_size: int = 5, max_overflow: int = 10, pool_timeout: float = 10
) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        connect_args={"connect_timeout": CONNECT_TIMEOUT},
    )


def create_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


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

    async def update(
        self,
        review_id: uuid.UUID,
        *,
        content: str,
        score: int,
        updated_at: datetime,
        expected_version: int | None = None,
    ) -> Review | None:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None or (expected_version is not None and row.version != expected_version):
                return None
            row.content = content
            row.score = score
            row.updated_at = updated_at
            row.version += 1
            return row.to_domain(await session.get(BookRow, row.book_id))

    async def delete(self, review_id: uuid.UUID, expected_version: int | None = None) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None or (expected_version is not None and row.version != expected_version):
                return False
            await session.delete(row)
            return True

    async def complete(self, review_id: uuid.UUID, book: Book, at: datetime) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None or row.status is not ReviewStatus.PENDING:
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


def migrations_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("bookreviews") / "migrations"))
    config.attributes["database_url"] = database_url
    return config


def upgrade_database(database_url: str, lock_timeout: int = MIGRATION_LOCK_TIMEOUT) -> None:
    config = migrations_config(database_url)
    config.attributes["lock_timeout"] = lock_timeout
    command.upgrade(config, "head")
