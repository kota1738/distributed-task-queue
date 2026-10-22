"""Application settings, loaded from the environment (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-only-insecure-secret"


class Settings(BaseSettings):
    """Runtime configuration.

    Every field can be overridden with an environment variable of the same name.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://taskqueue:taskqueue@localhost:5432/taskqueue"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600

    environment: Literal["dev", "prod"] = "dev"

    queue_name: str = "taskqueue:jobs"
    processing_queue_name: str = "taskqueue:jobs:processing"

    worker_poll_interval_seconds: float = 1.0
    worker_lease_seconds: float = 300.0
    worker_concurrency: int = 1
    default_max_attempts: int = 3
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 60.0

    @field_validator("environment")
    @classmethod
    def _reject_unknown_environment(cls, value: str) -> str:
        if value not in ("dev", "prod"):
            raise ValueError("environment must be 'dev' or 'prod'")
        return value

    def validate_for_runtime(self) -> None:
        """Fail fast on unsafe configuration instead of shipping a default secret."""
        if self.environment == "prod" and self.jwt_secret == DEFAULT_JWT_SECRET:
            raise RuntimeError("JWT_SECRET must be set to a unique value when ENVIRONMENT=prod")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
