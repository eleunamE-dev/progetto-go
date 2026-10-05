import uuid
from datetime import UTC, datetime
from typing import Any, Self, override

from sqlalchemy import (
    JSON,
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
    text,
)
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bookreviews.core.catalog import Book, Person
from bookreviews.core.reviews import Review, ReviewStatus

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
