import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from bookreviews.core.catalog import Book, BookNotFoundError, CatalogUnavailableError
from bookreviews.core.reviews import (
    Condition,
    IdempotencyKey,
    IdempotencyKeyReusedError,
    IdempotencyKeyTakenError,
    PreconditionFailedError,
    Review,
    ReviewForbiddenError,
    ReviewNotFoundError,
    ReviewService,
    ReviewStatus,
    request_fingerprint,
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
        review, content="Better on a second read.", score=10, updated_at=clock.now, version=2
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

    for condition in (None, lambda _review: True):
        first = await service.submit(1342, "A classic.", 9, OWNER)
        with pytest.raises(ReviewNotFoundError) as update_error:
            await service.update(first.id, "Changed.", 1, OWNER, condition=condition)
        second = await service.submit(1342, "A classic.", 9, OWNER)
        with pytest.raises(ReviewNotFoundError) as delete_error:
            await service.delete(second.id, OWNER, condition=condition)
        assert (update_error.value.review_id, delete_error.value.review_id) == (first.id, second.id)


async def test_a_repeated_submission_returns_the_first_review(
    service: ReviewService,
    repository: FakeReviewRepository,
    queue: FakeQueue,
    catalog: FakeCatalog,
) -> None:
    first = await service.submit(1342, "A classic.", 9, OWNER, idempotency_key="key-1")
    again = await service.submit(1342, "A classic.", 9, OWNER, idempotency_key="key-1")

    assert again == first
    assert list(repository.reviews) == [first.id]
    assert queue.enqueued == [first.id]
    assert catalog.book_requests == [1342]


async def test_a_replay_returns_the_review_as_it_is_now(service: ReviewService) -> None:
    first = await service.submit(1342, "A classic.", 9, OWNER, idempotency_key="key-1")
    updated = await service.update(first.id, "Changed.", 1, OWNER)

    again = await service.submit(1342, "A classic.", 9, OWNER, idempotency_key="key-1")

    assert again == updated


async def test_an_idempotency_key_cannot_be_reused_for_another_request(
    service: ReviewService, repository: FakeReviewRepository
) -> None:
    first = await service.submit(1342, "A classic.", 9, OWNER, idempotency_key="key-1")

    with pytest.raises(IdempotencyKeyReusedError) as excinfo:
        await service.submit(1342, "Another text.", 9, OWNER, idempotency_key="key-1")

    assert excinfo.value.key == "key-1"
    assert list(repository.reviews) == [first.id]


async def test_idempotency_keys_belong_to_their_client(service: ReviewService) -> None:
    mine = await service.submit(1342, "A classic.", 9, OWNER, idempotency_key="key-1")
    theirs = await service.submit(1342, "A classic.", 9, "someone-else", idempotency_key="key-1")

    assert mine.id != theirs.id


@dataclass
class RacingRepository(FakeReviewRepository):
    winner: Review | None = None
    winner_fingerprint: str = ""

    async def add(self, review: Review, idempotency_key: IdempotencyKey | None = None) -> None:
        if idempotency_key is not None and self.winner is not None:
            winner, self.winner = self.winner, None
            await super().add(
                winner,
                IdempotencyKey(
                    idempotency_key.owner, idempotency_key.value, self.winner_fingerprint
                ),
            )
        await super().add(review, idempotency_key)


def concurrent_winner() -> Review:
    return Review(
        id=uuid.uuid7(),
        book_id=1342,
        content="A classic.",
        score=9,
        status=ReviewStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        owner=OWNER,
    )


async def test_a_concurrent_twin_request_gets_the_review_of_the_first(
    catalog: FakeCatalog, queue: FakeQueue, clock: Clock
) -> None:
    winner = concurrent_winner()
    repository = RacingRepository(
        winner=winner, winner_fingerprint=request_fingerprint(1342, "A classic.", 9)
    )
    service = ReviewService(repository, catalog, queue, clock)

    review = await service.submit(1342, "A classic.", 9, OWNER, idempotency_key="key-1")

    assert review == winner
    assert list(repository.reviews) == [winner.id]
    assert queue.enqueued == []


async def test_a_concurrent_request_with_the_same_key_and_another_body_is_refused(
    catalog: FakeCatalog, queue: FakeQueue, clock: Clock
) -> None:
    repository = RacingRepository(
        winner=concurrent_winner(), winner_fingerprint=request_fingerprint(1342, "Other.", 1)
    )
    service = ReviewService(repository, catalog, queue, clock)

    with pytest.raises(IdempotencyKeyReusedError):
        await service.submit(1342, "A classic.", 9, OWNER, idempotency_key="key-1")


async def test_a_taken_key_whose_review_is_gone_is_reported(
    catalog: FakeCatalog, queue: FakeQueue, clock: Clock
) -> None:
    class GoneRepository(FakeReviewRepository):
        async def add(self, review: Review, idempotency_key: IdempotencyKey | None = None) -> None:
            raise IdempotencyKeyTakenError("key-1")

    service = ReviewService(GoneRepository(), catalog, queue, clock)

    with pytest.raises(IdempotencyKeyTakenError):
        await service.submit(1342, "A classic.", 9, OWNER, idempotency_key="key-1")


async def test_changes_happen_only_when_their_condition_holds(service: ReviewService) -> None:
    review = await service.submit(1342, "A classic.", 9, OWNER)

    with pytest.raises(PreconditionFailedError) as update_error:
        await service.update(review.id, "Changed.", 1, OWNER, condition=lambda _review: False)
    with pytest.raises(PreconditionFailedError):
        await service.delete(review.id, OWNER, condition=lambda _review: False)

    assert update_error.value.review_id == review.id
    assert await service.get(review.id) == review
    updated = await service.update(
        review.id, "Changed.", 1, OWNER, condition=lambda current: current.version == 1
    )
    assert updated.version == 2
    await service.delete(review.id, OWNER, condition=lambda current: current.version == 2)
    with pytest.raises(ReviewNotFoundError):
        await service.get(review.id)


async def test_a_change_made_after_the_condition_was_checked_is_kept(
    service: ReviewService, repository: FakeReviewRepository
) -> None:
    review = await service.submit(1342, "A classic.", 9, OWNER)

    def changed_meanwhile(current: Review) -> bool:
        repository.reviews[current.id] = replace(
            current, content="Changed by another request.", version=current.version + 1
        )
        return True

    with pytest.raises(PreconditionFailedError):
        await service.update(review.id, "Mine.", 1, OWNER, condition=changed_meanwhile)
    with pytest.raises(PreconditionFailedError):
        await service.delete(review.id, OWNER, condition=changed_meanwhile)

    assert repository.reviews[review.id].content == "Changed by another request."


class BookRefreshedMeanwhile(FakeReviewRepository):
    async def update(
        self,
        review_id: uuid.UUID,
        *,
        content: str,
        score: int,
        updated_at: datetime,
        expected_version: int | None = None,
        condition: Condition | None = None,
    ) -> Review | None:
        self._refresh_book(review_id)
        return await super().update(
            review_id,
            content=content,
            score=score,
            updated_at=updated_at,
            expected_version=expected_version,
            condition=condition,
        )

    async def delete(
        self,
        review_id: uuid.UUID,
        expected_version: int | None = None,
        *,
        condition: Condition | None = None,
    ) -> bool:
        self._refresh_book(review_id)
        return await super().delete(review_id, expected_version, condition=condition)

    def _refresh_book(self, review_id: uuid.UUID) -> None:
        refreshed = Book(id=1342, title="Pride and Prejudice", download_count=1)
        self.reviews[review_id] = replace(self.reviews[review_id], book=refreshed)


async def test_a_book_refreshed_after_the_check_fails_the_condition(
    catalog: FakeCatalog, queue: FakeQueue, clock: Clock
) -> None:
    repository = BookRefreshedMeanwhile()
    service = ReviewService(repository, catalog, queue, clock)
    edited = await service.submit(1342, "A classic.", 9, OWNER)
    deleted = await service.submit(1342, "A classic.", 9, OWNER)
    seen = {review_id: repository.reviews[review_id] for review_id in (edited.id, deleted.id)}

    def unchanged(current: Review) -> bool:
        return current == seen[current.id]

    with pytest.raises(PreconditionFailedError) as update_error:
        await service.update(edited.id, "Mine.", 1, OWNER, condition=unchanged)
    with pytest.raises(PreconditionFailedError) as delete_error:
        await service.delete(deleted.id, OWNER, condition=unchanged)

    assert (update_error.value.review_id, delete_error.value.review_id) == (edited.id, deleted.id)
    assert repository.reviews[edited.id].content == "A classic."
    assert repository.reviews[deleted.id].version == seen[deleted.id].version
