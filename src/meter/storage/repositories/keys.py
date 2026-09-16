"""Customers and API keys: the identity the hot path resolves a request to.

ADR-0015 in one paragraph: multiple live keys per customer, stored as a SHA-256 hex digest
and never in plaintext, shown once at creation, revoked by timestamp rather than by DELETE
so a charge from a key that no longer exists is still traceable.

Exactly ONE statement here runs anywhere near a customer request -- `lookup_by_hash`, on an
auth cache miss -- and it is a unique-index probe. Everything else is provisioning.

Nothing in this module logs, returns or accepts key material except the caller-supplied
secret at the moment it is hashed, and the freshly generated secret at the moment it is
handed back to be shown once.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: `mk_` + 8 hex characters of public id. Non-secret: it identifies a key in a list, a log
#: line or a support conversation, and reveals nothing about the secret half.
KEY_PREFIX_BYTES = 4
KEY_SECRET_BYTES = 32


def hash_key(presented: str) -> str:
    """SHA-256 hex digest of a presented key. The only representation we ever store.

    The database CHECK constraint refuses anything that is not a 64-character hex digest,
    which is specifically a defence against a bug writing a plaintext key here.
    """
    return hashlib.sha256(presented.encode("utf-8")).hexdigest()


def auth_cache_key(key_hash: str) -> str:
    """Redis key for a resolved (or resolved-absent) API key.

    Keyed by the digest, never by the key. What Redis holds is what Postgres holds.
    """
    return f"meter:auth:{key_hash}"


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """A newly minted key. `secret` is the only time the full key exists outside the
    customer's hands -- it is returned once and never stored."""

    key_id: str
    customer_id: str
    secret: str
    prefix: str
    key_hash: str


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """What auth resolves to. No key material beyond the digest that was looked up."""

    key_id: str
    customer_id: str
    key_hash: str
    revoked_at: datetime | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def generate_key() -> tuple[str, str]:
    """A new key and its non-secret prefix: ``mk_<pubid>_<secret>``, ``mk_<pubid>``."""
    public_id = secrets.token_hex(KEY_PREFIX_BYTES)
    secret = secrets.token_urlsafe(KEY_SECRET_BYTES)
    prefix = f"mk_{public_id}"
    return f"{prefix}_{secret}", prefix


# ---------------------------------------------------------------------------------------
# The one hot-path-adjacent query: resolve a key hash. Runs on an auth cache MISS only.
# ---------------------------------------------------------------------------------------

_LOOKUP_BY_HASH = text(
    """
    SELECT id::text AS id, customer_id::text AS customer_id, key_hash, revoked_at
      FROM api_keys
     WHERE key_hash = :key_hash
    """
)


async def lookup_by_hash(
    session_factory: async_sessionmaker[AsyncSession],
    key_hash: str,
    *,
    timeout_seconds: float,
) -> KeyRecord | None:
    """One index probe on `uq_api_keys_key_hash`. Bounded by an explicit timeout: a slow
    Postgres must degrade this request, not hang the worker (hot-path invariant #4)."""

    async def _run() -> KeyRecord | None:
        async with session_factory() as session:
            row = (await session.execute(_LOOKUP_BY_HASH, {"key_hash": key_hash})).first()
        if row is None:
            return None
        return KeyRecord(
            key_id=row.id,
            customer_id=row.customer_id,
            key_hash=row.key_hash,
            revoked_at=row.revoked_at,
        )

    return await asyncio.wait_for(_run(), timeout=timeout_seconds)


# ---------------------------------------------------------------------------------------
# Provisioning. Admin surface only -- none of this runs on a customer request.
# ---------------------------------------------------------------------------------------

_INSERT_CUSTOMER = text(
    "INSERT INTO customers (name) VALUES (:name) RETURNING id::text AS id"
)

_INSERT_KEY = text(
    """
    INSERT INTO api_keys (customer_id, key_hash, prefix, label)
    VALUES (:customer_id, :key_hash, :prefix, :label)
    ON CONFLICT (key_hash) DO NOTHING
    RETURNING id::text AS id
    """
)

