"""Turning stored facts into the domain's proration segments. ADR-0006, ADR-0017.

`data-model` stores plan assignments as non-overlapping time ranges. `billing-domain` rates
a tuple of `Segment(price_list, days, days_in_month, quantity)`. This module is the join
between them, and it is the only place in the pipeline that converts instants into days.

What it must get right, because everything downstream trusts it:

* **The change day belongs to the new plan** (ADR-0006). A segment owns every local date
  from its start up to, not including, its end -- which falls straight out of the half-open
  `tstzrange` the schema already stores.
* **A zero-day segment is legal** (ADR-0017). Two changes on the same day leave the middle
  plan with no days. It costs nothing, carries no allowance, and must never have usage
  attributed to it -- so if usage IS attributed to one, that is reported rather than hidden.
* **Segments may not cover the whole month** (ADR-0017). A mid-month signup has no segment
  before it signed up. `rate_period` is explicitly fine with that; nothing here should
  invent a segment to fill the gap.
* **The prorated price list is derived, never persisted.** The stored facts are
  (version, segment days, days in month); `prorate()` re-derives the rest on demand, which
  is what keeps a historical charge reproducible.
"""

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from meter.domain.plans import PriceList
from meter.domain.proration import Segment
from meter.pipeline import clock
from meter.storage.repositories import periods, rollups

logger = logging.getLogger("meter.pipeline.segments")


@dataclass(frozen=True, slots=True)
class PeriodSegment:
    """One proration segment, with the storage identity the invoice line has to cite."""

    assignment_id: str
    price_list_version_id: str
    price_list: PriceList
    days: int
    days_in_month: int
    quantity: int

    @property
    def domain(self) -> Segment:
        return Segment(
            price_list=self.price_list,
            days=self.days,
            days_in_month=self.days_in_month,
            quantity=self.quantity,
        )


async def resolve(
    conn: AsyncConnection,
    customer_id: str,
    period_id: str,
    period_month: date,
    period_start,
    period_end,
    *,
    quantities: dict[str, int] | None = None,
) -> list[PeriodSegment]:
    """The customer's segments for one period, with usage attached.

    `quantities` overrides the rollup usage per assignment -- the threshold computation
    uses it to ask "what if this segment had N requests?", which is the inversion ADR-0008
    needs. Left out, the usage is what the rollups say.
    """
    assignments = await periods.assignments_in_period(
        conn, customer_id, period_start, period_end
    )
    if not assignments:
        return []

    if quantities is None:
        quantities = {
            usage.plan_assignment_id: usage.billable_requests
            for usage in await rollups.segment_usage(conn, customer_id, period_id)
        }

    price_lists = await periods.load_price_lists(
        conn, [assignment.price_list_version_id for assignment in assignments]
    )
    days_in_month = clock.days_in_month(period_month)

    resolved: list[PeriodSegment] = []
    for assignment in assignments:
        days = clock.whole_days_between(assignment.start, assignment.end)
        quantity = quantities.get(assignment.assignment_id, 0)
        if days == 0 and quantity:
            # ADR-0017 is explicit that this must never happen. If it does, the segment
            # boundaries and the usage attribution disagree, and a silent zero-day segment
            # would drop the usage from the bill entirely.
            logger.error(
                "customer %s has %d requests attributed to a ZERO-DAY segment %s in %s: "
                "the usage cannot be rated against a segment with no allowance and no "
                "bands, and would be lost",
                customer_id,
                quantity,
                assignment.assignment_id,
                period_month,
            )
        resolved.append(
            PeriodSegment(
                assignment_id=assignment.assignment_id,
                price_list_version_id=assignment.price_list_version_id,
                price_list=price_lists[assignment.price_list_version_id],
                days=days,
                days_in_month=days_in_month,
                quantity=quantity,
            )
        )
    return resolved
