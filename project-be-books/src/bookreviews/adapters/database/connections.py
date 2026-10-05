from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

CONNECT_TIMEOUT = 5
POOL_RECYCLE = 1800


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
