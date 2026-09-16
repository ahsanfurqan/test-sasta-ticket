"""Spending-limit thresholds: rupees in, a request count out. ADR-0008 + ADR-0012.

The hot path must not run a ladder calculation per request, so it does not ask "what does
this customer owe?". It asks "is the counter past the threshold?" -- two integers. Turning
the rupee limit into that integer is the expensive direction, and it happens here.

**The ladder is never reimplemented in this module.** The inversion is
`meter.domain.rating.max_quantity_within`, the proration is
`meter.domain.proration.prorate`, and both are called with integers this module resolved
from storage. Two implementations of the same charge disagree eventually, and the
disagreement is found by a customer.

ADR-0012: the limit caps the WHOLE bill, monthly fee included -- and under a mid-month plan
change the fee component is the sum of the *prorated* segment fees, not a full monthly fee.
So the budget handed to the inversion is:

    limit
      - the prorated fees of every other segment
      - the usage charges already accrued in every other segment
      = the budget available inside the segment the customer is in right now

and `max_quantity_within` subtracts that segment's own prorated fee itself. The stored
threshold is then `usage already counted in other segments + what this segment affords`,
because the hot path's counter counts the whole period rather than one segment.

**Staleness is the failure mode to fear** (ADR-0008 calls a missed recomputation "the worst
kind, because everything looks fine"). Three defences: recompute on every input that moves
it, recompute anyway once the threshold is older than `threshold_max_age_seconds`, and
publish the age of the oldest threshold so the alert is on staleness rather than on
Postgres health (ADR-0018).
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from meter import billing_calendar as clock
from meter import segments
from meter.domain.proration import prorate
from meter.domain.rating import max_quantity_within, rate
from meter.money import Paisa, format_paisa
from meter.pipeline import keys
from meter.storage.repositories import periods

logger = logging.getLogger("meter.pipeline.thresholds")


@dataclass(frozen=True, slots=True)
class Threshold:
    """A computed threshold, with everything needed to explain or invalidate it."""

    customer_id: str
    period_id: str
    period_month: date
    limit_paisa: int
    threshold_requests: int
    price_list_version_id: str
    fee_component_paisa: int
    computed_at: datetime

    @property
    def redis_key(self) -> str:
        return keys.limit_threshold(self.customer_id, self.period_month)

    @property
    def unsatisfiable(self) -> bool:
        """The prorated fees alone exceed the limit, so no amount of refusing can honour it.

        This is a materially different fact from "you have spent your limit", and conflating
        the two is why a customer can be cut off with no idea why. The monthly fee is owed
        for the days they were on the plan whether or not we serve them -- so once the fee
        component passes the limit, refusing traffic cannot bring the bill back under it. We
        still refuse, because it stops the overage growing, but the customer must be told
        that their limit became impossible rather than that they used it up.

        ADR-0012 predicted the route in: an upgrade whose prorated fee consumes the whole
        limit, taken by a customer expecting more capacity.
        """
        return self.fee_component_paisa > self.limit_paisa

    #: What the hot path reads. A negative sentinel means unsatisfiable, so enforcement can
    #: tell the two states apart without a second Redis round trip -- the budget in ADR-0020
    #: is spent on three round trips already, and this fact rides along in one of them.
    UNSATISFIABLE = -1

    @property
    def redis_value(self) -> int:
        return self.UNSATISFIABLE if self.unsatisfiable else self.threshold_requests


class NoPlanForLimit(RuntimeError):
    """A spending limit on a customer who is on no plan in that period.

    There is no ladder to invert, so no threshold can be computed. Refusing to write a
    threshold is the safe answer: an absent threshold is visibly absent, whereas a
    guessed one enforces the wrong number while looking healthy.
    """


async def compute(
    conn: AsyncConnection,
    *,
    customer_id: str,
    period_id: str,
    period_month: date,
    period_start: datetime,
    period_end: datetime,
    limit_paisa: int,
    at: datetime | None = None,
) -> Threshold:
    """Invert one customer's rupee limit into a request count, net of prorated fees."""
    at = at or clock.now()
    period_segments = await segments.resolve(
        conn, customer_id, period_id, period_month, period_start, period_end
    )
    if not period_segments:
        raise NoPlanForLimit(
            f"customer {customer_id} has a spending limit for {period_month} but no plan "
            "assignment covering that period"
        )

    # The segment the customer is in right now. Past the end of the period (a close that
    # is running late), the last segment is the one that still applies.
    current_index = _current_segment_index(period_segments, at, period_start, period_end)
    current = period_segments[current_index]
    prorated_current = prorate(current.price_list, current.days, current.days_in_month)

    other_fees = 0
    other_usage_charges = 0
    usage_elsewhere = 0
    for index, segment in enumerate(period_segments):
        if index == current_index:
            continue
        prorated = prorate(segment.price_list, segment.days, segment.days_in_month)
        charge = rate(segment.quantity, prorated)
        other_fees += prorated.monthly_fee_paisa
        # The usage half only: the fee half is counted once, above.
        other_usage_charges += charge.total_paisa - prorated.monthly_fee_paisa
        usage_elsewhere += segment.quantity

    budget = limit_paisa - other_fees - other_usage_charges
    if budget < 0:
        # Other segments alone have already consumed the limit. Nothing more is affordable
        # here, and the threshold is whatever has already been counted.
        allowed_here = 0
    else:
        allowed_here = max_quantity_within(int(budget), prorated_current)

    threshold = usage_elsewhere + allowed_here
    fee_component = other_fees + prorated_current.monthly_fee_paisa

    if fee_component > limit_paisa:
        # ADR-0012 requires an unsatisfiable limit to be rejected when it is SET. Reaching
        # here means it became unsatisfiable AFTERWARDS -- almost always a plan change whose
        # prorated fee swallowed the limit. The customer is about to be refused for a reason
        # they did not cause and cannot fix by sending less traffic.
        logger.error(
            "customer %s has a limit of %s for %s, which is below the prorated fee "
            "component of %s: they will be refused from their first request",
            customer_id,
            format_paisa(Paisa(limit_paisa)),
            period_month,
            format_paisa(Paisa(fee_component)),
        )

    return Threshold(
        customer_id=customer_id,
        period_id=period_id,
        period_month=period_month,
        limit_paisa=limit_paisa,
        threshold_requests=threshold,
        price_list_version_id=current.price_list_version_id,
        fee_component_paisa=fee_component,
        computed_at=at,
    )


