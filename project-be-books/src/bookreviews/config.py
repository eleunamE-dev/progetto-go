from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True)

    log_level: LogLevel = "INFO"
    http_host: str = "0.0.0.0"  # noqa: S104
    http_port: int = Field(default=8080, ge=1, le=65535)
    http_shutdown_timeout: int = Field(default=15, gt=0)
    database_url: SecretStr = SecretStr("mysql+aiomysql://user:password@localhost:3306/bookreviews")
    gutendex_base_url: HttpUrl = HttpUrl("https://gutendex.com")
    gutendex_timeout: float = Field(default=60, gt=0)
    catalog_cache_ttl: float = Field(default=3600, gt=0)
    catalog_cache_size: int = Field(default=10_000, gt=0)

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value
