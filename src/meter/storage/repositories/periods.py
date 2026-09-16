"""Billing periods, plan assignments and price lists, as the pipeline needs them.

Explicit SQL (ADR-0003's note: the ORM declares the schema, the repositories do the work).

Two rules this module keeps:

* **Asia/Karachi lives in SQL here, never in Python.** The migration resolves period
  bounds with ``AT TIME ZONE 'Asia/Karachi'``; so does this. One conversion, one place,
  and the resolved instant is what gets persisted (ADR-0009).
* **A price list version becomes a `meter.domain.plans.PriceList` and nothing else.**
  Rating has exactly one kind of input (ADR-0005), so the only job here is to hand the
  domain a faithful copy of the stored rows -- no interpretation, no defaults.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from meter.domain.plans import Band, PriceList
from meter.money import Paisa

BILLING_TZ = "Asia/Karachi"  # ADR-0009


@dataclass(frozen=True, slots=True)
class PeriodRow:
    id: str
    customer_id: str
    period_month: date
    period_start: datetime
    period_end: datetime
    status: str
    reconciled_at: datetime | None
    closed_at: datetime | None
    invoiced_at: datetime | None


@dataclass(frozen=True, slots=True)
class AssignmentRow:
    """One plan assignment clipped to a period: who, which version, over what instants."""

    assignment_id: str
    price_list_version_id: str
    start: datetime
    end: datetime


def _period_row(row) -> PeriodRow:
    return PeriodRow(
        # asyncpg hands back uuid.UUID for a uuid column. The ORM declares these columns
        # `UUID(as_uuid=False)`, so the rest of the system treats an id as a str -- convert
        # once, here, rather than leaving two representations loose in the codebase.
        id=str(row.id),
        customer_id=str(row.customer_id),
        period_month=row.period_month,
        period_start=row.period_start,
        period_end=row.period_end,
        status=row.status,
        reconciled_at=row.reconciled_at,
        closed_at=row.closed_at,
        invoiced_at=row.invoiced_at,
    )


_PERIOD_COLUMNS = (
    "id, customer_id, period_month, period_start, period_end, status, "
    "reconciled_at, closed_at, invoiced_at"
)


# The month arrives once, as a bind parameter, and every cast is applied to the derived
# column rather than to the parameter -- `:name::type` confuses the bind-parameter parser,
# and the resulting syntax error only shows up at runtime.
ENSURE_PERIOD_SQL = f"""
INSERT INTO billing_periods (customer_id, period_month, period_start, period_end)
SELECT :customer_id,
       m.month,
       (m.month::timestamp) AT TIME ZONE '{BILLING_TZ}',
       ((m.month + interval '1 month')::timestamp) AT TIME ZONE '{BILLING_TZ}'
  FROM (SELECT CAST(:period_month AS date) AS month) m
ON CONFLICT (customer_id, period_month) DO NOTHING
"""


async def ensure_period(
    conn: AsyncConnection, customer_id: str, period_month: date
) -> PeriodRow:
    """The customer-month row, created if this is the first usage we have seen for it.

    Idempotent by the unique constraint: two workers racing to open the same period both
    succeed and both get the same row.
    """
    await conn.execute(
        text(ENSURE_PERIOD_SQL),
        {"customer_id": customer_id, "period_month": period_month},
    )
    row = (
        await conn.execute(
            text(
                f"SELECT {_PERIOD_COLUMNS} FROM billing_periods "
                "WHERE customer_id = :customer_id AND period_month = :period_month"
            ),
            {"customer_id": customer_id, "period_month": period_month},
        )
    ).one()
    return _period_row(row)


async def get_period(
    conn: AsyncConnection, customer_id: str, period_month: date
) -> PeriodRow | None:
    row = (
        await conn.execute(
            text(
                f"SELECT {_PERIOD_COLUMNS} FROM billing_periods "
                "WHERE customer_id = :customer_id AND period_month = :period_month"
            ),
            {"customer_id": customer_id, "period_month": period_month},
        )
    ).one_or_none()
    return None if row is None else _period_row(row)


async def periods_for_month(conn: AsyncConnection, period_month: date) -> list[PeriodRow]:
    """Every customer's row for one month -- the sweep the close job runs."""
    rows = (
        await conn.execute(
            text(
                f"SELECT {_PERIOD_COLUMNS} FROM billing_periods "
                "WHERE period_month = :period_month ORDER BY customer_id"
            ),
            {"period_month": period_month},
        )
    ).all()
    return [_period_row(row) for row in rows]


async def periods_before(
    conn: AsyncConnection, customer_id: str, period_month: date
) -> list[PeriodRow]:
    """This customer's earlier periods, oldest first. ADR-0010's roll-forward looks here."""
    rows = (
        await conn.execute(
            text(
                f"SELECT {_PERIOD_COLUMNS} FROM billing_periods "
                "WHERE customer_id = :customer_id AND period_month < :period_month "
                "ORDER BY period_month"
            ),
            {"customer_id": customer_id, "period_month": period_month},
        )
    ).all()
    return [_period_row(row) for row in rows]


