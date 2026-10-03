import pytest
from pydantic import ValidationError

from bookreviews.config import Settings

VARIABLES = ("LOG_LEVEL", "HTTP_HOST", "HTTP_PORT", "HTTP_SHUTDOWN_TIMEOUT")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_need_no_configuration() -> None:
    settings = Settings()

    assert settings.log_level == "INFO"
    assert settings.http_host == "0.0.0.0"  # noqa: S104
    assert settings.http_port == 8080
    assert settings.http_shutdown_timeout == 15


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "9090")
    monkeypatch.setenv("HTTP_SHUTDOWN_TIMEOUT", "30")

    settings = Settings()

    assert settings.log_level == "DEBUG"
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 9090
    assert settings.http_shutdown_timeout == 30


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOG_LEVEL", "loud"),
        ("HTTP_PORT", "http"),
        ("HTTP_PORT", "0"),
        ("HTTP_SHUTDOWN_TIMEOUT", "0"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=name.lower()):
        Settings()
