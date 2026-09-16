"""Provisioning: put a customer on a plan, issue a key, set a spending limit.

**This is an admin/demo surface, not a product.** In a real deployment these actions come
from a signup flow, a billing console and a plan-change job, each with its own
authorisation, audit trail and approval rules. They exist here so the system can be
demonstrated end to end -- create a customer, call the API, watch the counter move, set a
limit, cross it -- without hand-writing SQL into psql.

Three notes on what lives here and why:

* The SQL sits in `meter.api` rather than in `meter.storage.repositories` because those are
  data-model's files and this is a temporary surface. When provisioning becomes real it
  should move down a layer, and this module should not survive the move.
* `meter.domain` IS imported here, on purpose: setting a limit is exactly the expensive
  rupees-to-requests direction ADR-0008 says to compute away from the request path. It runs
  once when the limit is set, not once per request, and nothing in `meter.api.metering`
  imports it.
* Threshold recomputation on every input that moves it -- a plan change, a new price list
  version, a mid-month upgrade -- is `pipeline`'s (ADR-0008). What is here is the
  computation at the moment the limit is set, so the limit takes effect immediately instead
  of at the next sweep.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meter.domain import catalogue, proration, rating
from meter.domain.plans import Band, PriceList
from meter.storage.repositories import keys as keys_repo
from meter.storage.repositories import usage as usage_repo

logger = logging.getLogger(__name__)

PLANS: dict[str, PriceList] = {
    price_list.name: price_list for price_list in catalogue.LAUNCH_PRICE_LISTS
}


class ProvisioningError(Exception):
    """A provisioning request that cannot be satisfied, with a reason for the caller."""


# ---------------------------------------------------------------------------------------
# Period arithmetic (ADR-0009: boundaries in Asia/Karachi, stored as UTC instants)
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeriodBounds:
    label: str
    month: date
    start: datetime
    end: datetime
    days_in_month: int


def period_bounds(period: usage_repo.Period) -> PeriodBounds:
    local_start = period.start.astimezone(usage_repo.BILLING_TIMEZONE)
    year, month = local_start.year, local_start.month
    days_in_month = calendar.monthrange(year, month)[1]
    next_month_local = local_start.replace(
        year=year + (month == 12), month=1 if month == 12 else month + 1
    )
    return PeriodBounds(
        label=period.label,
        month=date(year, month, 1),
        start=period.start,
        end=next_month_local.astimezone(UTC),
        days_in_month=days_in_month,
    )


def _remaining_days(bounds: PeriodBounds, starts_at: datetime) -> int:
    """Whole days from `starts_at` to the end of the period, the change day included.

    ADR-0017: a mid-month signup prorates exactly like a plan change, and ADR-0006 gives
    the change day to the new plan.
    """
    local = max(starts_at, bounds.start).astimezone(usage_repo.BILLING_TIMEZONE)
    first_day = local.date()
    last_day = (bounds.end.astimezone(usage_repo.BILLING_TIMEZONE) - timedelta(seconds=1)).date()
    return (last_day - first_day).days + 1


# ---------------------------------------------------------------------------------------
# Price lists (ADR-0005). Seeded from meter.domain.catalogue, which is seed data only.
# ---------------------------------------------------------------------------------------

_PRICE_LIST_ID = text("SELECT id::text AS id FROM price_lists WHERE name = :name")
_INSERT_PRICE_LIST = text(
    "INSERT INTO price_lists (name) VALUES (:name) "
    "ON CONFLICT (name) DO NOTHING RETURNING id::text AS id"
)
_VERSION_ID = text(
    "SELECT id::text AS id FROM price_list_versions "
    "WHERE price_list_id = :price_list_id AND version = :version"
)
_INSERT_VERSION = text(
    """
    INSERT INTO price_list_versions
        (price_list_id, version, name, monthly_fee_paisa, included_quantity)
    VALUES (:price_list_id, :version, :name, :monthly_fee_paisa, :included_quantity)
    RETURNING id::text AS id
    """
)
_INSERT_BAND = text(
    """
    INSERT INTO price_bands (price_list_version_id, band_index, up_to, unit_price_paisa)
    VALUES (:version_id, :band_index, :up_to, :unit_price_paisa)
    """
)
_PUBLISH_VERSION = text(
    "UPDATE price_list_versions SET published_at = now() "
    "WHERE id = :id AND published_at IS NULL"
)
_LOAD_VERSION = text(
    """
    SELECT name, version, monthly_fee_paisa, included_quantity
      FROM price_list_versions WHERE id = :id
    """
)
_LOAD_BANDS = text(
    """
    SELECT up_to, unit_price_paisa FROM price_bands
     WHERE price_list_version_id = :id ORDER BY band_index
    """
)


async def ensure_price_list_version(
    session_factory: async_sessionmaker[AsyncSession], price_list: PriceList
) -> str:
    """Seed one launch price list into the database and publish it. Idempotent.

    Insert draft, add bands, publish -- in that order, because the database validates the
    whole ladder at the moment of publication and freezes it immediately afterwards.
    """
    async with session_factory() as session:
        list_id = (
            await session.execute(_INSERT_PRICE_LIST, {"name": price_list.name})
        ).scalar_one_or_none()
        if list_id is None:
            list_id = (
                await session.execute(_PRICE_LIST_ID, {"name": price_list.name})
            ).scalar_one()

        version_id = (
            await session.execute(
                _VERSION_ID, {"price_list_id": list_id, "version": price_list.version}
            )
        ).scalar_one_or_none()
        if version_id is not None:
            await session.commit()
            return version_id

        version_id = (
            await session.execute(
                _INSERT_VERSION,
                {
                    "price_list_id": list_id,
                    "version": price_list.version,
                    "name": price_list.name,
                    "monthly_fee_paisa": price_list.monthly_fee_paisa,
                    "included_quantity": price_list.included_quantity,
                },
            )
        ).scalar_one()
        for index, band in enumerate(price_list.bands):
            await session.execute(
                _INSERT_BAND,
                {
                    "version_id": version_id,
                    "band_index": index,
                    "up_to": band.up_to,
                    "unit_price_paisa": band.unit_price_paisa,
                },
            )
        await session.execute(_PUBLISH_VERSION, {"id": version_id})
        await session.commit()
    return version_id


async def load_price_list(
    session_factory: async_sessionmaker[AsyncSession], version_id: str
) -> PriceList:
    """Rebuild the domain value object from the database, which is the source of truth."""
    async with session_factory() as session:
        row = (await session.execute(_LOAD_VERSION, {"id": version_id})).one()
        bands = (await session.execute(_LOAD_BANDS, {"id": version_id})).all()
    return PriceList(
        name=row.name,
        version=row.version,
        monthly_fee_paisa=row.monthly_fee_paisa,
        included_quantity=row.included_quantity,
        bands=tuple(
            Band(up_to=band.up_to, unit_price_paisa=band.unit_price_paisa) for band in bands
        ),
    )


# ---------------------------------------------------------------------------------------
# Customers, plans and billing periods
# ---------------------------------------------------------------------------------------

_INSERT_ASSIGNMENT = text(
    """
    INSERT INTO plan_assignments (customer_id, price_list_version_id, effective)
    VALUES (:customer_id, :version_id, tstzrange(:starts_at, NULL, '[)'))
    RETURNING id::text AS id
    """
)
_CLOSE_ASSIGNMENT = text(
    """
    UPDATE plan_assignments
       SET effective = tstzrange(lower(effective), :at, '[)')
     WHERE customer_id = :customer_id AND upper_inf(effective)
    RETURNING id::text AS id
    """
)
_CURRENT_ASSIGNMENT = text(
    """
    SELECT id::text AS id,
           price_list_version_id::text AS version_id,
           lower(effective) AS starts_at
      FROM plan_assignments
     WHERE customer_id = :customer_id AND effective @> CAST(:at AS timestamptz)
    """
)
_INSERT_PERIOD = text(
    """
    INSERT INTO billing_periods (customer_id, period_month, period_start, period_end)
    VALUES (:customer_id, :period_month, :period_start, :period_end)
    ON CONFLICT (customer_id, period_month) DO NOTHING
    RETURNING id::text AS id
    """
)
_SELECT_PERIOD = text(
    "SELECT id::text AS id FROM billing_periods "
    "WHERE customer_id = :customer_id AND period_month = :period_month"
)
_UPSERT_LIMIT = text(
    """
    INSERT INTO spending_limits
        (customer_id, billing_period_id, limit_paisa, threshold_requests,
         threshold_computed_at, threshold_price_list_version_id)
    VALUES (:customer_id, :billing_period_id, :limit_paisa, :threshold_requests,
            CASE WHEN CAST(:threshold_requests AS bigint) IS NULL THEN NULL ELSE now() END,
            :version_id)
    ON CONFLICT (customer_id, billing_period_id) DO UPDATE
       SET limit_paisa = EXCLUDED.limit_paisa,
           threshold_requests = EXCLUDED.threshold_requests,
           -- NULL when we deferred, so `thresholds._needs_recompute` reports "never
           -- computed" and the sweep fills it in. Stamping now() here would mark an absent
           -- threshold as freshly computed, and the sweep would skip it forever.
           threshold_computed_at = EXCLUDED.threshold_computed_at,
           threshold_price_list_version_id = EXCLUDED.threshold_price_list_version_id,
           updated_at = now()
    RETURNING id::text AS id
    """
)


async def assign_plan(
    session_factory: async_sessionmaker[AsyncSession],
    customer_id: str,
    version_id: str,
    starts_at: datetime,
) -> str:
    async with session_factory() as session:
        assignment_id = (
            await session.execute(
                _INSERT_ASSIGNMENT,
                {"customer_id": customer_id, "version_id": version_id, "starts_at": starts_at},
            )
        ).scalar_one()
        await session.commit()
    return assignment_id


async def change_plan(
    session_factory: async_sessionmaker[AsyncSession],
    customer_id: str,
    version_id: str,
    at: datetime,
) -> tuple[str | None, str]:
    """Move a customer onto a different price list version from `at`.

    Closing the open assignment and opening the new one happen in ONE transaction, in that
    order. Plan assignments carry an exclusion constraint over their time range (a customer
    cannot be on two plans at once), so inserting first would be rejected by the database --
    which is the constraint doing its job, and the reason this is not two calls.

    The change takes effect from `at`, and ADR-0006 gives the change DAY to the new plan.
    """
    async with session_factory() as session:
        closed = (
            await session.execute(
                _CLOSE_ASSIGNMENT, {"customer_id": customer_id, "at": at}
            )
        ).scalar_one_or_none()
        opened = (
            await session.execute(
                _INSERT_ASSIGNMENT,
                {"customer_id": customer_id, "version_id": version_id, "starts_at": at},
            )
        ).scalar_one()
        await session.commit()
    return closed, opened


_ASSIGNMENTS_IN_PERIOD = text(
    """
    SELECT count(*) AS n
    FROM plan_assignments
    WHERE customer_id = :customer_id
      AND effective && tstzrange(:period_start, :period_end, '[)')
    """
)


async def assignments_in_period(
    session_factory: async_sessionmaker[AsyncSession],
    customer_id: str,
    bounds: PeriodBounds,
) -> int:
    """How many plan assignments touch this period -- i.e. how many segments it has."""
    async with session_factory() as session:
        return (
            await session.execute(
                _ASSIGNMENTS_IN_PERIOD,
                {
                    "customer_id": customer_id,
                    "period_start": bounds.start,
                    "period_end": bounds.end,
                },
            )
        ).scalar_one()


async def current_assignment(
    session_factory: async_sessionmaker[AsyncSession], customer_id: str, at: datetime
) -> tuple[str, datetime] | None:
    async with session_factory() as session:
        row = (
            await session.execute(_CURRENT_ASSIGNMENT, {"customer_id": customer_id, "at": at})
        ).first()
    return (row.version_id, row.starts_at) if row else None


async def ensure_billing_period(
    session_factory: async_sessionmaker[AsyncSession],
    customer_id: str,
    bounds: PeriodBounds,
) -> str:
    async with session_factory() as session:
        period_id = (
            await session.execute(
                _INSERT_PERIOD,
                {
                    "customer_id": customer_id,
                    "period_month": bounds.month,
                    "period_start": bounds.start,
                    "period_end": bounds.end,
                },
            )
        ).scalar_one_or_none()
        if period_id is None:
            period_id = (
                await session.execute(
                    _SELECT_PERIOD,
                    {"customer_id": customer_id, "period_month": bounds.month},
                )
            ).scalar_one()
        await session.commit()
    return period_id


async def provision_customer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    name: str,
    plan: str,
    secret: str | None = None,
    prefix: str | None = None,
    label: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Create a customer on a plan, open their billing period, and issue their first key."""
    if plan not in PLANS:
        raise ProvisioningError(f"unknown plan {plan!r}; one of {sorted(PLANS)}")

    now = now or datetime.now(UTC)
    bounds = period_bounds(usage_repo.period_for(now))

    version_id = await ensure_price_list_version(session_factory, PLANS[plan])
    customer_id = await keys_repo.create_customer(session_factory, name)
    assignment_id = await assign_plan(session_factory, customer_id, version_id, now)
    period_id = await ensure_billing_period(session_factory, customer_id, bounds)
    issued = await keys_repo.issue_key(
        session_factory, customer_id, label=label, secret=secret, prefix=prefix
    )

    return {
        "customer_id": customer_id,
        "name": name,
        "plan": plan,
        "price_list_version_id": version_id,
        "plan_assignment_id": assignment_id,
        "billing_period_id": period_id,
        "billing_period": bounds.label,
        "api_key": issued.secret,
        "api_key_id": issued.key_id,
        "api_key_prefix": issued.prefix,
    }