async def mark_status(
    conn: AsyncConnection,
    period_id: str,
    status: str,
    *,
    reconciled: bool = False,
    discrepancy_requests: int | None = None,
    discrepancy_note: str | None = None,
) -> None:
    """Move a period through open -> reconciling -> closed -> invoiced.

    The CHECK constraints tie `closed_at` and `invoiced_at` to the status, so they are set
    here rather than left to the caller to remember.
    """
    await conn.execute(
        text(
            """
            UPDATE billing_periods
               SET status = :status,
                   reconciled_at = CASE WHEN :reconciled THEN now() ELSE reconciled_at END,
                   closed_at = CASE
                       WHEN :status IN ('closed', 'invoiced')
                            THEN COALESCE(closed_at, now())
                       ELSE NULL END,
                   invoiced_at = CASE
                       WHEN :status = 'invoiced' THEN COALESCE(invoiced_at, now())
                       ELSE NULL END,
                   discrepancy_requests =
                       COALESCE(:discrepancy_requests, discrepancy_requests),
                   discrepancy_note = COALESCE(:discrepancy_note, discrepancy_note)
             WHERE id = :period_id
            """
        ),
        {
            "period_id": period_id,
            "status": status,
            "reconciled": reconciled,
            "discrepancy_requests": discrepancy_requests,
            "discrepancy_note": discrepancy_note,
        },
    )


async def customers_with_usage(conn: AsyncConnection, period_start: datetime) -> list[str]:
    """Every customer with a usage row in one period. Drives the counter rebuild."""
    rows = (
        await conn.execute(
            text(
                "SELECT DISTINCT customer_id FROM usage_events "
                "WHERE billing_period_start = :period_start"
            ),
            {"period_start": period_start},
        )
    ).all()
    return [str(row.customer_id) for row in rows]


async def ensure_usage_partition(conn: AsyncConnection, period_month: date) -> str:
    """Create the usage partition for a month if it does not exist.

    The migration ships this month and next; the drain calls it whenever it sees a period
    it has not seen before, so a worker that is running when the boundary passes does not
    write into the default partition. The function is idempotent by design -- see
    `meter_create_usage_partition` in migrations/versions/0002_schema.py.
    """
    return await conn.scalar(
        text("SELECT meter_create_usage_partition(:month)"), {"month": period_month}
    )


# ---------------------------------------------------------------------------------------
# Plan assignments and price lists
# ---------------------------------------------------------------------------------------


async def assignments_in_period(
    conn: AsyncConnection, customer_id: str, period_start: datetime, period_end: datetime
) -> list[AssignmentRow]:
    """The customer's plan assignments overlapping a period, clipped to it, in order.

    Clipping is what turns "a plan assignment" into "a proration segment": an assignment
    that started in June and is still open contributes only the part of it that falls
    inside this period. The exclusion constraint on `plan_assignments` guarantees these do
    not overlap, so the clipped spans tile the period with no double counting.

    Gaps are legal and are preserved: a customer who signed up on the 12th has no segment
    before the 12th, which is ADR-0017's partial period.
    """
    rows = (
        await conn.execute(
            text(
                """
                SELECT id,
                       price_list_version_id,
                       GREATEST(lower(effective), :period_start) AS seg_start,
                       LEAST(COALESCE(upper(effective), :period_end), :period_end) AS seg_end
                  FROM plan_assignments
                 WHERE customer_id = :customer_id
                   AND effective && tstzrange(:period_start, :period_end, '[)')
                 ORDER BY lower(effective)
                """
            ),
            {
                "customer_id": customer_id,
                "period_start": period_start,
                "period_end": period_end,
            },
        )
    ).all()
    return [
        AssignmentRow(
            assignment_id=str(row.id),
            price_list_version_id=str(row.price_list_version_id),
            start=row.seg_start,
            end=row.seg_end,
        )
        for row in rows
    ]


async def assignment_at(
    conn: AsyncConnection, customer_id: str, instant: datetime
) -> AssignmentRow | None:
    row = (
        await conn.execute(
            text(
                "SELECT id, price_list_version_id, lower(effective) AS seg_start, "
                "       COALESCE(upper(effective), :instant) AS seg_end "
                "  FROM plan_assignments "
                " WHERE customer_id = :customer_id AND effective @> :instant"
            ),
            {"customer_id": customer_id, "instant": instant},
        )
    ).one_or_none()
    if row is None:
        return None
    return AssignmentRow(
        assignment_id=str(row.id),
        price_list_version_id=str(row.price_list_version_id),
        start=row.seg_start,
        end=row.seg_end,
    )


async def load_price_list(conn: AsyncConnection, version_id: str) -> PriceList:
    """A stored price list version as the domain's `PriceList`. No interpretation."""
    header = (
        await conn.execute(
            text(
                "SELECT name, version, monthly_fee_paisa, included_quantity "
                "  FROM price_list_versions WHERE id = :id"
            ),
            {"id": version_id},
        )
    ).one()
    bands = (
        await conn.execute(
            text(
                "SELECT up_to, unit_price_paisa FROM price_bands "
                " WHERE price_list_version_id = :id ORDER BY band_index"
            ),
            {"id": version_id},
        )
    ).all()
    return PriceList(
        name=header.name,
        version=header.version,
        monthly_fee_paisa=Paisa(header.monthly_fee_paisa),
        included_quantity=header.included_quantity,
        bands=tuple(
            Band(up_to=band.up_to, unit_price_paisa=Paisa(band.unit_price_paisa))
            for band in bands
        ),
    )


async def load_price_lists(
    conn: AsyncConnection, version_ids: list[str]
) -> dict[str, PriceList]:
    """Several versions at once, because a period with plan changes needs all of them."""
    return {
        version_id: await load_price_list(conn, version_id)
        for version_id in dict.fromkeys(version_ids)
    }
