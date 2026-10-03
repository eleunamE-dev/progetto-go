import logging
import re
import time
import uuid
from http import HTTPStatus

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bookreviews.logs import request_id_var
from bookreviews.problems import problem_response

REQUEST_ID_HEADER = "X-Request-ID"

_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

logger = logging.getLogger("bookreviews.http")


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
            logger.log(
                logging.ERROR if status >= HTTPStatus.INTERNAL_SERVER_ERROR else logging.INFO,
                "http request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 3),
                },
            )
            request_id_var.reset(token)
