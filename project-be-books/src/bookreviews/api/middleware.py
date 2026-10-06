import logging
import re
import time
import uuid
from http import HTTPStatus

from fastapi.routing import iter_route_contexts
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bookreviews.api.problems import problem_response
from bookreviews.observability import metrics
from bookreviews.observability.logs import client_var, request_id_var

REQUEST_ID_HEADER = "X-Request-ID"
CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"

_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

logger = logging.getLogger("bookreviews.http")


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    if route is None:
        route = next(
            (
                context
                for context in iter_route_contexts(scope["app"].routes)
                if context.matches(scope)[0] is not Match.NONE
            ),
            None,
        )
    template: str = getattr(route, "path", None) or "unmatched"
    return template


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        if not _VALID_REQUEST_ID.fullmatch(request_id):
            request_id = uuid.uuid4().hex
        token = request_id_var.set(request_id)
        client_token = client_var.set(None)
        start = time.perf_counter()
        status = HTTPStatus.INTERNAL_SERVER_ERROR.value
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception("unhandled error")
            if response_started:
                raise
            response = problem_response(HTTPStatus.INTERNAL_SERVER_ERROR, scope["path"])
            await response(scope, receive, send_with_request_id)
        finally:
            duration = time.perf_counter() - start
            route = _route_template(scope)
            metrics.http_requests.labels(scope["method"], route, str(status)).inc()
            metrics.http_request_duration.labels(scope["method"], route).observe(duration)
            logger.log(
                logging.ERROR if status >= HTTPStatus.INTERNAL_SERVER_ERROR else logging.INFO,
                "http request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round(duration * 1000, 3),
                },
            )
            client_var.reset(client_token)
            request_id_var.reset(token)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_size: int) -> None:
        self.app = app
        self.max_size = max_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        detail = f"the request body must not exceed {self.max_size} bytes"
        length = Headers(scope=scope).get("content-length", "")
        if length.isdigit() and int(length) > self.max_size:
            response = problem_response(HTTPStatus.CONTENT_TOO_LARGE, scope["path"], detail)
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_size:
                    raise HTTPException(HTTPStatus.CONTENT_TOO_LARGE, detail)
            return message

        await self.app(scope, limited_receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cache-Control", "no-store")
                if not headers.get("content-type", "").startswith("text/html"):
                    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            await send(message)

        await self.app(scope, receive, send_with_headers)
