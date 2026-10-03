from sqlalchemy.ext.asyncio import AsyncEngine

from bookreviews.catalog import BookCatalog, CachedCatalog, ResilienceOptions, ResilientCatalog
from bookreviews.config import Settings
from bookreviews.database import create_engine


def build_engine(settings: Settings) -> AsyncEngine:
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
    )


def build_catalog(source: BookCatalog, settings: Settings) -> CachedCatalog:
    resilient = ResilientCatalog(
        source,
        ResilienceOptions(
            max_concurrency=settings.gutendex_max_concurrency,
            queue_timeout=settings.gutendex_queue_timeout,
            failure_threshold=settings.gutendex_failure_threshold,
            reset_timeout=settings.gutendex_reset_timeout,
        ),
    )
    return CachedCatalog(
        resilient, ttl=settings.catalog_cache_ttl, max_books=settings.catalog_cache_size
    )