def _current_segment_index(
    period_segments: list[segments.PeriodSegment],
    at: datetime,
    period_start: datetime,
    period_end: datetime,
) -> int:
    """Which segment `at` falls in. Clamped to the period, and skipping zero-day segments.

    A zero-day segment has no allowance and no bands (ADR-0017), so inverting a limit
    against one would return nothing affordable at all -- which would refuse a customer
    whose real, non-zero-day plan has plenty of headroom.
    """
    moment = min(max(at, period_start), period_end)
    days_elapsed = clock.whole_days_between(period_start, moment)
    cumulative = 0
    chosen = 0
    for index, segment in enumerate(period_segments):
        if segment.days == 0:
            continue
        chosen = index
        cumulative += segment.days
        if days_elapsed < cumulative:
            return index
    return chosen


# ---------------------------------------------------------------------------------------
# Publishing and the recomputation sweep
# ---------------------------------------------------------------------------------------


async def publish(
    conn: AsyncConnection, redis: aioredis.Redis, threshold: Threshold
) -> None:
    """Write the threshold where the hot path reads it, and record what it came from.

    Redis first, Postgres second, on purpose: Redis is what enforcement actually reads, and
    the Postgres row is the audit trail plus the staleness input. If the process dies
    between them the sweep recomputes, because the recorded `threshold_computed_at` is
    still old -- whereas the reverse order would leave enforcement running on a stale
    number that the database claims is fresh.
    """
    # The Redis value carries the sentinel; the Postgres column is CHECK (>= 0) and stores
    # the plain count. The audit trail records what was computed; Redis records what to
    # enforce, and those are allowed to differ in exactly this one way.
    await redis.set(threshold.redis_key, threshold.redis_value)
    await conn.execute(
        text(
            """
            UPDATE spending_limits
               SET threshold_requests = :threshold,
                   -- now(), not a Python instant: the touch trigger stamps updated_at
                   -- with now() in this same statement, so the two are exactly equal and
                   -- "the limit changed since we computed" does not fire on our own write.
                   threshold_computed_at = now(),
                   threshold_price_list_version_id = :version_id
             WHERE customer_id = :customer_id AND billing_period_id = :period_id
            """
        ),
        {
            "threshold": threshold.threshold_requests,
            "version_id": threshold.price_list_version_id,
            "customer_id": threshold.customer_id,
            "period_id": threshold.period_id,
        },
    )


SWEEP_SQL = """
SELECT sl.customer_id,
       sl.billing_period_id,
       sl.limit_paisa,
       sl.threshold_requests,
       sl.threshold_computed_at,
       sl.threshold_price_list_version_id,
       sl.updated_at,
       bp.period_month,
       bp.period_start,
       bp.period_end,
       (SELECT max(pa.created_at) FROM plan_assignments pa
         WHERE pa.customer_id = sl.customer_id) AS plan_changed_at
  FROM spending_limits sl
  JOIN billing_periods bp ON bp.id = sl.billing_period_id
 WHERE bp.status <> 'invoiced'
"""


