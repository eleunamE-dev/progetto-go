import hashlib
import re
from typing import Annotated, Any

from fastapi import Header

from bookreviews.api.schemas import ReviewResponse
from bookreviews.core.reviews import Condition, Review

_ENTITY_TAG = re.compile(r'(W/)?("[^"]*")')


def entity_tag(review: Review) -> str:
    representation = ReviewResponse.from_review(review).model_dump_json()
    return f'"{hashlib.sha256(representation.encode()).hexdigest()[:32]}"'


def tag_matches(header: str, tag: str, *, weak: bool) -> bool:
    if header.strip() == "*":
        return True
    return any(
        listed == tag and (weak or not prefix) for prefix, listed in _ENTITY_TAG.findall(header)
    )


def precondition(if_match: str | None) -> Condition | None:
    if if_match is None:
        return None
    return lambda review: tag_matches(if_match, entity_tag(review), weak=False)


IfMatch = Annotated[
    str | None,
    Header(
        description="Entity tag from an earlier response: the review is changed only if it "
        "still has it, otherwise the answer is 412."
    ),
]
IfNoneMatch = Annotated[
    str | None,
    Header(description="Entity tags the client already has: 304 if the review still has one."),
]
ENTITY_TAG_HEADERS: dict[str, Any] = {
    "ETag": {
        "description": "Entity tag of the review, for If-Match and If-None-Match",
        "schema": {"type": "string"},
    }
}
