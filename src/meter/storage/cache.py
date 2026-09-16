"""Redis client. Owned by data-model.

Redis holds hot-path counters and limit thresholds. It is a cache and a buffer, never
the system of record: assume it restarts on the 19th with an empty keyspace and the
invoice must still come out exact.
"""

import redis.asyncio as aioredis

from meter.config import Settings


def create_client(settings: Settings) -> aioredis.Redis:
    return aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
    )


async def ping(client: aioredis.Redis) -> bool:
    return bool(await client.ping())
