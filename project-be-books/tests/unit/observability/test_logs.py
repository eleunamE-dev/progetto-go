import json
import logging
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from bookreviews.observability.logs import JsonFormatter, configure_logging, request_id_var


def _record(**attributes: object) -> logging.LogRecord:
    return logging.makeLogRecord(
        {"name": "bookreviews.test", "levelno": logging.INFO, "levelname": "INFO"} | attributes
    )


def test_formats_records_as_json_with_extra_fields() -> None:
    record = _record(msg="hello %s", args=("world",), attempt=2)

    entry = json.loads(JsonFormatter().format(record))

    assert entry["msg"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "bookreviews.test"
    assert entry["attempt"] == 2
    assert entry["time"].endswith("+00:00")
    assert "args" not in entry
    assert "request_id" not in entry


def test_values_json_cannot_hold_are_written_as_text() -> None:
    review_id = uuid.UUID("0190c1a0-0000-7000-8000-000000000000")
    record = _record(review_id=review_id, at=datetime(2026, 10, 6, 12, 0, tzinfo=UTC))

    entry = json.loads(JsonFormatter().format(record))

    assert entry["review_id"] == str(review_id)
    assert entry["at"] == "2026-10-06 12:00:00+00:00"


def test_drops_uvicorn_color_messages() -> None:
    record = _record(msg="started", color_message="\x1b[36mstarted\x1b[0m")

    entry = json.loads(JsonFormatter().format(record))

    assert "color_message" not in entry


def test_adds_the_current_request_id() -> None:
    token = request_id_var.set("req-42")
    try:
        entry = json.loads(JsonFormatter().format(_record(msg="hello")))
    finally:
        request_id_var.reset(token)

    assert entry["request_id"] == "req-42"


def test_formats_exceptions() -> None:
    error = ValueError("boom")
    record = _record(msg="failed", exc_info=(ValueError, error, None))

    entry = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in entry["exception"]


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.usefixtures("restore_root_logger")
def test_configure_logging_writes_json_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")

    logging.getLogger("bookreviews.test").info("dropped")
    logging.getLogger("bookreviews.test").warning("kept", extra={"attempt": 1})

    [line] = capsys.readouterr().out.splitlines()
    assert json.loads(line) | {"time": None} == {
        "time": None,
        "level": "WARNING",
        "logger": "bookreviews.test",
        "msg": "kept",
        "attempt": 1,
    }


@pytest.mark.usefixtures("restore_root_logger")
def test_command_line_tools_can_log_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", sys.stderr)

    logging.getLogger("bookreviews.test").info("to stderr")

    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["msg"] == "to stderr"
