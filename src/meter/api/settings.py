"""Hot-path knobs. Owned by hot-path.

Separate from `meter.config` deliberately: these are not deployment trivia. Each one is a
parameter of a decision that has an ADR behind it, and changing it changes a stated
guarantee rather than a preference.

  * `auth_cache_ttl_seconds` IS the revocation window (ADR-0015). Raising it lengthens the
    time a revoked key keeps working.
  * `retry_after_seconds` is what a failed-closed customer is told to wait (ADR-0011).
  * `capture_enabled` exists only to produce ADR-0014's baseline: the same endpoint with
    capture off, so "what did this add to p99?" has a measured answer rather than an
    opinion. It is never off in a deployment that bills anyone.

The stream bound -- the depth at which a Postgres outage becomes a total outage (ADR-0018)
-- deliberately does NOT live here. It is in `meter.config` alongside the stream key,
because `pipeline` alerts against the same number and the two sides must not be able to
disagree about it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class HotPathSettings:
    #: ADR-0015: revocation is effective within this window, because this is how long a
    #: resolved key lives in the auth cache.
    auth_cache_ttl_seconds: int = 30
    # ADR-0019: how long a POSITIVE auth entry may be served after its TTL, but only while
    # Postgres is unreachable. This is the revocation window during an outage, so it is a
    # security parameter -- raising it erodes ADR-0015's guarantee by increments.
    auth_stale_ceiling_seconds: int = 900

    #: ADR-0014's baseline mode. Off = no limit check, no counter, no XADD.
    capture_enabled: bool = True

    #: ADR-0011: what we tell a customer we refused.
    retry_after_seconds: int = 1

    #: Admin/demo surface. When unset, the provisioning endpoints are available only in a
    #: local environment -- they are not a customer-facing product.
    admin_token: str | None = None

    #: Registers DEV_API_KEY as a real, hashed key row at startup so the development key
    #: in `.env` goes through exactly the same path as a customer's. Local only.
    bootstrap_dev_key: bool = True

    @classmethod
    def from_env(cls) -> HotPathSettings:
        return cls(
            auth_cache_ttl_seconds=_int_env("AUTH_CACHE_TTL_SECONDS", 30),
            auth_stale_ceiling_seconds=_int_env("AUTH_STALE_CEILING_SECONDS", 900),
            capture_enabled=_bool_env("CAPTURE_ENABLED", True),
            retry_after_seconds=_int_env("RETRY_AFTER_SECONDS", 1),
            admin_token=os.environ.get("ADMIN_TOKEN") or None,
            bootstrap_dev_key=_bool_env("BOOTSTRAP_DEV_KEY", True),
        )
