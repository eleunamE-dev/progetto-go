import pytest
from pydantic import ValidationError

from bookreviews.config import Settings

VARIABLES = (
    "LOG_LEVEL",
    "HTTP_HOST",
    "HTTP_PORT",
    "HTTP_SHUTDOWN_TIMEOUT",
    "DATABASE_URL",
    "GUTENDEX_BASE_URL",
    "GUTENDEX_TIMEOUT",
    "CATALOG_CACHE_TTL",
    "CATALOG_CACHE_SIZE",
    "RABBITMQ_URL",
    "WORKER_CONCURRENCY",
    "ENRICHMENT_MAX_ATTEMPTS",
    "SWEEP_INTERVAL",
    "SWEEP_AFTER",
    "WORKER_SHUTDOWN_TIMEOUT",
)


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
    assert settings.database_url.get_secret_value() == (
        "mysql+aiomysql://user:password@localhost:3306/bookreviews"
    )
    assert str(settings.gutendex_base_url) == "https://gutendex.com/"
    assert settings.gutendex_timeout == 60
    assert settings.catalog_cache_ttl == 3600
    assert settings.catalog_cache_size == 10_000
    assert settings.rabbitmq_url.get_secret_value() == "amqp://user:password@localhost:5672/"
    assert settings.worker_concurrency == 4
    assert settings.enrichment_max_attempts == 5
    assert settings.sweep_interval == 60
    assert settings.sweep_after == 600
    assert settings.worker_shutdown_timeout == 15


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "9090")
    monkeypatch.setenv("HTTP_SHUTDOWN_TIMEOUT", "30")
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://app:secret@db:3306/reviews")
    monkeypatch.setenv("GUTENDEX_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("GUTENDEX_TIMEOUT", "2.5")
    monkeypatch.setenv("CATALOG_CACHE_TTL", "60")
    monkeypatch.setenv("CATALOG_CACHE_SIZE", "100")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://app:secret@broker:5672/")
    monkeypatch.setenv("WORKER_CONCURRENCY", "8")
    monkeypatch.setenv("ENRICHMENT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("SWEEP_INTERVAL", "30")
    monkeypatch.setenv("SWEEP_AFTER", "120")
    monkeypatch.setenv("WORKER_SHUTDOWN_TIMEOUT", "5")

    settings = Settings()

    assert settings.log_level == "DEBUG"
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 9090
    assert settings.http_shutdown_timeout == 30
    assert settings.database_url.get_secret_value() == "mysql+aiomysql://app:secret@db:3306/reviews"
    assert str(settings.gutendex_base_url) == "http://localhost:8000/"
    assert settings.gutendex_timeout == 2.5
    assert settings.catalog_cache_ttl == 60
    assert settings.catalog_cache_size == 100
    assert settings.rabbitmq_url.get_secret_value() == "amqp://app:secret@broker:5672/"
    assert settings.worker_concurrency == 8
    assert settings.enrichment_max_attempts == 3
    assert settings.sweep_interval == 30
    assert settings.sweep_after == 120
    assert settings.worker_shutdown_timeout == 5


def test_connection_urls_stay_out_of_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://app:secret@db:3306/reviews")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://app:secret@broker:5672/")

    assert "secret" not in repr(Settings())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOG_LEVEL", "loud"),
        ("HTTP_PORT", "http"),
        ("HTTP_PORT", "0"),
        ("HTTP_SHUTDOWN_TIMEOUT", "0"),
        ("GUTENDEX_BASE_URL", "gutendex.com"),
        ("GUTENDEX_TIMEOUT", "-1"),
        ("CATALOG_CACHE_TTL", "0"),
        ("CATALOG_CACHE_SIZE", "0"),
        ("WORKER_CONCURRENCY", "0"),
        ("ENRICHMENT_MAX_ATTEMPTS", "0"),
        ("SWEEP_INTERVAL", "0"),
        ("SWEEP_AFTER", "0"),
        ("WORKER_SHUTDOWN_TIMEOUT", "0"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=name.lower()):
        Settings()
