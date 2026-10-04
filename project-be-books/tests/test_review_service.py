import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from bookreviews.catalog import Book, BookNotFoundError, CatalogUnavailableError
from bookreviews.review_service import (
    Review,
    ReviewForbiddenError,
    ReviewNotFoundError,
    ReviewService,
    ReviewStatus,
)
from tests.conftest import LogRecords
from tests.fakes import FakeCatalog, FakeQueue, FakeReviewRepository

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
OWNER = "web-app"


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
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def service(
    repository: FakeReviewRepository, catalog: FakeCatalog, queue: FakeQueue, clock: Clock
) -> ReviewService:
    return ReviewService(repository, catalog, queue, clock)


async def test_submit_stores_a_pending_review(
    service: ReviewService, repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    review = await service.submit(1342, "A classic.", 9, OWNER)

    assert review == Review(
        id=review.id,
        book_id=1342,
        content="A classic.",
        score=9,
        status=ReviewStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        owner=OWNER,
    )
    assert review.id.version == 7
    assert repository.reviews == {review.id: review}
    assert catalog.book_requests == [1342]


async def test_submit_queues_the_review_for_enrichment(
    service: ReviewService, queue: FakeQueue
) -> None:
    review = await service.submit(1342, "A classic.", 9, OWNER)

    assert queue.enqueued == [review.id]


async def test_submit_keeps_the_review_when_the_queue_is_unavailable(
    service: ReviewService,
    repository: FakeReviewRepository,
    queue: FakeQueue,
    json_logs: LogRecords,
) -> None:
    queue.accepted = 0

    review = await service.submit(1342, "A classic.", 9, OWNER)

    assert repository.reviews == {review.id: review}
    [record] = [r for r in json_logs() if r["level"] == "WARNING"]
    assert record["msg"] == "review saved but not queued, the sweeper will queue it"
    assert record["review_id"] == str(review.id)


async def test_review_ids_follow_creation_order(service: ReviewService) -> None:
    first = await service.submit(1342, "First.", 5, OWNER)
    second = await service.submit(1342, "Second.", 5, OWNER)

    assert first.id < second.id


async def test_submit_rejects_unknown_books(
    service: ReviewService, repository: FakeReviewRepository
) -> None:
    with pytest.raises(BookNotFoundError):
        await service.submit(999, "A classic.", 9, OWNER)

    assert repository.reviews == {}


async def test_submit_stores_nothing_when_the_catalog_is_unavailable(
    service: ReviewService, repository: FakeReviewRepository, catalog: FakeCatalog
) -> None:
    catalog.error = CatalogUnavailableError("Gutendex GET /books/1342/ returned status 503")

    with pytest.raises(CatalogUnavailableError):
        await service.submit(1342, "A classic.", 9, OWNER)

    assert repository.reviews == {}


async def test_get(service: ReviewService) -> None:
    review = await service.submit(1342, "A classic.", 9, OWNER)

    assert await service.get(review.id) == review


async def test_update_changes_text_score_and_update_time(
    service: ReviewService, clock: Clock
) -> None:
    review = await service.submit(1342, "A classic.", 9, OWNER)
    clock.now = NOW + timedelta(minutes=5)

    updated = await service.update(review.id, "Better on a second read.", 10, OWNER)

    assert updated == replace(
        review, content="Better on a second read.", score=10, updated_at=clock.now
    )
    assert await service.get(review.id) == updated


async def test_delete(service: ReviewService) -> None:
    review = await service.submit(1342, "A classic.", 9, OWNER)

    await service.delete(review.id, OWNER)

    with pytest.raises(ReviewNotFoundError):
        await service.get(review.id)


async def test_only_the_owner_changes_or_deletes_a_review(service: ReviewService) -> None:
    review = await service.submit(1342, "A classic.", 9, OWNER)

    with pytest.raises(ReviewForbiddenError) as update_error:
        await service.update(review.id, "Changed.", 1, "someone-else")
    with pytest.raises(ReviewForbiddenError) as delete_error:
        await service.delete(review.id, "someone-else")

    assert update_error.value.review_id == delete_error.value.review_id == review.id
    assert await service.get(review.id) == review


async def test_unknown_reviews(service: ReviewService) -> None:
    review = await service.submit(1342, "A classic.", 9, OWNER)
    await service.delete(review.id, OWNER)

    with pytest.raises(ReviewNotFoundError):
        await service.get(review.id)
    with pytest.raises(ReviewNotFoundError):
        await service.update(review.id, "Changed.", 1, OWNER)
    with pytest.raises(ReviewNotFoundError) as excinfo:
        await service.delete(review.id, OWNER)
    assert excinfo.value.review_id == review.id


@dataclass
class VanishingRepository(FakeReviewRepository):
    async def get(self, review_id: uuid.UUID) -> Review | None:
        review = await super().get(review_id)
        self.reviews.pop(review_id, None)
        return review


async def test_a_review_deleted_during_a_change_is_not_found(
    catalog: FakeCatalog, queue: FakeQueue, clock: Clock
) -> None:
    repository = VanishingRepository()
    service = ReviewService(repository, catalog, queue, clock)

    first = await service.submit(1342, "A classic.", 9, OWNER)
    with pytest.raises(ReviewNotFoundError):
        await service.update(first.id, "Changed.", 1, OWNER)
    second = await service.submit(1342, "A classic.", 9, OWNER)
    with pytest.raises(ReviewNotFoundError):
        await service.delete(second.id, OWNER)
