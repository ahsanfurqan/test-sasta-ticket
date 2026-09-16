"""Aggregation into rollups. ADR-0016.

Rollups are written HERE, at aggregation time, and never derived from `usage_events` at
query time -- because per-request rows live 90 days and rollups live forever. A rollup bug
found on day 91 is unrecoverable for the affected period, which is why this module does the
least clever thing available: it re-derives a whole cell from the per-request rows and
*assigns* the result, so running it twice is the same as running it once.

Grain, per ADR-0016 and generously: **customer x local day x price list version x plan
segment x api key**. The key is in the grain so "which of my keys caused the March spike?"
is answerable in April, after the detail has gone.

Two passes, one statement:

* **Incremental.** The drain marks every (customer, local day) it touched. This pass
  re-aggregates exactly those cells, which is what keeps the live usage figure near-current.
  The dirty set lives in Redis, and Redis is never the system of record -- so losing it
  costs freshness, not correctness.
* **Whole-period.** Month close re-aggregates the entire period from Postgres before it
  reconciles. That is the safety net: whatever the dirty set did or did not remember, the
  rollups that an invoice is computed from were derived from the per-request rows minutes
  earlier.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meter import billing_calendar as clock
from meter.pipeline import drain
from meter.storage.repositories import periods, rollups

logger = logging.getLogger("meter.pipeline.aggregate")


@dataclass(slots=True)
class AggregationResult:
    cells: int = 0
    rows_written: int = 0
    unattributed: int = 0


def cell_for(customer_id: str, usage_date: date) -> rollups.Cell:
    """Resolve a local day to its UTC bounds and its period, once, here.

    `meter.storage` does not own the timezone and `meter.domain` must never see one, so
    the conversion happens in exactly this function and the repository receives instants.
    """
    day_start = datetime(
        usage_date.year, usage_date.month, usage_date.day, tzinfo=clock.BILLING_TZ
    ).astimezone(clock.UTC)
    day_end = day_start + timedelta(days=1)
    # A local day never straddles a local month boundary, so one day is in one period.
    return rollups.Cell(
        customer_id=customer_id,
        usage_date=usage_date,
        period_start=clock.month_start(date(usage_date.year, usage_date.month, 1)),
        day_start=day_start,
        day_end=day_end,
    )


async def aggregate_dirty(engine: AsyncEngine, redis: aioredis.Redis) -> AggregationResult:
    """Re-aggregate every cell the drain has touched since the last pass.

    Order: read the dirty set, ensure each cell's period exists, aggregate, COMMIT, and
    only then clear the set. A crash anywhere before the clear leaves the cells marked and
    the next pass repeats work that is defined to be repeatable.
    """
    cells = await drain.take_dirty_cells(redis)
    if not cells:
        return AggregationResult()

    resolved = [cell_for(customer_id, day) for customer_id, day in cells]

    try:
        written = await _write(engine, resolved)
    except SQLAlchemyError:
        # One statement covers many cells, so one bad cell would otherwise fail the pass
        # for every customer in it -- and, because the cells stay dirty, fail the next pass
        # too, and the next. That is a wedged pipeline, which is worse than a wrong rollup:
        # nothing aggregates at all until someone notices. Fall back to one cell at a time
        # and quarantine what still fails.
        logger.exception("aggregation pass failed for %d cells; isolating", len(resolved))
        written = await _write_one_at_a_time(engine, redis, resolved)

    await drain.clear_dirty_cells(
        redis, [drain.cell_member(cell.customer_id, cell.usage_date) for cell in resolved]
    )
    return AggregationResult(cells=len(resolved), rows_written=written)


async def _write(engine: AsyncEngine, cells: list[rollups.Cell]) -> int:
    async with engine.begin() as conn:
        # A rollup references a billing period, so the period row has to exist first. It
        # is created from usage rather than from a calendar: a customer with no traffic in
        # a month has no period, and nothing to invoice.
        for customer_id, month in sorted(
            {
                (cell.customer_id, date(cell.usage_date.year, cell.usage_date.month, 1))
                for cell in cells
            }
        ):
            await periods.ensure_period(conn, customer_id, month)
        return await rollups.aggregate_cells(conn, cells)


async def _write_one_at_a_time(
    engine: AsyncEngine, redis: aioredis.Redis, cells: list[rollups.Cell]
) -> int:
    """Aggregate cell by cell so a single poisoned cell costs only itself.

    A cell that still fails is dropped from the dirty set rather than retried forever. It
    is NOT lost: the per-request rows are untouched, month close re-aggregates the whole
    period from them, and reconciliation reports the shortfall as a rollup delta. The
    choice here is between one loud, visible gap and a silent total stall.
    """
    written = 0
    for cell in cells:
        try:
            written += await _write(engine, [cell])
        except SQLAlchemyError:
            logger.exception(
                "cannot aggregate customer %s for %s; quarantining the cell. The usage "
                "rows are still in usage_events and reconciliation will report the "
                "shortfall, but this period cannot be invoiced until it is fixed",
                cell.customer_id,
                cell.usage_date,
            )
    return written


async def aggregate_period(
    engine: AsyncEngine, customer_id: str, period_month: date
) -> AggregationResult:
    """Re-derive every rollup for one customer-period from the per-request rows.

    Month close runs this before it reconciles, so the numbers an invoice is built from
    were derived from `usage_events` moments earlier rather than accumulated over a month
    of incremental passes that nobody re-checked.
    """
    period_start = clock.month_start(period_month)
    async with engine.begin() as conn:
        await periods.ensure_period(conn, customer_id, period_month)
        written = await rollups.aggregate_period(conn, customer_id, period_start)
        unattributed = await rollups.unattributed_events(conn, customer_id, period_start)

    if unattributed:
        logger.error(
            "customer %s has %d served requests in %s with no plan assignment covering "
            "them: they are recorded but cannot be rated, and they will show up in "
            "reconciliation as a rollup shortfall",
            customer_id,
            unattributed,
            period_month,
        )
    return AggregationResult(cells=1, rows_written=written, unattributed=unattributed)
