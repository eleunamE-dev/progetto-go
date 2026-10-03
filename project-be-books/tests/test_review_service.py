from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from bookreviews.catalog import Book, BookNotFoundError, CatalogUnavailableError
from bookreviews.review_service import Review, ReviewNotFoundError, ReviewService, ReviewStatus
from tests.fakes import FakeCatalog, FakeReviewRepository

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def repository() -> FakeReviewRepository:
    return FakeReviewRepository()


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog(books={1342: Book(id=1342, title="Pride and Prejudice")})


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(repository: FakeReviewRepository, catalog: FakeCatalog, clock: Clock) -> ReviewService:
    return ReviewService(repository, catalog, clock)


async def test_submit_stores_a_pending_review(
    service: ReviewService, repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    review = await service.submit(1342, "A classic.", 9)

    assert review == Review(
        id=review.id,
        book_id=1342,
        content="A classic.",
        score=9,
        status=ReviewStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )
    assert review.id.version == 7
    assert repository.reviews == {review.id: review}
    assert catalog.book_requests == [1342]


async def test_review_ids_follow_creation_order(service: ReviewService) -> None:
    first = await service.submit(1342, "First.", 5)
    second = await service.submit(1342, "Second.", 5)

    assert first.id < second.id


async def test_submit_rejects_unknown_books(
    service: ReviewService, repository: FakeReviewRepository
) -> None:
    with pytest.raises(BookNotFoundError):
        await service.submit(999, "A classic.", 9)

    assert repository.reviews == {}


async def test_submit_stores_nothing_when_the_catalog_is_unavailable(
    service: ReviewService, repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    catalog.error = CatalogUnavailableError("Gutendex GET /books/1342/ returned status 503")

    with pytest.raises(CatalogUnavailableError):
        await service.submit(1342, "A classic.", 9)

    assert repository.reviews == {}


async def test_get(service: ReviewService) -> None:
    review = await service.submit(1342, "A classic.", 9)

    assert await service.get(review.id) == review


async def test_update_changes_text_score_and_update_time(
    service: ReviewService, clock: Clock
) -> None:
    review = await service.submit(1342, "A classic.", 9)
    clock.now = NOW + timedelta(minutes=5)

    updated = await service.update(review.id, "Better on a second read.", 10)

    assert updated == replace(
        review, content="Better on a second read.", score=10, updated_at=clock.now
    )
    assert await service.get(review.id) == updated


async def test_delete(service: ReviewService) -> None:
    review = await service.submit(1342, "A classic.", 9)

    await service.delete(review.id)

    with pytest.raises(ReviewNotFoundError):
        await service.get(review.id)


async def test_unknown_reviews(service: ReviewService) -> None:
    review = await service.submit(1342, "A classic.", 9)
    await service.delete(review.id)

    with pytest.raises(ReviewNotFoundError):
        await service.get(review.id)
    with pytest.raises(ReviewNotFoundError):
        await service.update(review.id, "Changed.", 1)
    with pytest.raises(ReviewNotFoundError) as excinfo:
        await service.delete(review.id)
    assert excinfo.value.review_id == review.id
