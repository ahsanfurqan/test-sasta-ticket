"""Billing boundaries, resolved in Asia/Karachi (ADR-0009).

Storage is UTC; *boundaries* are evaluated in the customer's billing timezone, and the
resolved instants are what everything downstream compares. This module is the only place
in `pipeline` that knows the timezone exists -- `meter.domain` never sees a clock at all,
which is why proration takes `days` and `days_in_month` as plain integers.

Pure functions over datetimes. `now()` is the one exception and is isolated here so every
other module takes the instant as an argument and is therefore testable.
"""

from calendar import monthrange
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# ADR-0009. Stored per customer on `customers.billing_timezone` against the day this is
# not the only value; until then, resolving it per customer would be ceremony.
BILLING_TZ = ZoneInfo("Asia/Karachi")


def now() -> datetime:
    return datetime.now(UTC)


def local_date(instant: datetime) -> date:
    """The LOCAL calendar date an instant falls on. Five hours from the UTC date, and the
    difference is a whole day's usage at the edges -- which is why rollups store this."""
    return instant.astimezone(BILLING_TZ).date()


def period_month(instant: datetime) -> date:
    """The local first-of-month that names the billing period an instant falls in."""
    day = local_date(instant)
    return date(day.year, day.month, 1)


def month_start(period_month_: date) -> datetime:
    """The UTC instant at which a local month begins."""
    return datetime(
        period_month_.year, period_month_.month, 1, tzinfo=BILLING_TZ
    ).astimezone(UTC)


def month_end(period_month_: date) -> datetime:
    """The UTC instant at which a local month ends -- exclusive, half-open with the next."""
    return month_start(next_month(period_month_))


def next_month(period_month_: date) -> date:
    if period_month_.month == 12:
        return date(period_month_.year + 1, 1, 1)
    return date(period_month_.year, period_month_.month + 1, 1)


def previous_month(period_month_: date) -> date:
    if period_month_.month == 1:
        return date(period_month_.year - 1, 12, 1)
    return date(period_month_.year, period_month_.month - 1, 1)


def days_in_month(period_month_: date) -> int:
    """Actual calendar days, 28-31, never a notional 30 (ADR-0006)."""
    return monthrange(period_month_.year, period_month_.month)[1]


def period_start_of(instant: datetime) -> datetime:
    """The partition key for a usage event: the resolved start of its billing period.

    Deterministic from `occurred_at` alone, which matters more than it looks: the usage
    idempotency key is unique per (key, billing_period_start), so a redelivered event must
    resolve to the same partition every time or the uniqueness that makes redelivery safe
    would not hold.
    """
    return month_start(period_month(instant))


def whole_days_between(start: datetime, end: datetime) -> int:
    """Local whole days in a half-open [start, end) span.

    ADR-0006 prorates by whole days with the change day belonging to the NEW plan, so a
    segment owns every local date from its start date up to (not including) its end date.
    A zero-day segment is legal (two changes on the same day) and costs nothing.
    """
    if end <= start:
        return 0
    return (local_date(end) - local_date(start)).days
