import pytest
from pydantic import ValidationError

from bookreviews.auth import key_digest
from bookreviews.config import Settings

DIGEST = key_digest("web-app-key")
OTHER_DIGEST = key_digest("batch-key")

VARIABLES = (
    "LOG_LEVEL",
    "HTTP_HOST",
    "HTTP_PORT",
    "HTTP_SHUTDOWN_TIMEOUT",
    "HTTP_MAX_BODY_SIZE",
    "API_KEYS",
    "API_DOCS_ENABLED",
    "CORS_ALLOW_ORIGINS",
    "DATABASE_URL",
    "DATABASE_POOL_SIZE",
    "DATABASE_MAX_OVERFLOW",
    "DATABASE_POOL_TIMEOUT",
    "GUTENDEX_BASE_URL",
    "GUTENDEX_TIMEOUT",
    "GUTENDEX_MAX_CONCURRENCY",
    "GUTENDEX_QUEUE_TIMEOUT",
    "GUTENDEX_FAILURE_THRESHOLD",
    "GUTENDEX_RESET_TIMEOUT",
    "CATALOG_CACHE_TTL",
    "CATALOG_CACHE_SIZE",
    "RABBITMQ_URL",
    "WORKER_CONCURRENCY",
    "ENRICHMENT_MAX_ATTEMPTS",
    "ENRICHMENT_DEADLINE",
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
    assert settings.http_max_body_size == 65_536
    assert settings.api_keys == {}
    assert settings.api_docs_enabled
    assert settings.cors_allow_origins == []
    assert settings.database_url.get_secret_value() == (
        "mysql+aiomysql://app:app-password@localhost:3306/bookreviews"
    )
    assert settings.database_pool_size == 5
    assert settings.database_max_overflow == 10
    assert settings.database_pool_timeout == 10
    assert str(settings.gutendex_base_url) == "https://gutendex.com/"
    assert settings.gutendex_timeout == 60
    assert settings.gutendex_max_concurrency == 8
    assert settings.gutendex_queue_timeout == 10
    assert settings.gutendex_failure_threshold == 5
    assert settings.gutendex_reset_timeout == 30
    assert settings.catalog_cache_ttl == 3600
    assert settings.catalog_cache_size == 10_000
    assert settings.rabbitmq_url.get_secret_value() == "amqp://user:password@localhost:5672/"
    assert settings.worker_concurrency == 4
    assert settings.enrichment_max_attempts == 5
    assert settings.enrichment_deadline == 86_400
    assert settings.sweep_interval == 60
    assert settings.sweep_after == 600
    assert settings.worker_shutdown_timeout == 15


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "9090")
    monkeypatch.setenv("HTTP_SHUTDOWN_TIMEOUT", "30")
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://app:secret@db:3306/reviews")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "20")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DATABASE_POOL_TIMEOUT", "2.5")
    monkeypatch.setenv("GUTENDEX_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("GUTENDEX_TIMEOUT", "2.5")
    monkeypatch.setenv("GUTENDEX_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("GUTENDEX_QUEUE_TIMEOUT", "1.5")
    monkeypatch.setenv("GUTENDEX_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("GUTENDEX_RESET_TIMEOUT", "120")
    monkeypatch.setenv("CATALOG_CACHE_TTL", "60")
    monkeypatch.setenv("CATALOG_CACHE_SIZE", "100")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://app:secret@broker:5672/")
    monkeypatch.setenv("WORKER_CONCURRENCY", "8")
    monkeypatch.setenv("ENRICHMENT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("ENRICHMENT_DEADLINE", "3600")
    monkeypatch.setenv("SWEEP_INTERVAL", "30")
    monkeypatch.setenv("SWEEP_AFTER", "120")
    monkeypatch.setenv("WORKER_SHUTDOWN_TIMEOUT", "5")

    settings = Settings()

    assert settings.log_level == "DEBUG"
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 9090
    assert settings.http_shutdown_timeout == 30
    assert settings.database_url.get_secret_value() == "mysql+aiomysql://app:secret@db:3306/reviews"
    assert settings.database_pool_size == 20
    assert settings.database_max_overflow == 0
    assert settings.database_pool_timeout == 2.5
    assert str(settings.gutendex_base_url) == "http://localhost:8000/"
    assert settings.gutendex_timeout == 2.5
    assert settings.gutendex_max_concurrency == 2
    assert settings.gutendex_queue_timeout == 1.5
    assert settings.gutendex_failure_threshold == 3
    assert settings.gutendex_reset_timeout == 120
    assert settings.catalog_cache_ttl == 60
    assert settings.catalog_cache_size == 100
    assert settings.rabbitmq_url.get_secret_value() == "amqp://app:secret@broker:5672/"
    assert settings.worker_concurrency == 8
    assert settings.enrichment_max_attempts == 3
    assert settings.enrichment_deadline == 3600
    assert settings.sweep_interval == 30
    assert settings.sweep_after == 120
    assert settings.worker_shutdown_timeout == 5


def test_environment_sets_the_http_security_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_MAX_BODY_SIZE", "1024")
    monkeypatch.setenv("API_KEYS", f'{{"web-app": "{DIGEST}", "batch-2": "{OTHER_DIGEST}"}}')
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    monkeypatch.setenv(
        "CORS_ALLOW_ORIGINS", '["https://reviews.example.com", "http://localhost:3000"]'
    )

    settings = Settings()

    assert settings.http_max_body_size == 1024
    assert settings.api_keys == {"web-app": [DIGEST], "batch-2": [OTHER_DIGEST]}
    assert not settings.api_docs_enabled
    assert settings.cors_allow_origins == ["https://reviews.example.com", "http://localhost:3000"]


def test_secrets_stay_out_of_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://app:secret@db:3306/reviews")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://app:secret@broker:5672/")
    monkeypatch.setenv("API_KEYS", f'{{"web-app": "{DIGEST}"}}')

    shown = repr(Settings())

    assert "secret" not in shown
    assert DIGEST not in shown


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOG_LEVEL", "loud"),
        ("HTTP_PORT", "http"),
        ("HTTP_PORT", "0"),
        ("HTTP_SHUTDOWN_TIMEOUT", "0"),
        ("HTTP_MAX_BODY_SIZE", "0"),
        ("API_KEYS", f'{{"Web App": "{DIGEST}"}}'),
        ("API_KEYS", f'{{"-web": "{DIGEST}"}}'),
        ("API_KEYS", '{"web-app": "not-a-sha256-digest"}'),
        ("API_KEYS", '{"web-app": []}'),
        ("API_KEYS", '["web-app"]'),
        ("API_KEYS", f'{{"web-app": "{DIGEST.upper()}"}}'),
        ("API_DOCS_ENABLED", "maybe"),
        ("CORS_ALLOW_ORIGINS", '["https://reviews.example.com/app"]'),
        ("CORS_ALLOW_ORIGINS", '["reviews.example.com"]'),
        ("DATABASE_POOL_SIZE", "0"),
        ("DATABASE_MAX_OVERFLOW", "-1"),
        ("DATABASE_POOL_TIMEOUT", "0"),
        ("GUTENDEX_BASE_URL", "gutendex.com"),
        ("GUTENDEX_TIMEOUT", "-1"),
        ("GUTENDEX_MAX_CONCURRENCY", "0"),
        ("GUTENDEX_QUEUE_TIMEOUT", "0"),
        ("GUTENDEX_FAILURE_THRESHOLD", "0"),
        ("GUTENDEX_RESET_TIMEOUT", "0"),
        ("CATALOG_CACHE_TTL", "0"),
        ("CATALOG_CACHE_SIZE", "0"),
        ("WORKER_CONCURRENCY", "0"),
        ("ENRICHMENT_MAX_ATTEMPTS", "0"),
        ("ENRICHMENT_DEADLINE", "0"),
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


def test_the_enrichment_deadline_must_outlast_the_sweeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWEEP_AFTER", "600")
    monkeypatch.setenv("ENRICHMENT_DEADLINE", "600")

    with pytest.raises(ValidationError, match="must be longer than sweep_after"):
        Settings()


def test_a_client_may_have_several_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", f'{{"web-app": ["{DIGEST}", "{OTHER_DIGEST}"]}}')

    assert Settings().api_keys == {"web-app": [DIGEST, OTHER_DIGEST]}


@pytest.mark.parametrize(
    "api_keys",
    [
        f'{{"web-app": "{DIGEST}", "batch": "{DIGEST}"}}',
        f'{{"web-app": ["{DIGEST}", "{DIGEST}"]}}',
    ],
)
def test_an_api_key_appears_only_once(monkeypatch: pytest.MonkeyPatch, api_keys: str) -> None:
    monkeypatch.setenv("API_KEYS", api_keys)

    with pytest.raises(ValidationError, match="an API key must appear only once"):
        Settings()


def test_any_origin_can_be_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", '["*"]')

    assert Settings().cors_allow_origins == ["*"]
