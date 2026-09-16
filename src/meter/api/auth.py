"""API key authentication. Owned by hot-path. See docs/adr/0015-api-key-model.md.

The shape of this, and why:

    presented key -> SHA-256 digest -> Redis GET (30s TTL) -> Caller

On a cache HIT that is the whole cost: one Redis read, already pipelined with the other
reads the request needs, plus a `compare_digest`. On a MISS it costs exactly one Postgres
probe of a unique index, and the result is cached -- including the *negative* result, so a
client spraying invalid keys cannot turn itself into one Postgres read per request.

Three things this module will not do:

  * log key material, or put it in an exception, a span or a metric label;
  * compare key material with `==`;
  * make a Postgres call on a cache hit (hot-path invariant #1).

The staleness window is stated rather than incidental: a key revoked at T stops working by
T+`auth_cache_ttl_seconds` (30s, ADR-0015), on every instance independently, because the
entry is shared in Redis rather than held per process.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from meter.api.context import HotPathContext
from meter.storage.repositories import keys as keys_repo

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"
_API_KEY_HEADER_BYTES = b"x-api-key"

#: Cached value for "this digest matches no live key". Short, unambiguous, and cheaper to
#: branch on than an empty record.
UNKNOWN = "-"


@dataclass(frozen=True, slots=True)
class Caller:
    """The authenticated customer, and which of their keys presented itself."""

    customer_id: str
    api_key_id: str


def api_key_from_headers(scope_headers: list[tuple[bytes, bytes]]) -> str | None:
    """Pull X-API-Key straight out of the raw ASGI headers.

    Deliberately not via `Request(scope).headers`: this runs before anything else on every
    metered request, and building a framework object to read one header is a cost with no
    return.
    """
    for name, value in scope_headers:
        if name == _API_KEY_HEADER_BYTES:
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes any byte
                return None
    return None


def encode(record: keys_repo.KeyRecord) -> str:
    """Cache encoding: `customer_id|key_id|key_hash`.

    A three-field split beats JSON here by enough to matter on a path measured in
    microseconds, and what it holds is the digest Postgres holds -- never the key.
    """
    return f"{record.customer_id}|{record.key_id}|{record.key_hash}"


def decode(cached: str, digest: str) -> Caller | None:
    """Decode a cached entry, confirming the digest in constant time."""
    if cached == UNKNOWN:
        return None
    customer_id, _, rest = cached.partition("|")
    key_id, _, key_hash = rest.partition("|")
    if not key_hash or not secrets.compare_digest(key_hash, digest):
        return None
    return Caller(customer_id=customer_id, api_key_id=key_id)


async def resolve(
    context: HotPathContext,
    presented: str,
    digest: str,
    cached: str | None,
) -> Caller | None:
    """Resolve a presented key to a caller, or None if it is not a live key.

    `cached` is the value the middleware already fetched in its first Redis round trip --
    passing it in rather than reading it here is what keeps auth from costing a round trip
    of its own.

    Raises the underlying Redis error on a cache write failure: an API that cannot reach
    Redis fails closed (ADR-0011), and swallowing it here would serve the request instead.
    """
    if cached is not None:
        return decode(cached, digest)

    record = await keys_repo.lookup_by_hash(
        context.sessions, digest, timeout_seconds=context.settings.db_timeout_seconds
    )

    live = record is not None and record.active and secrets.compare_digest(record.key_hash, digest)
    value = encode(record) if live else UNKNOWN  # type: ignore[arg-type]

    await context.cache.set(
        keys_repo.auth_cache_key(digest),
        value,
        ex=context.hot.auth_cache_ttl_seconds,
    )
    return decode(value, digest) if live else None


async def require_api_key(request: Request) -> Caller:
    """FastAPI dependency: the caller the middleware already resolved.

    Authentication happens once, in `meter.api.metering`, because the same resolution
    decides whether the request is served at all, which counter it moves and which customer
    the usage event names. Re-resolving it here would be a second Redis read per request
    for an answer we are already holding.
    """
    caller = getattr(request.state, "caller", None)
    if caller is None:  # a route outside the metered surface asking for a caller
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
    return caller
