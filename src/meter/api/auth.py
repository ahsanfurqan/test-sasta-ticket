"""API key authentication. Owned by hot-path.

SESSION-1 SCAFFOLDING: a single hardcoded key from config, so the echo route can prove
the stack boots. Real auth resolves a per-customer key from cache -- never a synchronous
Postgres lookup on the request path (hot-path invariant #1). Open question #10 covers key
rotation, revocation staleness and hashing.
"""

import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from meter.config import get_settings

API_KEY_HEADER = "X-API-Key"


@dataclass(frozen=True)
class Caller:
    """The authenticated customer. A stub identity until customers exist."""

    customer_id: str


async def require_api_key(x_api_key: str | None = Header(default=None)) -> Caller:
    settings = get_settings()

    # Constant-time comparison: a timing side channel on key material is a real leak.
    # Never log the key itself.
    if x_api_key is None or not secrets.compare_digest(x_api_key, settings.dev_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
    return Caller(customer_id="dev-customer")
