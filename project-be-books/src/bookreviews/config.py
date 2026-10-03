from typing import Literal, Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True)

    log_level: LogLevel = "INFO"
    http_host: str = "0.0.0.0"  # noqa: S104
    http_port: int = Field(default=8080, ge=1, le=65535)
    http_shutdown_timeout: int = Field(default=15, gt=0)
    database_url: SecretStr = SecretStr("mysql+aiomysql://user:password@localhost:3306/bookreviews")
    database_pool_size: int = Field(default=5, gt=0)
    database_max_overflow: int = Field(default=10, ge=0)
    database_pool_timeout: float = Field(default=10, gt=0)
    rabbitmq_url: SecretStr = SecretStr("amqp://user:password@localhost:5672/")
    gutendex_base_url: HttpUrl = HttpUrl("https://gutendex.com")
    gutendex_timeout: float = Field(default=60, gt=0)
    gutendex_max_concurrency: int = Field(default=8, gt=0)
    gutendex_queue_timeout: float = Field(default=10, gt=0)
    gutendex_failure_threshold: int = Field(default=5, gt=0)
    gutendex_reset_timeout: float = Field(default=30, gt=0)
    catalog_cache_ttl: float = Field(default=3600, gt=0)
    catalog_cache_size: int = Field(default=10_000, gt=0)
    worker_concurrency: int = Field(default=4, gt=0)
    enrichment_max_attempts: int = Field(default=5, gt=0)
    enrichment_deadline: float = Field(default=86_400, gt=0)
    sweep_interval: float = Field(default=60, gt=0)
    sweep_after: float = Field(default=600, gt=0)
    worker_shutdown_timeout: float = Field(default=15, gt=0)

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _deadline_outlasts_the_sweeps(self) -> Self:
        if self.enrichment_deadline <= self.sweep_after:
            raise ValueError("enrichment_deadline must be longer than sweep_after")
        return self
