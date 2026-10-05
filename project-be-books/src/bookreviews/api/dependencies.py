from typing import Annotated

from fastapi import Depends, Request

from bookreviews.core.catalog import BookCatalog
from bookreviews.core.reviews import ReviewQueue, ReviewRepository, ReviewService


def get_catalog(request: Request) -> BookCatalog:
    catalog: BookCatalog = request.app.state.catalog
    return catalog


def get_review_repository(request: Request) -> ReviewRepository:
    repository: ReviewRepository = request.app.state.reviews
    return repository


def get_review_queue(request: Request) -> ReviewQueue:
    queue: ReviewQueue = request.app.state.queue
    return queue


def get_review_service(
    repository: Annotated[ReviewRepository, Depends(get_review_repository)],
    catalog: Annotated[BookCatalog, Depends(get_catalog)],
    queue: Annotated[ReviewQueue, Depends(get_review_queue)],
) -> ReviewService:
    return ReviewService(repository, catalog, queue)


Service = Annotated[ReviewService, Depends(get_review_service)]
