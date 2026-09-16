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

    # -----------------------------------------------------------------------------------
    # The hot-path/pipeline contract (ADR-0018). These names are shared: `hot-path` XADDs
    # to `usage_stream_key` and reads the counter and threshold keys; `pipeline` reads the
    # stream through a consumer group and writes the counter and threshold keys.
    # The key SHAPES live in meter.pipeline.keys, which is documentation as much as code.
    # -----------------------------------------------------------------------------------
    usage_stream_key: str = "usage:events"
    usage_stream_group: str = "drain"
    # ADR-0018 bounds the stream on purpose: an unbounded stream exhausts Redis memory,
    # which by ADR-0011 takes the whole API down -- turning a recoverable Postgres outage
    # into a total one. `pipeline` alerts at usage_stream_alert_ratio of this.
    usage_stream_max_entries: int = 1_000_000
    usage_stream_alert_ratio: float = 0.8

    # Drain. Batch size is the redelivery unit: a killed worker leaves at most this many
    # entries pending, and every one of them is safe to redeliver (idempotency key).
    # 1,000 is measured, not guessed: the drain's insert is one statement with one array
    # per column, so batch size trades redelivery unit against round trips rather than
    # hitting a parameter ceiling. See the note above INSERT_USAGE_SQL.
    drain_batch_size: int = 1_000
    # Strictly below redis_timeout_seconds: the server holds a BLOCKed XREADGROUP for this
    # long, and the client gives up on the socket after its own timeout. If the two are
    # equal they race on every idle poll, and the drain logs a timeout once a second while
    # nothing whatsoever is wrong.
    drain_block_ms: int = 500
    # A consumer that has not touched its pending entries for this long is assumed dead
    # and its batch is reclaimed by XAUTOCLAIM.
    drain_reclaim_idle_ms: int = 30_000
    drain_consumer_name: str = ""  # defaults to the hostname

    # Aggregation. The overlap re-reads a little history every pass; it is free because
    # the rollup upsert assigns rather than adds, so re-aggregating a day is a no-op.
    aggregate_interval_seconds: float = 2.0
    aggregate_overlap_seconds: int = 60

    # ADR-0008: a threshold is only as good as its freshness, and a missed recomputation
    # is a silent enforcement failure. Sweep often, and alert when the oldest threshold
    # is older than the max age (ADR-0018 wants this alert distinct from Postgres health).
    threshold_interval_seconds: float = 5.0
    threshold_max_age_seconds: int = 60

    # ADR-0010: reconcile, then issue -- with a bounded grace window as the fallback.
    # A config value, not a constant, because the right number depends on observed drain
    # latency we do not have yet.
    month_close_grace_seconds: float = 120.0
    month_close_poll_seconds: float = 1.0
    reconcile_interval_seconds: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
