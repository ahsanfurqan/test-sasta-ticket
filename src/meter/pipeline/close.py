"""Month close: reconcile, THEN issue. ADR-0010.

The sequence, and why each step is where it is:

1. **Mark the period `reconciling`.** The boundary has passed; the period no longer counts
   new usage toward its own total. Late usage is still *recorded* against the period it was
   incurred in -- that is what makes roll-forward possible -- it simply stops changing the
   number being closed.
2. **Drain the buffer to empty.** Not "wait a bit": drain until `XPENDING` and the group lag
   are both zero, so what is in Postgres is everything that was captured.
3. **Re-aggregate the whole period from the per-request rows.** Not from the incremental
   dirty set, which lives in Redis and is therefore not the system of record.
4. **Reconcile.** Redis counter against Postgres truth, as a number with a query behind it.
5. **Issue when it converges.** Or, when the grace window elapses first, issue anyway and
   write the shortfall into `billing_periods.discrepancy_requests` -- loudly, because "a
   recorded-but-unnoticed discrepancy is worse than no record at all, since it creates the
   appearance of control".

The grace window is configuration (`month_close_grace_seconds`), not a constant, because
ADR-0010 says the right number depends on drain latency we do not have yet and expects the
first value to be wrong.

**The invoice run is the same code whether it is triggered by the calendar or by hand.**
`close_customer` is what the worker's scheduler calls and what `python -m meter.pipeline.cli
close-month` calls, so a demo on the 16th exercises the path that runs on the 1st.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from meter.config import Settings
from meter.pipeline import aggregate, clock, drain, invoicing, reconcile
from meter.pipeline.reconcile import Reconciliation
from meter.storage.repositories import invoices as invoice_repo
from meter.storage.repositories import periods

logger = logging.getLogger("meter.pipeline.close")


@dataclass(slots=True)
class CloseResult:
    customer_id: str
    period_month: date
    converged: bool
    waited_seconds: float
    reconciliation: Reconciliation
    invoice: invoicing.InvoiceResult | None = None
    discrepancy_requests: int = 0
    error: str | None = None

    def explain(self) -> str:
        head = (
            f"close {self.customer_id} {self.period_month}: "
            f"{'converged' if self.converged else 'GRACE WINDOW ELAPSED'} after "
            f"{self.waited_seconds:.1f}s"
        )
        body = [self.reconciliation.explain()]
        if self.discrepancy_requests:
            body.append(
                f"  DISCREPANCY: {self.discrepancy_requests:,} requests unaccounted for at "
                "issue time, recorded on the billing period"
            )
        if self.invoice is not None:
            body.append(self.invoice.explain())
        if self.error:
            body.append(f"  ERROR: {self.error}")
        return "\n".join([head, *body])


async def close_customer(
    settings: Settings,
    engine: AsyncEngine,
    redis: aioredis.Redis,
    drainer: drain.Drain,
    customer_id: str,
    period_month: date,
    *,
    grace_seconds: float | None = None,
) -> CloseResult:
    """Reconcile then issue for one customer-period. Safe to re-run at any point."""
    grace = settings.month_close_grace_seconds if grace_seconds is None else grace_seconds
    started = time.perf_counter()

    async with engine.begin() as conn:
        period = await periods.ensure_period(conn, customer_id, period_month)
        if period.status == "open":
            await periods.mark_status(conn, period.id, "reconciling")

    report = await _converge(
        settings, engine, redis, drainer, customer_id, period_month, grace
    )
    waited = time.perf_counter() - started

    # Both directions count. A negative counter delta (Postgres ahead of Redis) is a worse
    # sign than a positive one, not a credit against it.
    discrepancy = (
        0 if report.converged else abs(report.counter_delta) + abs(report.rollup_delta)
    )
    if not report.converged:
        logger.error(
            "GRACE WINDOW ELAPSED for customer %s %s after %.1fs without convergence. "
            "Issuing anyway (ADR-0010's fallback) with a recorded shortfall of %d "
            "requests.\n%s",
            customer_id,
            period_month,
            waited,
            discrepancy,
            report.explain(),
        )

    async with engine.begin() as conn:
        await periods.mark_status(
            conn,
            period.id,
            "closed",
            reconciled=report.converged,
            discrepancy_requests=discrepancy or None,
            discrepancy_note=(
                None
                if report.converged
                else (
                    f"grace window of {grace:.0f}s elapsed before reconciliation "
                    f"converged: redis counter {report.redis_counter}, usage_events "
                    f"{report.events_billable}, usage_rollups {report.rollup_billable}, "
                    f"{report.stream_outstanding} entries still in the buffer, "
                    f"{report.unattributed} requests with no plan assignment"
                )
            ),
        )

    try:
        invoice = await invoicing.generate(engine, customer_id, period_month)
    except (invoicing.NothingToInvoice, invoice_repo.InvoiceImmutable) as exc:
        logger.error("cannot invoice customer %s for %s: %s", customer_id, period_month, exc)
        return CloseResult(
            customer_id=customer_id,
            period_month=period_month,
            converged=report.converged,
            waited_seconds=waited,
            reconciliation=report,
            discrepancy_requests=discrepancy,
            error=str(exc),
        )

    result = CloseResult(
        customer_id=customer_id,
        period_month=period_month,
        converged=report.converged,
        waited_seconds=waited,
        reconciliation=report,
        invoice=invoice,
        discrepancy_requests=discrepancy,
    )
    logger.info("%s", result.explain())
    return result


async def _converge(
    settings: Settings,
    engine: AsyncEngine,
    redis: aioredis.Redis,
    drainer: drain.Drain,
    customer_id: str,
    period_month: date,
    grace_seconds: float,
) -> Reconciliation:
    """Drain, aggregate, reconcile -- until it converges or the grace window runs out."""
    deadline = time.monotonic() + grace_seconds
    report: Reconciliation | None = None
    while True:
        await drainer.drain_until_empty()
        await aggregate.aggregate_period(engine, customer_id, period_month)
        report = await reconcile.reconcile(
            engine, redis, customer_id, period_month, drainer=drainer
        )
        if report.converged or time.monotonic() >= deadline:
            return report
        await asyncio.sleep(settings.month_close_poll_seconds)


async def close_month(
    settings: Settings,
    engine: AsyncEngine,
    redis: aioredis.Redis,
    drainer: drain.Drain,
    period_month: date,
    *,
    grace_seconds: float | None = None,
) -> list[CloseResult]:
    """Close every customer with a period for one month. The scheduled job, and the demo."""
    async with engine.connect() as conn:
        rows = await periods.periods_for_month(conn, period_month)

    pending = [row for row in rows if row.status != "invoiced"]
    logger.info(
        "closing %s: %d periods, %d already invoiced",
        period_month,
        len(pending),
        len(rows) - len(pending),
    )
    results = []
    for row in pending:
        results.append(
            await close_customer(
                settings,
                engine,
                redis,
                drainer,
                row.customer_id,
                period_month,
                grace_seconds=grace_seconds,
            )
        )
    return results


async def due_month(engine: AsyncEngine, at=None) -> date | None:
    """The most recent month that has ended and still has an uninvoiced period.

    Deliberately not "is it the 1st?": a worker that was down on the 1st must still close
    the month when it comes back, and a worker that is up on the 3rd must not re-close a
    month it already finished.
    """
    at = at or clock.now()
    month = clock.previous_month(clock.period_month(at))
    async with engine.connect() as conn:
        rows = await periods.periods_for_month(conn, month)
    if any(row.status != "invoiced" for row in rows):
        return month
    return None
