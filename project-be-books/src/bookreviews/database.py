import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, Self, override

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    Dialect,
    Enum,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    Uuid,
    select,
    text,
    update,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bookreviews.catalog import Book, Person
from bookreviews.review_service import Review, ReviewStatus

CONNECT_TIMEOUT = 5
POOL_RECYCLE = 1800
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
            book=book.to_domain()
            if book is not None and self.status is ReviewStatus.COMPLETED
            else None,
        )


def create_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE,
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

    async def add(self, review: Review) -> None:
        async with self._sessions.begin() as session:
            session.add(ReviewRow.from_domain(review))

    async def get(self, review_id: uuid.UUID) -> Review | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(ReviewRow, BookRow)
                .outerjoin(BookRow, BookRow.id == ReviewRow.book_id)
                .where(ReviewRow.id == review_id)
            )
            found = result.first()
            return None if found is None else found[0].to_domain(found[1])

    async def update(
        self, review_id: uuid.UUID, *, content: str, score: int, updated_at: datetime
    ) -> Review | None:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None:
                return None
            row.content = content
            row.score = score
            row.updated_at = updated_at
            return row.to_domain(await session.get(BookRow, row.book_id))

    async def delete(self, review_id: uuid.UUID) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None:
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
            return True

    async def fail(self, review_id: uuid.UUID, at: datetime) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None or row.status is not ReviewStatus.PENDING:
                return False
            row.status = ReviewStatus.FAILED
            row.processed_at = at
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


def upgrade_database(database_url: str) -> None:
    command.upgrade(migrations_config(database_url), "head")
