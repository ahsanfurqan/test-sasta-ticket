"""Invoice generation. ADR-0010, ADR-0013, and every line explainable by the system.

Every number on an invoice produced here comes out of `meter.domain`. This module resolves
storage facts into `(price list version, segment days, days in month, quantity)`, hands them
to `rate_period`, and writes what comes back. It contains no arithmetic on money beyond
adding up amounts the domain produced -- deliberately, because the moment a "just this one
multiplication" appears here there are two implementations of a charge and they will
eventually disagree in front of a customer.

**Immutability is not defended by politeness.** The database refuses to update an issued
invoice (ADR-0013), so regeneration cannot "fix" anything even by accident. What
regeneration does is recompute and compare:

* same total -> return the existing invoice, having written nothing;
* different total -> raise, naming both numbers.

The second case is what late usage looks like, and the answer to it is never an edit. It is
ADR-0010's roll-forward: the usage appears on the NEXT invoice as a labelled prior-period
line, priced at the price list version in effect when it was incurred.

**Roll-forward is computed from invoice lines, not from a flag.** "How much of September has
already been billed?" is `sum(quantity)` over the issued lines for September, per plan
segment. Invoice lines are immutable, so that answer cannot drift however much late usage
arrives; a mutable `invoiced` flag on the rollup could not make the same promise.
"""

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from meter import segments
from meter.domain.plans import PriceList
from meter.domain.proration import prorate, rate_period
from meter.domain.rating import FEE, INCLUDED, USAGE, Charge, rate
from meter.money import format_paisa
from meter.storage.repositories import invoices, periods, rollups

logger = logging.getLogger("meter.pipeline.invoicing")

_KIND_FOR = {FEE: "monthly_fee", INCLUDED: "usage", USAGE: "usage"}


class NothingToInvoice(RuntimeError):
    """No plan assignment covers the period, so there is no ladder to rate against."""


@dataclass(slots=True)
class InvoiceResult:
    invoice_id: str
    invoice_number: str
    total_paisa: int
    period_total_paisa: int
    prior_period_paisa: int
    prior_period_requests: int
    lines: list[invoices.LineRow] = field(default_factory=list)
    already_existed: bool = False

    def explain(self) -> str:
        head = (
            f"{self.invoice_number}: {format_paisa(self.total_paisa)} "
            f"({format_paisa(self.period_total_paisa)} this period"
            + (
                f" + {format_paisa(self.prior_period_paisa)} rolled forward from "
                f"{self.prior_period_requests:,} late requests"
                if self.prior_period_requests
                else ""
            )
            + ")"
        )
        rows = [
            f"  {line.line_number:>2}. [{line.kind}] {line.description}: "
            f"{format_paisa(line.amount_paisa)}"
            for line in self.lines
        ]
        return "\n".join([head, *rows])


# ---------------------------------------------------------------------------------------
# Band bookkeeping. The domain does not label its usage lines with a band index, because a
# band index means nothing to a customer -- but an invoice line stores one so that Support
# can point at the rung. Reconstructing it is a walk down the same prorated ladder the
# domain walked, in the same order, which is why it cannot drift out of step.
# ---------------------------------------------------------------------------------------


def _band_indices(charge: Charge, price_list: PriceList) -> list[int | None]:
    """A band index for each line of `charge`, in line order. None where there is none.

    The index is into the PRORATED ladder that was actually rated against, not into the
    stored price list -- proration can drop a rung that has no width in a short segment
    (ADR-0017), so the two differ precisely when a segment is short. The stored facts on
    the line (version, assignment) plus the period make the prorated ladder re-derivable,
    so the index stays meaningful.
    """
    indices: list[int | None] = []
    cursor = 0
    for line in charge.lines:
        if line.kind != USAGE:
            indices.append(None)
            continue
        while (
            cursor < len(price_list.bands)
            and price_list.bands[cursor].unit_price_paisa != line.unit_price_paisa
        ):
            cursor += 1
        indices.append(cursor if cursor < len(price_list.bands) else None)
        cursor += 1
    return indices


def _chargeable_by_band(
    charge: Charge, price_list: PriceList
) -> dict[int | None, tuple[int, int]]:
    """band index -> (quantity, unit price) for the chargeable lines of a charge.

    Used to subtract "what we already billed" from "what the period now totals", band by
    band, so a roll-forward line is priced at the rung the late requests actually occupy
    rather than at the cheapest or the dearest.
    """
    by_band: dict[int | None, tuple[int, int]] = {}
    for line, index in zip(charge.lines, _band_indices(charge, price_list), strict=True):
        if line.kind == FEE:
            continue
        quantity, _ = by_band.get(index, (0, line.unit_price_paisa))
        by_band[index] = (quantity + line.quantity, line.unit_price_paisa)
    return by_band


# ---------------------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------------------


