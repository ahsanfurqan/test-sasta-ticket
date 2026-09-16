"""Configuration, read from the environment. See .env.example."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://meter:meter@postgres:5432/meter"
    redis_url: str = "redis://redis:6379/0"

    db_pool_min: int = 2
    db_pool_max: int = 10

    # Every external call on the request path needs a timeout (hot-path invariant:
    # no unbounded wait). These are the defaults; the real budget is open question #9.
    db_timeout_seconds: float = 2.0
    redis_timeout_seconds: float = 1.0

    # SESSION-1 SCAFFOLDING. A single hardcoded key standing in for real API-key
    # auth, so the echo route can prove the stack boots. Real keys are per customer,
    # stored hashed, and resolved from cache -- see open question #10.
    dev_api_key: str = "dev-key-change-me"


@lru_cache
def get_settings() -> Settings:
    return Settings()
