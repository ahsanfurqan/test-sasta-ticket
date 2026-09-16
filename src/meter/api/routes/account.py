"""Customer-facing account endpoints: what have I used, and what am I being charged?

Capabilities 3 and 5 of the brief. Authenticated by the customer's own API key -- a customer
sees their own figures and nobody else's, without needing to know an internal id.

## These are authenticated but NOT billable

They live under `/v1/` and so pass through the metering middleware, which exempts them.
ADR-0007 defines a billable request as one we authenticated and processed, which read
literally would bill a customer for asking what they owe. That is the same objection that
ruled out billing a request refused for hitting a spending limit: charging someone to be
told their own balance is indefensible, and the volume is negligible either way.

## Speed

The live figure comes from the Redis counter, so it is near-current rather than exact --
which is the trade ADR-0010 makes deliberately. The cost is rated from that counter through
`meter.segments` and `meter.domain`, the same path the invoice run takes, so the estimate
and the eventual bill cannot drift apart by anything except the counter's lag.

Rating here is affordable because this is an account endpoint called occasionally, not the
metered path called thousands of times a second. It must never become a model for `/v1/echo`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status

from meter import billing_calendar
from meter import segments as segment_resolver
from meter.api.auth import Caller, require_api_key
from meter.domain.proration import rate_period
from meter.money import format_paisa
from meter.storage.repositories import invoices as invoices_repo
from meter.storage.repositories import periods as periods_repo
from meter.storage.repositories import usage as usage_repo

router = APIRouter(prefix="/v1", tags=["account"])

#: Resolved by the metering middleware; this dependency just hands it over.
Authenticated = Depends(require_api_key)


@router.get("/usage")
async def usage(request: Request, caller: Caller = Authenticated) -> dict:
    """Usage so far this month, and what it will cost if it stopped now."""
    context = request.app.state.hot_path
    now = datetime.now(UTC)
    period = usage_repo.period_for(now)

    counter = await context.cache.get(
        usage_repo.billable_counter_key(caller.customer_id, period.label)
    )
    threshold = await context.cache.get(
        usage_repo.threshold_key(caller.customer_id, period.label)
    )
    counted = int(counter or 0)

    async with request.app.state.engine.connect() as conn:
        period_month = billing_calendar.period_month(now)
        period_row = await periods_repo.get_period(conn, caller.customer_id, period_month)
        if period_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no billing period open for {period.label}",
            )
        resolved = await segment_resolver.resolve(
            conn,
            caller.customer_id,
            period_row.id,
            period_row.period_month,
            period_row.period_start,
            period_row.period_end,
        )

    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no plan is in effect for this period, so usage cannot be priced",
        )

    # Attribute everything counted so far to the segment in effect now. The live figure is
    # explicitly allowed to be approximate (ADR-0010); the invoice rates each segment from
    # the rollups, which is exact. Saying so in the response is better than implying an
    # exactness this number does not have.
    domain_segments = [segment.domain for segment in resolved]
    already_attributed = sum(segment.quantity for segment in resolved)
    tail = max(0, counted - already_attributed)
    domain_segments[-1] = type(domain_segments[-1])(
        price_list=domain_segments[-1].price_list,
        days=domain_segments[-1].days,
        days_in_month=domain_segments[-1].days_in_month,
        quantity=domain_segments[-1].quantity + tail,
    )

    charge = rate_period(domain_segments)
    raw_threshold = int(threshold) if threshold is not None else None
    # A negative threshold is the unsatisfiable sentinel (see meter.pipeline.thresholds).
    # This endpoint is the one place a cut-off customer can find out WHY, so it must not
    # report an impossible limit as an ordinary exhausted one.
    unsatisfiable = raw_threshold is not None and raw_threshold < 0
    limit = None if unsatisfiable else raw_threshold

    return {
        "billing_period": period.label,
        "requests_this_period": counted,
        "estimated_cost_paisa": charge.total_paisa,
        "estimated_cost": format_paisa(charge.total_paisa),
        "as_of": now.isoformat(),
        "freshness": (
            "near-current: counted from the live counter, which can lag the durable record "
            "by the buffer window. The invoice is rated from the durable record and is exact."
        ),
        "spending_limit": {
            "request_threshold": limit,
            "requests_remaining": None if limit is None else max(0, limit - counted),
            "serving": not unsatisfiable and (limit is None or counted < limit),
            "unsatisfiable": unsatisfiable,
            "why": (
                "this period's plan fee alone exceeds your spending limit, so the limit "
                "cannot be met however little you use. Refusing requests does not reduce "
                "the fee -- it is owed for the days you were on the plan. Raise the limit "
                "or change plan."
                if unsatisfiable
                else None
            ),
        },
        "segments": [
            {
                "plan": segment.price_list_name,
                "price_list_version": segment.price_list_version,
                "days": segment.days,
                "days_in_month": segment.days_in_month,
                "requests": segment.quantity,
                "prorated_monthly_fee": format_paisa(segment.prorated_fee_paisa),
                "prorated_included_requests": segment.prorated_allowance,
                "cost": format_paisa(segment.total_paisa),
            }
            for segment in charge.segments
        ],
        "explanation": charge.explain(),
    }


@router.get("/invoices")
async def list_invoices(request: Request, caller: Caller = Authenticated) -> dict:
    """Every invoice issued to this customer, newest first."""
    async with request.app.state.engine.connect() as conn:
        rows = await invoices_repo.list_for_customer(conn, caller.customer_id)

    return {
        "invoices": [
            {
                "invoice_number": row.invoice_number,
                "billing_period": row.period_month.strftime("%Y-%m") if row.period_month else None,
                "status": row.status,
                "total_paisa": row.total_paisa,
                "total": format_paisa(row.total_paisa),
                "issued_at": row.issued_at.isoformat() if row.issued_at else None,
            }
            for row in rows
        ]
    }


@router.get("/invoices/{invoice_number}")
async def get_invoice(
    invoice_number: str, request: Request, caller: Caller = Authenticated
) -> dict:
    """One invoice, with every line explained.

    This is the brief's last definition-of-done step: pick a line, ask why it says what it
    says, and get the answer from the system. Each line carries the quantity, the unit price,
    the amount, and the price list VERSION it was computed from -- so the charge re-derives
    from stored facts rather than from anyone's memory, and a later price change cannot alter
    what this line means.
    """
    async with request.app.state.engine.connect() as conn:
        invoice = await invoices_repo.get_by_number(conn, invoice_number)
        if invoice is None or invoice.customer_id != caller.customer_id:
            # Not found and not yours are the same answer: otherwise this endpoint confirms
            # which invoice numbers exist for other customers.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such invoice"
            )
        lines = await invoices_repo.lines_for(conn, invoice.id)

    return {
        "invoice_number": invoice.invoice_number,
        "billing_period": invoice.period_month.strftime("%Y-%m") if invoice.period_month else None,
        "status": invoice.status,
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "total_paisa": invoice.total_paisa,
        "total": format_paisa(invoice.total_paisa),
        "immutable": invoice.status == "issued",
        "lines": [
            {
                "line": line.line_number,
                "kind": line.kind,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price_paisa": line.unit_price_paisa,
                "unit_price": format_paisa(line.unit_price_paisa),
                "amount_paisa": line.amount_paisa,
                "amount": format_paisa(line.amount_paisa),
                "price_list_version_id": line.price_list_version_id,
                "band_index": line.band_index,
                "why": (
                    f"{line.quantity:,} x {format_paisa(line.unit_price_paisa)} "
                    f"= {format_paisa(line.amount_paisa)}, priced by price list version "
                    f"{line.price_list_version_id}"
                    if line.kind == "usage" or line.kind == "prior_period_usage"
                    else line.description
                ),
            }
            for line in lines
        ],
        "lines_sum_to_total": sum(line.amount_paisa for line in lines) == invoice.total_paisa,
    }
