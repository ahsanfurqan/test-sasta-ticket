"""Rebuilding Redis counters from Postgres, and timing it. ADR-0011.

ADR-0011 chose to fail closed when Redis is unavailable, and named one thing as the sharpest
edge of that choice:

> **Empty-keyspace restart is not distinguishable from "no usage yet" without care.** A
> Redis that comes back healthy but empty will happily serve requests against a zero
> counter, which is worse than being down.

So the hot path does not serve on "Redis responds". It serves on
`meter:counters:authoritative` being set -- and that key is written by this module, last,
after every counter for the open period has been rebuilt from the per-request rows. A Redis
that restarts empty has no such key, which is exactly the state that must not serve. The
flag is never given a TTL and never written by anything else.

> **Rebuild time from Postgres is the number that matters and is currently unknown.**

`rebuild()` returns it and publishes it to `meter:counters:rebuild_seconds`, because a
number that is only in a log line is a number nobody has. Measured values are in the report
for this session; the shape is one grouped scan of one partition plus one pipelined MSET,
so it is linear in customers rather than in requests.

Ordering, deliberately: counters, then thresholds, then the flag. Serving with a counter but
no threshold would enforce nothing; serving with neither would count nothing. Both are
states the flag exists to prevent.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meter import billing_calendar as clock
from meter.pipeline import keys, thresholds
from meter.storage.repositories import rollups

logger = logging.getLogger("meter.pipeline.counters")

# How many SET commands go in one pipeline round trip during a rebuild.
_BATCH = 1_000


@dataclass(slots=True)
class RebuildResult:
    """ADR-0011's missing number, plus what it was measured over."""

    period_month: date
    customers: int
    requests: int
    thresholds_written: int
    seconds: float

    @property
    def customers_per_second(self) -> float:
        return self.customers / self.seconds if self.seconds else float("inf")

    def explain(self) -> str:
        return (
            f"counter rebuild for {self.period_month}: {self.customers:,} customers, "
            f"{self.requests:,} billable requests, {self.thresholds_written:,} thresholds, "
            f"in {self.seconds * 1000:.0f}ms "
            f"({self.customers_per_second:,.0f} customers/s). "
            "This is the duration of the fail-closed outage after a Redis restart."
        )


async def is_authoritative(redis: aioredis.Redis) -> bool:
    """Whether the hot path may serve. Absent means "rebuilt? no" -- never "no usage yet"."""
    return await redis.get(keys.COUNTERS_AUTHORITATIVE) == "1"


async def rebuild(
    engine: AsyncEngine,
    redis: aioredis.Redis,
    period_month: date | None = None,
    *,
    mark_authoritative: bool = True,
) -> RebuildResult:
    """Rebuild every counter for a period from `usage_events`, then mark them authoritative.

    Safe to run at any time: it SETs absolute values read from the system of record rather
    than incrementing, so a rebuild that runs twice produces the same keyspace. It is NOT
    safe to run while the hot path is serving -- which is the whole point of the flag: the
    hot path is refusing traffic for the duration, so no INCR can race the SET.
    """
    month = period_month or clock.period_month(clock.now())
    period_start = clock.month_start(month)

    started = time.perf_counter()

    async with engine.connect() as conn:
        counts = await rollups.billable_counts_by_customer(conn, period_start)

    pipe = redis.pipeline(transaction=False)
    queued = 0
    for customer_id, count in counts.items():
        pipe.set(keys.usage_count(customer_id, month), count)
        queued += 1
        if queued % _BATCH == 0:
            await pipe.execute()
            pipe = redis.pipeline(transaction=False)
    if queued % _BATCH:
        await pipe.execute()

    thresholds_written = await _rebuild_thresholds(engine, redis, month)

    if mark_authoritative:
        # Last. Everything the hot path needs to both count and enforce is in place.
        await redis.set(keys.COUNTERS_AUTHORITATIVE, "1")
        await redis.set(keys.COUNTERS_REBUILT_AT, f"{clock.now().timestamp():.3f}")

    elapsed = time.perf_counter() - started
    await redis.set(keys.COUNTERS_REBUILD_SECONDS, f"{elapsed:.6f}")

    result = RebuildResult(
        period_month=month,
        customers=len(counts),
        requests=sum(counts.values()),
        thresholds_written=thresholds_written,
        seconds=elapsed,
    )
    logger.warning("%s", result.explain())
    return result


_LIMITS_SQL = """
SELECT sl.customer_id, sl.billing_period_id, sl.limit_paisa,
       bp.period_month, bp.period_start, bp.period_end
  FROM spending_limits sl
  JOIN billing_periods bp ON bp.id = sl.billing_period_id
 WHERE bp.period_month = :period_month
"""


async def _rebuild_thresholds(
    engine: AsyncEngine, redis: aioredis.Redis, period_month: date
) -> int:
    """Thresholds are lost with the counters and must come back with them.

    ADR-0008 is explicit: "If Redis restarts, both the counter and the threshold are gone.
    Enforcement silently stops until both are rebuilt." Rebuilding one without the other
    gives a system that counts correctly and enforces nothing.
    """
    written = 0
    async with engine.begin() as conn:
        rows = (
            await conn.execute(text(_LIMITS_SQL), {"period_month": period_month})
        ).all()
        for row in rows:
            try:
                threshold = await thresholds.compute(
                    conn,
                    customer_id=row.customer_id,
                    period_id=row.billing_period_id,
                    period_month=row.period_month,
                    period_start=row.period_start,
                    period_end=row.period_end,
                    limit_paisa=int(row.limit_paisa),
                )
            except thresholds.NoPlanForLimit as exc:
                logger.error("%s", exc)
                continue
            await thresholds.publish(conn, redis, threshold)
            written += 1
    return written


async def ensure_authoritative(
    engine: AsyncEngine, redis: aioredis.Redis
) -> RebuildResult | None:
    """The watchdog: if the flag is gone, Redis restarted -- rebuild before it can serve.

    Run on every worker tick, not just at startup. A Redis that restarts at 03:00 on the
    19th does not wait for a worker deploy, and until this runs the hot path is returning
    503 to everybody (which is ADR-0011 working, and also an outage being measured).
    """
    if await is_authoritative(redis):
        return None
    logger.error(
        "counters are not marked authoritative: Redis has restarted empty or has never "
        "been primed. The hot path is refusing traffic (ADR-0011) until this rebuild "
        "finishes -- the duration below IS the outage."
    )
    return await rebuild(engine, redis)
