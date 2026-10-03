import uuid
from datetime import UTC, datetime
from importlib.resources import files
from typing import Self, override

from alembic import command
from alembic.config import Config
from sqlalchemy import Dialect, Enum, Integer, SmallInteger, Text, TypeDecorator, Uuid, text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bookreviews.review_service import Review, ReviewStatus

CONNECT_TIMEOUT = 5
POOL_RECYCLE = 1800


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


class ReviewRow(Base):
    __tablename__ = "reviews"
    __table_args__ = ({"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    book_id: Mapped[int] = mapped_column(Integer, index=True)
    content: Mapped[str] = mapped_column(Text)
    score: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status", values_callable=lambda e: [m.value for m in e])
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)

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
        )

    def to_domain(self) -> Review:
        return Review(
            id=self.id,
            book_id=self.book_id,
            content=self.content,
            score=self.score,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
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
            row = await session.get(ReviewRow, review_id)
            return None if row is None else row.to_domain()

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
            return row.to_domain()

    async def delete(self, review_id: uuid.UUID) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(ReviewRow, review_id, with_for_update=True)
            if row is None:
                return False
            await session.delete(row)
            return True


def migrations_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("bookreviews") / "migrations"))
    config.attributes["database_url"] = database_url
    return config


def upgrade_database(database_url: str) -> None:
    command.upgrade(migrations_config(database_url), "head")
