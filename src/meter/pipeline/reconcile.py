"""Reconciliation: a number, and the query that produced it. ADR-0010 step 2.

"Looks about right" is not reconciliation. For one customer and one period this module
states four counts and the two deltas between them, and it carries the SQL that produced
each one so the number never has to be taken on trust:

    redis_counter      what the hot path counted   (INCR per billable request)
    events_billable    per-request truth           (usage_events, 90 days)
    rollup_billable    aggregated truth            (usage_rollups, forever)
    stream_outstanding what has not been drained   (XPENDING + group lag)

    counter_delta = redis_counter - events_billable
    rollup_delta  = events_billable - rollup_billable

**What each delta means.**

`counter_delta > 0` is normal and healthy: the hot path has counted requests that the drain
has not yet committed. It is the live figure running ahead of the exact one, and it is
exactly `stream_outstanding` when the pipeline is well. That equality is the strongest
statement this module makes, because it accounts for the whole difference rather than
tolerating it -- `counter_delta - stream_outstanding` is the *unexplained* part, and it is
the number that has to be zero.

`counter_delta < 0` is never normal. Postgres holds requests Redis never counted, which
means the counter was rebuilt from a stale read, or something wrote usage that did not go
through the hot path.

`rollup_delta > 0` means aggregation is behind, or some usage cannot be attributed to a
plan segment at all (a customer on no plan). Those two look identical in the delta and are
reported separately, because one resolves itself in seconds and the other never does.

ADR-0010 makes convergence -- all three zero -- the gate on the invoice run.
"""

import logging
from dataclasses import dataclass, field
from datetime import date

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from meter import billing_calendar as clock
from meter.pipeline import drain, keys
from meter.storage.repositories import periods, rollups

logger = logging.getLogger("meter.pipeline.reconcile")


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """One customer-period, proved. Every field is a number; `queries` is the evidence."""

    customer_id: str
    period_month: date
    period_id: str | None
    redis_counter: int
    events_billable: int
    rollup_billable: int
    unattributed: int
    stream_outstanding: int
    queries: dict[str, str] = field(default_factory=dict)

    @property
    def counter_delta(self) -> int:
        """Redis minus Postgres. Positive is usage in flight; negative is a real problem."""
        return self.redis_counter - self.events_billable

    @property
    def rollup_delta(self) -> int:
        """Per-request rows minus rollups. Aggregation lag, or unattributable usage."""
        return self.events_billable - self.rollup_billable

    @property
    def unexplained(self) -> int:
        """The part of the counter delta that undrained stream entries do NOT account for.

        This is the number reconciliation exists to produce. Zero means every request the
        hot path counted is either in Postgres or still visibly in the buffer -- nothing has
        gone missing and nothing has been counted twice.
        """
        return self.counter_delta - self.stream_outstanding

    @property
    def converged(self) -> bool:
        return (
            self.stream_outstanding == 0
            and self.counter_delta == 0
            and self.rollup_delta == 0
        )

    def explain(self) -> str:
        lines = [
            f"reconciliation for customer {self.customer_id}, period {self.period_month}",
            f"  redis counter      {self.redis_counter:>12,}",
            f"  usage_events       {self.events_billable:>12,}  (billable, per request)",
            f"  usage_rollups      {self.rollup_billable:>12,}  (billable, aggregated)",
            f"  stream outstanding {self.stream_outstanding:>12,}  (pending + undelivered)",
            f"  counter delta      {self.counter_delta:>+12,}  (redis - events)",
            f"  rollup delta       {self.rollup_delta:>+12,}  (events - rollups)",
            f"  unexplained        {self.unexplained:>+12,}  (counter delta - outstanding)",
        ]
        if self.unattributed:
            lines.append(
                f"  UNATTRIBUTED       {self.unattributed:>12,}  served requests with no "
                "plan assignment -- recorded but unbillable"
            )
        lines.append(f"  converged: {self.converged}")
        for name, sql in self.queries.items():
            lines.append(f"\n-- {name}{sql.rstrip()}")
        return "\n".join(lines)


async def reconcile(
    engine: AsyncEngine,
    redis: aioredis.Redis,
    customer_id: str,
    period_month: date,
    *,
    drainer: drain.Drain | None = None,
) -> Reconciliation:
    """Prove the Redis counter and the Postgres truth differ by a known delta."""
    period_start = clock.month_start(period_month)

    raw_counter = await redis.get(keys.usage_count(customer_id, period_month))
    counter = 0 if raw_counter is None else int(raw_counter)

    outstanding = 0 if drainer is None else await drainer.outstanding()

    async with engine.connect() as conn:
        period = await periods.get_period(conn, customer_id, period_month)
        events = await rollups.events_billable(conn, customer_id, period_start)
        rolled = (
            0
            if period is None
            else await rollups.rollups_billable(conn, customer_id, period.id)
        )
        unattributed = await rollups.unattributed_events(conn, customer_id, period_start)

    result = Reconciliation(
        customer_id=customer_id,
        period_month=period_month,
        period_id=None if period is None else period.id,
        redis_counter=counter,
        events_billable=events,
        rollup_billable=rolled,
        unattributed=unattributed,
        stream_outstanding=outstanding,
        queries={
            f"redis: GET {keys.usage_count(customer_id, period_month)}": "",
            "postgres, per-request truth:": rollups.EVENTS_BILLABLE_SQL,
            "postgres, rollup truth:": rollups.ROLLUPS_BILLABLE_SQL,
            "postgres, unattributable usage:": rollups.UNATTRIBUTED_SQL,
            "redis, undrained buffer:": (
                "\n  XPENDING <stream> <group>   -> delivered but not acked\n"
                "  XINFO GROUPS <stream> -> lag -> never delivered\n"
            ),
        },
    )

    if result.counter_delta < 0:
        logger.error(
            "customer %s %s: Postgres holds %d billable requests that Redis never counted "
            "(counter delta %+d). The counter is behind the truth, which is not a lag "
            "state -- it means the counter was rebuilt from a stale read or usage reached "
            "Postgres without passing the hot path.",
            customer_id,
            period_month,
            -result.counter_delta,
            result.counter_delta,
        )
    elif result.unexplained:
        logger.warning(
            "customer %s %s: %d requests of the counter delta are not explained by the "
            "%d entries still in the buffer",
            customer_id,
            period_month,
            result.unexplained,
            result.stream_outstanding,
        )
    return result