# ---------------------------------------------------------------------------------------
# Spending limits (ADR-0008 inversion, ADR-0012 scope)
# ---------------------------------------------------------------------------------------


async def set_spending_limit(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    customer_id: str,
    limit_paisa: int,
    now: datetime | None = None,
) -> dict:
    """Invert a rupee limit into the request count the hot path compares against.

    ADR-0012: the limit caps the TOTAL bill, monthly fee included, so the fee is spent
    before a single request is. A limit that cannot cover the (prorated) fee is rejected
    here, with the fee named, rather than silently becoming a threshold of zero.

    ADR-0008: the inversion is exact, not a search -- `rating.max_quantity_within` walks the
    ladder once. Computed here, written to Redis, and never computed again until something
    moves it.

    Single-segment only: a customer who changed plan mid-period needs the threshold computed
    across segments, which is `pipeline`'s recomputation path and is not implemented here.
    """
    if limit_paisa <= 0:
        raise ProvisioningError("a spending limit must be a positive number of paisa")

    now = now or datetime.now(UTC)
    period = usage_repo.period_for(now)
    bounds = period_bounds(period)

    assignment = await current_assignment(session_factory, customer_id, now)
    if assignment is None:
        raise ProvisioningError(
            "customer is not on a plan right now, so there is no ladder to invert"
        )
    version_id, starts_at = assignment

    price_list = await load_price_list(session_factory, version_id)
    days = _remaining_days(bounds, starts_at)
    applicable = proration.prorate(price_list, days, bounds.days_in_month)

    if limit_paisa < applicable.monthly_fee_paisa:
        raise ProvisioningError(
            f"a limit of {limit_paisa} paisa is below the {applicable.monthly_fee_paisa} "
            f"paisa monthly fee for this period ({days} of {bounds.days_in_month} days on "
            f"{price_list.label}); the bill can never come in under it (ADR-0012)"
        )

    # ADR-0008's inversion is authoritative only across ALL of a period's segments: the
    # earlier segments' prorated fees and usage charges have already spent part of the
    # limit. This endpoint sees one segment, so for a single-segment period it computes the
    # identical answer to `pipeline.thresholds.compute` (there, other_fees and
    # other_usage_charges are both zero and the expression reduces to exactly this call) --
    # and for a multi-segment period it MUST NOT guess. Guessing here would be generous in
    # the customer's favour and wrong in ours, and it would be a second, quieter
    # implementation of the same money question. So it defers: the row is written with no
    # threshold, and the sweep, which can see every segment, fills it in.
    segment_count = await assignments_in_period(session_factory, customer_id, bounds)
    defer_to_sweep = segment_count > 1
    threshold = None if defer_to_sweep else rating.max_quantity_within(limit_paisa, applicable)

    period_id = await ensure_billing_period(session_factory, customer_id, bounds)

    # Redis first, Postgres second -- the same order `pipeline.thresholds.publish` uses, and
    # for the same reason. Redis is what enforcement actually reads; the Postgres row is the
    # audit trail and the staleness input. Dying between them leaves a threshold that is
    # live but not yet recorded, which the sweep will simply recompute. The reverse order
    # would leave a row claiming a fresh threshold that nothing is enforcing -- ADR-0008's
    # "silent enforcement failure", which is the failure this system likes least.
    if threshold is None:
        # Clear any stale threshold rather than leaving an old number enforcing. Until the
        # sweep lands, this customer is unlimited -- which is the honest consequence of not
        # guessing, and it is bounded by the sweep interval.
        await redis.delete(usage_repo.threshold_key(customer_id, period.label))
    else:
        await redis.set(usage_repo.threshold_key(customer_id, period.label), threshold)

    async with session_factory() as session:
        limit_id = (
            await session.execute(
                _UPSERT_LIMIT,
                {
                    "customer_id": customer_id,
                    "billing_period_id": period_id,
                    "limit_paisa": limit_paisa,
                    "threshold_requests": threshold,
                    "version_id": version_id,
                },
            )
        ).scalar_one()
        await session.commit()

    return {
        "spending_limit_id": limit_id,
        "customer_id": customer_id,
        "billing_period": period.label,
        "limit_paisa": limit_paisa,
        "request_threshold": threshold,
        "threshold_pending": defer_to_sweep,
        "threshold_note": (
            f"this period has {segment_count} plan segments, so the threshold is computed "
            "by the pipeline across all of them rather than guessed from the current one"
        )
        if defer_to_sweep
        else None,
        "prorated_fee_paisa": applicable.monthly_fee_paisa,
        "prorated_included_requests": applicable.included_quantity,
        "days_in_period": days,
        "days_in_month": bounds.days_in_month,
        "price_list": price_list.label,
    }