_SELECT_KEY_BY_HASH_ID = text("SELECT id::text AS id FROM api_keys WHERE key_hash = :key_hash")

_REVOKE_KEY = text(
    """
    UPDATE api_keys
       SET revoked_at = now()
     WHERE id = :key_id AND revoked_at IS NULL
    RETURNING id::text AS id
    """
)

_LIST_KEYS = text(
    """
    SELECT id::text AS id, prefix, label, created_at, revoked_at
      FROM api_keys
     WHERE customer_id = :customer_id
     ORDER BY created_at
    """
)

_CUSTOMER_EXISTS = text("SELECT id::text AS id, name FROM customers WHERE id = :customer_id")

_CUSTOMER_BY_NAME = text("SELECT id::text AS id FROM customers WHERE name = :name")


async def create_customer(
    session_factory: async_sessionmaker[AsyncSession], name: str
) -> str:
    async with session_factory() as session:
        customer_id = (await session.execute(_INSERT_CUSTOMER, {"name": name})).scalar_one()
        await session.commit()
    return customer_id


async def find_customer_by_name(
    session_factory: async_sessionmaker[AsyncSession], name: str
) -> str | None:
    async with session_factory() as session:
        return (await session.execute(_CUSTOMER_BY_NAME, {"name": name})).scalar_one_or_none()


async def customer_exists(
    session_factory: async_sessionmaker[AsyncSession], customer_id: str
) -> bool:
    async with session_factory() as session:
        row = (await session.execute(_CUSTOMER_EXISTS, {"customer_id": customer_id})).first()
    return row is not None


async def issue_key(
    session_factory: async_sessionmaker[AsyncSession],
    customer_id: str,
    *,
    label: str | None = None,
    secret: str | None = None,
    prefix: str | None = None,
) -> IssuedKey:
    """Mint (or adopt) a key for a customer and store only its digest.

    `secret` is an argument solely so the local development key from `.env` can be
    registered as a real row at startup; every customer-facing path generates it here.
    """
    if secret is None:
        secret, prefix = generate_key()
    elif prefix is None:
        prefix = secret[:12]

    key_hash = hash_key(secret)
    async with session_factory() as session:
        key_id = (
            await session.execute(
                _INSERT_KEY,
                {
                    "customer_id": customer_id,
                    "key_hash": key_hash,
                    "prefix": prefix,
                    "label": label,
                },
            )
        ).scalar_one_or_none()
        if key_id is None:  # already registered (the dev key on a second boot)
            key_id = (
                await session.execute(_SELECT_KEY_BY_HASH_ID, {"key_hash": key_hash})
            ).scalar_one()
        await session.commit()

    return IssuedKey(
        key_id=key_id,
        customer_id=customer_id,
        secret=secret,
        prefix=prefix,
        key_hash=key_hash,
    )


async def revoke_key(
    session_factory: async_sessionmaker[AsyncSession], key_id: str
) -> bool:
    """Revocation is a timestamp, never a DELETE. Returns False if it was already revoked.

    Deliberately does NOT invalidate the auth cache. ADR-0015 makes the revocation window
    the cache TTL (30s) and rejects push invalidation for v1; a DEL here would make
    revocation look instant on a single instance and still be 30s on a fleet, which is a
    guarantee that silently depends on topology.
    """
    async with session_factory() as session:
        revoked = (await session.execute(_REVOKE_KEY, {"key_id": key_id})).scalar_one_or_none()
        await session.commit()
    return revoked is not None


async def list_keys(
    session_factory: async_sessionmaker[AsyncSession], customer_id: str
) -> list[dict]:
    async with session_factory() as session:
        rows = (await session.execute(_LIST_KEYS, {"customer_id": customer_id})).all()
    return [
        {
            "key_id": row.id,
            "prefix": row.prefix,
            "label": row.label,
            "created_at": row.created_at.isoformat(),
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        }
        for row in rows
    ]
