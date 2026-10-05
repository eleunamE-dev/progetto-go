import hashlib
import hmac
import secrets
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from bookreviews.observability.logs import client_var

API_KEY_HEADER = "X-API-Key"
KEY_PREFIX = "bkr_"

api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    scheme_name="ApiKey",
    description="Key of the client application, required to create, change and delete reviews.",
    auto_error=False,
)


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


class ApiKeys:
    def __init__(self, keys: Mapping[str, Sequence[str]]) -> None:
        self._digests = [
            (client, bytes.fromhex(digest))
            for client, digests in keys.items()
            for digest in digests
        ]

    def client_for(self, key: str) -> str | None:
        presented = hashlib.sha256(key.encode()).digest()
        found = None
        for client, expected in self._digests:
            if hmac.compare_digest(presented, expected):
                found = client
        return found


async def authenticated_client(
    request: Request, key: Annotated[str | None, Depends(api_key_header)]
) -> str:
    keys: ApiKeys = request.app.state.api_keys
    client = None if key is None else keys.client_for(key)
    if client is None:
        raise HTTPException(
            HTTPStatus.UNAUTHORIZED,
            f"a valid {API_KEY_HEADER} header is required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    client_var.set(client)
    return client


Client = Annotated[str, Depends(authenticated_client)]
