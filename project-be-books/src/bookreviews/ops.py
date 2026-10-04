import asyncio
import contextlib
import logging
from http import HTTPStatus

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

READ_TIMEOUT = 5.0

logger = logging.getLogger("bookreviews.ops")


class OpsServer:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        logger.info("ops server listening", extra={"port": self.port})

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(READ_TIMEOUT):
                head = await reader.readuntil(b"\r\n\r\n")
            method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            status, content_type, body = self._answer(method, target.split("?", 1)[0])
            writer.write(
                f"HTTP/1.1 {status.value} {status.phrase}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode("latin-1")
                + body
            )
            await writer.drain()
        except (
            TimeoutError,
            ValueError,
            ConnectionError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    @staticmethod
    def _answer(method: str, path: str) -> tuple[HTTPStatus, str, bytes]:
        if method != "GET":
            return HTTPStatus.METHOD_NOT_ALLOWED, "text/plain", b"method not allowed\n"
        if path == "/metrics":
            return HTTPStatus.OK, CONTENT_TYPE_LATEST, generate_latest()
        if path == "/healthz":
            return HTTPStatus.OK, "application/json", b'{"status": "ok"}'
        return HTTPStatus.NOT_FOUND, "text/plain", b"not found\n"