async def _current_period_lines(
    conn: AsyncConnection, customer_id: str, period, next_line: int
) -> tuple[list[invoices.LineRow], int]:
    period_segments = await segments.resolve(
        conn,
        customer_id,
        period.id,
        period.period_month,
        period.period_start,
        period.period_end,
    )
    if not period_segments:
        raise NothingToInvoice(
            f"customer {customer_id} has no plan assignment covering {period.period_month}"
        )

    charge = rate_period([segment.domain for segment in period_segments])

    lines: list[invoices.LineRow] = []
    for segment, rated in zip(period_segments, charge.segments, strict=True):
        prorated = prorate(segment.price_list, segment.days, segment.days_in_month)
        indices = _band_indices(rated.charge, prorated)
        for line, band_index in zip(rated.charge.lines, indices, strict=True):
            lines.append(
                invoices.LineRow(
                    line_number=next_line,
                    kind=_KIND_FOR[line.kind],
                    description=_describe(line, segment),
                    quantity=line.quantity,
                    unit_price_paisa=line.unit_price_paisa,
                    amount_paisa=line.amount_paisa,
                    price_list_version_id=segment.price_list_version_id,
                    plan_assignment_id=segment.assignment_id,
                    band_index=band_index,
                )
            )
            next_line += 1
    return lines, charge.total_paisa


def _describe(line, segment: segments.PeriodSegment) -> str:
    """The domain's own words, with the segment named when the period is split.

    ADR-0006's test is whether the sentence sounds fair to an annoyed customer on the
    phone, so the days are on the line rather than in a footnote.
    """
    if segment.days == segment.days_in_month:
        return line.description
    return (
        f"{line.description} [{segment.price_list.name} for {segment.days} of "
        f"{segment.days_in_month} days]"
    )


async def _prior_period_lines(
    conn: AsyncConnection, customer_id: str, period, next_line: int
) -> tuple[list[invoices.LineRow], int, int]:
    """ADR-0010's roll-forward: usage that arrived after its own invoice was issued.

    Priced at the price list version in effect when it was INCURRED, and at the marginal
    band those extra requests actually occupy -- which is why it is computed as the
    difference between rating the period's current total and rating what was already
    billed, rather than by rating the late requests as if they stood alone.
    """
    lines: list[invoices.LineRow] = []
    total_paisa = 0
    total_requests = 0

    for earlier in await periods.periods_before(conn, customer_id, period.period_month):
        if earlier.status != "invoiced":
            if earlier.status != "open":
                logger.error(
                    "customer %s has period %s in status %r with no issued invoice: its "
                    "usage is not being rolled forward, because there is no billed "
                    "baseline to roll forward FROM",
                    customer_id,
                    earlier.period_month,
                    earlier.status,
                )
            continue

        earlier_segments = await segments.resolve(
            conn,
            customer_id,
            earlier.id,
            earlier.period_month,
            earlier.period_start,
            earlier.period_end,
        )
        billed = await invoices.billed_quantity_by_segment(conn, customer_id, earlier.id)

        for segment in earlier_segments:
            already = billed.get(segment.assignment_id, 0)
            if segment.quantity <= already:
                if segment.quantity < already:
                    logger.error(
                        "customer %s segment %s in %s was billed %d requests but the "
                        "rollups now total only %d -- an invoice cannot be un-issued, so "
                        "this needs a credit note (ADR-0013), not a pipeline fix",
                        customer_id,
                        segment.assignment_id,
                        earlier.period_month,
                        already,
                        segment.quantity,
                    )
                continue

            prorated = prorate(segment.price_list, segment.days, segment.days_in_month)
            now_bands = _chargeable_by_band(rate(segment.quantity, prorated), prorated)
            billed_bands = _chargeable_by_band(rate(already, prorated), prorated)

            late = segment.quantity - already
            total_requests += late

            for band_index, (quantity, unit_price) in sorted(
                now_bands.items(), key=lambda item: (item[0] is None, item[0])
            ):
                billed_quantity, _ = billed_bands.get(band_index, (0, unit_price))
                delta = quantity - billed_quantity
                if delta <= 0:
                    continue
                amount = delta * unit_price
                total_paisa += amount
                lines.append(
                    invoices.LineRow(
                        line_number=next_line,
                        kind="prior_period_usage",
                        description=(
                            f"{delta:,} requests from "
                            f"{earlier.period_month:%B %Y}, received after that invoice "
                            f"was issued, at {format_paisa(unit_price)} each "
                            f"({segment.price_list.label})"
                        ),
                        quantity=delta,
                        unit_price_paisa=unit_price,
                        amount_paisa=amount,
                        price_list_version_id=segment.price_list_version_id,
                        plan_assignment_id=segment.assignment_id,
                        band_index=band_index,
                        usage_period_id=earlier.id,
                    )
                )
                next_line += 1

            logger.warning(
                "customer %s: %d late requests from %s roll forward onto the %s invoice",
                customer_id,
                late,
                earlier.period_month,
                period.period_month,
            )

    return lines, total_paisa, total_requests


