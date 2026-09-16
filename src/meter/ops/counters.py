"""Whether the Redis counters may be trusted. Read-side only. ADR-0011.

ADR-0011's sharpest edge is that a Redis which comes back healthy but EMPTY is worse than
a Redis that is down: every counter reads zero, every spending limit looks unreached, and
the system serves confidently past limits it can no longer see. A zero counter and an
absent counter are indistinguishable to `GET`, so the thing that distinguishes them cannot
be a counter -- it is a separate marker key, written last by a rebuild that has just read
the truth out of Postgres.

**The rebuild itself is `pipeline`'s** (`meter.pipeline.counters`), and deliberately not
duplicated here: two processes writing the same marker is how it comes to mean nothing.
`pipeline` re-checks it every worker tick, so a Redis that restarts at 03:00 does not wait
for a deploy. The hot path only ever reads it, and refuses everything while it is absent.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from meter.storage.repositories import usage as usage_repo


async def marker(redis: aioredis.Redis) -> str | None:
    """The raw marker value, or None if the keyspace has not been rebuilt."""
    return await redis.get(usage_repo.COUNTERS_AUTHORITATIVE)


async def is_authoritative(redis: aioredis.Redis) -> bool:
    """Whether the hot path may serve. Absent means "not rebuilt", never "no usage yet"."""
    return await marker(redis) is not None