def _needs_recompute(row, max_age_seconds: int, now: datetime) -> str | None:
    """Why this threshold must be recomputed, or None. The reason is logged, because a
    threshold that changed for no stated reason is indistinguishable from a bug."""
    if row.threshold_computed_at is None:
        return "never computed"
    computed_at = row.threshold_computed_at
    if row.updated_at is not None and computed_at < row.updated_at:
        return "the limit changed"
    if row.plan_changed_at is not None and computed_at < row.plan_changed_at:
        return "the plan or price list version changed"
    age = (now - computed_at).total_seconds()
    if age > max_age_seconds:
        return f"it is {age:.0f}s old"
    return None


@dataclass(slots=True)
class SweepResult:
    examined: int = 0
    recomputed: int = 0
    skipped_no_plan: int = 0
    oldest_age_seconds: float = 0.0
    # Customers whose limit is already spent as of this recomputation -- ADR-0012's
    # upgrade-exhausts-the-limit case, carried out rather than only logged.
    exhausted: list[str] = field(default_factory=list)


async def sweep(
    engine: AsyncEngine, redis: aioredis.Redis, *, max_age_seconds: int, at=None
) -> SweepResult:
    """Recompute every threshold that an input has moved, or that has simply gone stale."""
    now = at or clock.now()
    result = SweepResult()

    async with engine.begin() as conn:
        rows = (await conn.execute(text(SWEEP_SQL))).all()
        result.examined = len(rows)

        for row in rows:
            reason = _needs_recompute(row, max_age_seconds, now)
            if reason is None:
                result.oldest_age_seconds = max(
                    result.oldest_age_seconds,
                    (now - row.threshold_computed_at).total_seconds(),
                )
                continue
            try:
                threshold = await compute(
                    conn,
                    customer_id=row.customer_id,
                    period_id=row.billing_period_id,
                    period_month=row.period_month,
                    period_start=row.period_start,
                    period_end=row.period_end,
                    limit_paisa=int(row.limit_paisa),
                    at=now,
                )
            except NoPlanForLimit as exc:
                result.skipped_no_plan += 1
                logger.error("%s", exc)
                continue

            await publish(conn, redis, threshold)
            result.recomputed += 1
            logger.info(
                "threshold for %s %s = %d requests (%s); recomputed because %s",
                row.customer_id,
                row.period_month,
                threshold.threshold_requests,
                format_paisa(Paisa(int(row.limit_paisa))),
                reason,
            )
            if await _limit_already_spent(redis, threshold):
                result.exhausted.append(row.customer_id)

    await redis.set(keys.THRESHOLD_OLDEST_AGE_SECONDS, f"{result.oldest_age_seconds:.1f}")
    return result


async def _limit_already_spent(redis: aioredis.Redis, threshold: Threshold) -> bool:
    """ADR-0012's sharpest edge, surfaced rather than discovered.

    A mid-month upgrade consumes more of the same cap through its larger prorated fee, so
    a customer who took an action expecting MORE capacity can be refused from that instant.
    The ADR says this needs surfacing at the moment of upgrade -- this is the moment the
    pipeline learns of it.
    """
    counted = await redis.get(keys.usage_count(threshold.customer_id, threshold.period_month))
    if counted is None or int(counted) < threshold.threshold_requests:
        return False
    logger.error(
        "customer %s is at %s requests against a NEW threshold of %s for %s: their "
        "spending limit is exhausted as of this recomputation, and the most likely cause "
        "is a plan change whose larger prorated fee consumed the remaining headroom "
        "(ADR-0012)",
        threshold.customer_id,
        counted,
        threshold.threshold_requests,
        threshold.period_month,
    )
    return True


async def recompute_for(
    engine: AsyncEngine,
    redis: aioredis.Redis,
    customer_id: str,
    period_month: date,
) -> Threshold | None:
    """Recompute one customer's threshold now. The manual trigger, and the test hook."""
    async with engine.begin() as conn:
        period = await periods.get_period(conn, customer_id, period_month)
        if period is None:
            return None
        row = (
            await conn.execute(
                text(
                    "SELECT limit_paisa FROM spending_limits "
                    " WHERE customer_id = :customer_id AND billing_period_id = :period_id"
                ),
                {"customer_id": customer_id, "period_id": period.id},
            )
        ).one_or_none()
        if row is None:
            return None
        threshold = await compute(
            conn,
            customer_id=customer_id,
            period_id=period.id,
            period_month=period_month,
            period_start=period.period_start,
            period_end=period.period_end,
            limit_paisa=int(row.limit_paisa),
        )
        await publish(conn, redis, threshold)
        return threshold