async def build(
    conn: AsyncConnection, customer_id: str, period_month: date
) -> tuple[list[invoices.LineRow], int, int, int, int]:
    """Compute the invoice without writing anything.

    Returns (lines, total, this-period total, rolled-forward paisa, rolled-forward
    requests). Regeneration calls this and compares, which is how "the same number or fail
    loudly" is checked without going anywhere near an UPDATE.
    """
    period = await periods.get_period(conn, customer_id, period_month)
    if period is None:
        raise NothingToInvoice(f"customer {customer_id} has no period for {period_month}")

    lines, period_total = await _current_period_lines(conn, customer_id, period, 1)
    prior_lines, prior_total, prior_requests = await _prior_period_lines(
        conn, customer_id, period, len(lines) + 1
    )
    all_lines = lines + prior_lines
    return all_lines, period_total + prior_total, period_total, prior_total, prior_requests


async def generate(
    engine: AsyncEngine, customer_id: str, period_month: date
) -> InvoiceResult:
    """Produce and issue the invoice for one customer-period.

    **Call this through `meter.pipeline.close.close_customer`, never directly.** It issues
    whatever the rollups currently say, and an issued invoice cannot be corrected (ADR-0013).
    ADR-0010's "reconcile, THEN issue" is not a nicety: called on a customer whose usage is
    still draining, this function will happily issue a short invoice, and nothing afterwards
    can fix it. `close_customer` drains, aggregates and reconciles first, and records a
    discrepancy when it cannot converge.

    Idempotent where it can be (an unchanged regeneration returns the same invoice without
    writing) and loud where it cannot be (a changed one raises). There is no path through
    this function that modifies an issued invoice.
    """
    async with engine.begin() as conn:
        period = await periods.get_period(conn, customer_id, period_month)
        if period is None:
            raise NothingToInvoice(
                f"customer {customer_id} has no period for {period_month}"
            )

        lines, total, period_total, prior_total, prior_requests = await build(
            conn, customer_id, period_month
        )
        number = invoices.invoice_number(customer_id, period_month)
        existing = await invoices.get_for_period(conn, customer_id, period.id)

        if existing is not None and existing.status == "issued":
            if existing.total_paisa != total:
                raise invoices.InvoiceImmutable(
                    f"invoice {existing.invoice_number} was issued at "
                    f"{format_paisa(existing.total_paisa)} but regenerating it now gives "
                    f"{format_paisa(total)}. An issued invoice is immutable (ADR-0013): "
                    "the difference is late usage, and it belongs on the NEXT invoice as "
                    "a prior-period line (ADR-0010), not on this one."
                )
            logger.info(
                "invoice %s regenerated to the same total %s; nothing written",
                existing.invoice_number,
                format_paisa(total),
            )
            # The invoice is untouched, but the PERIOD's status is not part of the invoice
            # and a re-run of month close will have walked it back to 'closed' on its way
            # here. Restore it, so "has this month been invoiced?" keeps answering yes.
            await periods.mark_status(conn, period.id, "invoiced")
            return InvoiceResult(
                invoice_id=existing.id,
                invoice_number=existing.invoice_number,
                total_paisa=existing.total_paisa,
                period_total_paisa=period_total,
                prior_period_paisa=prior_total,
                prior_period_requests=prior_requests,
                lines=await invoices.lines_for(conn, existing.id),
                already_existed=True,
            )

        if existing is not None:
            # A draft from an interrupted run. Drafts carry no promise, so replacing one
            # is not a correction -- and the trigger would refuse if it were issued.
            await invoices.delete_draft(conn, existing.id)

        invoice_id = await invoices.create_draft(conn, customer_id, period.id, number)
        await invoices.add_lines(conn, invoice_id, lines)
        await invoices.issue(conn, invoice_id, total)
        await rollups.stamp_invoice(conn, period.id, invoice_id)
        await periods.mark_status(conn, period.id, "invoiced")

        for earlier in await periods.periods_before(conn, customer_id, period_month):
            # A rolled-forward period's late rollups now belong to an invoice too. Cells
            # that were already stamped keep their original invoice, so the stamp stays a
            # record of "this usage first reached a bill here".
            if earlier.status == "invoiced":
                await rollups.stamp_invoice(conn, earlier.id, invoice_id)

        result = InvoiceResult(
            invoice_id=invoice_id,
            invoice_number=number,
            total_paisa=total,
            period_total_paisa=period_total,
            prior_period_paisa=prior_total,
            prior_period_requests=prior_requests,
            lines=lines,
        )
    logger.info("%s", result.explain())
    return result
