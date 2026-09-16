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
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError

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


class PostgresUnavailable(Exception):
    """Postgres could not answer, and no stale entry was licensed to stand in for it."""


def encode(record: keys_repo.KeyRecord, fresh_until: float) -> str:
    """Cache encoding: `customer_id|key_id|key_hash|fresh_until`.

    A four-field split beats JSON here by enough to matter on a path measured in
    microseconds, and what it holds is the digest Postgres holds -- never the key.

    The entry carries TWO ages (ADR-0019). `fresh_until` is when it stops being trusted
    outright; the Redis TTL is the later ceiling past which it is not served at all. Between
    them the entry is refreshed on access, and served stale only if Postgres is unreachable.
    """
    # FLOOR the deadline, never round it. Rounding to nearest can push a revocation window
    # past the 30 seconds ADR-0015 promises, and a security parameter must only ever err
    # short.
    return f"{record.customer_id}|{record.key_id}|{record.key_hash}|{int(fresh_until)}"


def decode(cached: str, digest: str) -> Caller | None:
    """Decode a cached entry, confirming the digest in constant time.

    Freshness is not consulted here: this answers *who* the entry names, and `resolve`
    decides whether the entry is still allowed to speak.
    """
    if cached.startswith(UNKNOWN):
        return None
    customer_id, _, rest = cached.partition("|")
    key_id, _, rest = rest.partition("|")
    key_hash, _, _ = rest.partition("|")
    if not key_hash or not secrets.compare_digest(key_hash, digest):
        return None
    return Caller(customer_id=customer_id, api_key_id=key_id)


def fresh_until(cached: str) -> float:
    """When this entry stops being trusted outright.

    A NEGATIVE entry is fresh for as long as it exists: it is written with the short TTL and
    never served stale (ADR-0019), so its presence and its freshness are the same fact. Only
    positive entries carry the longer stale ceiling and therefore need a deadline inside them.

    An unreadable deadline is treated as already stale -- refreshing needlessly is a wasted
    query; trusting a value we cannot parse is an auth decision made on a guess.
    """
    if cached.startswith(UNKNOWN):
        return float("inf")
    try:
        return float(cached.rpartition("|")[2])
    except ValueError:
        return 0.0


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

    Raises `PostgresUnavailable` when the directory cannot be reached and no stale entry is
    licensed to stand in for it (ADR-0019).
    """
    now = time.time()

    if cached is not None and now < fresh_until(cached):
        return decode(cached, digest)  # fresh: the whole cost is the read already made

    try:
        record = await keys_repo.lookup_by_hash(
            context.sessions, digest, timeout_seconds=context.settings.db_timeout_seconds
        )
    except (SQLAlchemyError, TimeoutError, OSError) as exc:
        # ADR-0019: stale-while-error. Three restrictions, all deliberate.
        #   * only a Postgres FAILURE licenses staleness -- a key Postgres positively reports
        #     as revoked is revoked, outage or not. This branch is reached only on an error.
        #   * negatives are never served stale: an outage must not turn "no such key" into
        #     "maybe", so an UNKNOWN entry decodes to None and is refused below.
        #   * past the Redis TTL there is no entry at all, which is the ceiling.
        stale = decode(cached, digest) if cached is not None else None
        if stale is not None:
            logger.warning(
                "DEGRADED: serving a stale auth entry, the key directory is unreachable (%s). "
                "Revocation is not effective until it returns (ADR-0019).",
                type(exc).__name__,
            )
            return stale
        raise PostgresUnavailable(str(exc)) from exc

    live = record is not None and record.active and secrets.compare_digest(record.key_hash, digest)
    value = (
        encode(record, now + context.hot.auth_cache_ttl_seconds)  # type: ignore[arg-type]
        if live
        else UNKNOWN
    )

    await context.cache.set(
        keys_repo.auth_cache_key(digest),
        value,
        # The Redis TTL is the STALE CEILING, not the freshness window -- freshness is the
        # timestamp inside the value. A negative entry gets only the short TTL, because it
        # is never served stale and so has nothing to stay alive for.
        ex=(
            context.hot.auth_stale_ceiling_seconds
            if live
            else context.hot.auth_cache_ttl_seconds
        ),
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
