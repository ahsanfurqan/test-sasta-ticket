"""An HTTP trigger for every pipeline stage. Owned by pipeline.

## Why this is on the worker and not the API

`meter.api` may not import `meter.pipeline` -- they are siblings in the layers contract, and
`make lint` enforces it. Close orchestration lives here, so the trigger for it lives here
too. Putting a thin proxy on the customer API would either break the contract or duplicate
the orchestration, and duplicating a money-producing job is exactly what the boundary exists
to prevent.

The practical consequence is a second port. That is the honest cost of the layering, and it
is a small one for an operations surface.

## What it is for

Two audiences, the same need. A walkthrough must not require waiting for the 1st of the
month or shelling into a container mid-demo; an incident must not require either. Every
endpoint here calls the SAME function the scheduled loop calls, so what a demo exercises is
the production path rather than a parallel one written to be demonstrable.

## What it deliberately does not offer

There is no "just issue the invoice" endpoint, for the same reason the CLI has no such
subcommand. `invoicing.generate` issues whatever the rollups currently say, and ADR-0010's
whole point is that the rollups are not trustworthy until the buffer is drained and
reconciliation has proved nothing is outstanding. That shortcut existed once, was used on a
customer whose requests were still draining, and issued an immutable invoice Rs. 22,049.65
short -- which the database then correctly refused to let anyone repair.

`close-customer` is drain, aggregate, reconcile, then issue. It is the only path to an
invoice, and it is safe to re-run.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from meter import billing_calendar as clock
from meter.pipeline import aggregate, close, counters, reconcile, thresholds

logger = logging.getLogger("meter.pipeline.ops")

router = APIRouter(prefix="/ops", tags=["ops"])


class CloseCustomer(BaseModel):
    customer_id: str
    #: Defaults to the period now open. Pass "2026-09" to close a month that has passed.
    month: str | None = None
    #: Override the grace window ADR-0010 waits before issuing a short invoice. 0 means
    #: "issue whatever reconciliation currently says", which is only ever right in a demo.
    grace_seconds: float | None = None


class CloseMonth(BaseModel):
    month: str | None = None
    grace_seconds: float | None = None


def _month(raw: str | None) -> date:
    if raw is None:
        return clock.period_month(clock.now())
    try:
        return datetime.strptime(raw, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"month must look like 2026-09, got {raw!r}",
        ) from exc


def _deps(request: Request):
    state = request.app.state
    return state.settings, state.engine, state.redis, state.drainer


@router.post("/close-customer")
async def close_one(body: CloseCustomer, request: Request) -> dict:
    """Drain, aggregate, reconcile, then issue -- for one customer. Safe to re-run.

    Re-running an already-closed period returns the same invoice rather than a second one:
    the invoice is immutable and regeneration must agree with it or fail loudly, never
    quietly write a new number.
    """
    settings, engine, redis, drainer = _deps(request)
    result = await close.close_customer(
        settings, engine, redis, drainer, body.customer_id, _month(body.month),
        grace_seconds=body.grace_seconds,
    )
    return _close_payload(result)


@router.post("/close-month")
async def close_all(body: CloseMonth, request: Request) -> dict:
    """The same, for every customer with a period open in that month."""
    settings, engine, redis, drainer = _deps(request)
    results = await close.close_month(
        settings, engine, redis, drainer, _month(body.month),
        grace_seconds=body.grace_seconds,
    )
    return {
        "month": _month(body.month).strftime("%Y-%m"),
        "customers": len(results),
        "issued": sum(1 for r in results if r.invoice is not None),
        "not_converged": [r.customer_id for r in results if not r.converged],
        "results": [_close_payload(r) for r in results],
    }


def _close_payload(result: close.CloseResult) -> dict:
    return {
        "customer_id": result.customer_id,
        "period_month": result.period_month.strftime("%Y-%m"),
        # False means the grace window elapsed with usage still outstanding, so the invoice
        # was issued short and the shortfall recorded. Never silently.
        "converged": result.converged,
        "waited_seconds": round(result.waited_seconds, 2),
        "discrepancy_requests": result.discrepancy_requests,
        "error": result.error,
        "invoice": (
            {
                "invoice_number": result.invoice.invoice_number,
                "total_paisa": result.invoice.total_paisa,
                "prior_period_paisa": result.invoice.prior_period_paisa,
                "prior_period_requests": result.invoice.prior_period_requests,
                "already_existed": result.invoice.already_existed,
            }
            if result.invoice is not None
            else None
        ),
        # The reconciliation number and the SQL behind it, always -- an invoice issued
        # without showing what was reconciled is an invoice nobody can check.
        "explain": result.explain(),
    }


@router.post("/drain")
async def drain_now(request: Request) -> dict:
    _, _, _, drainer = _deps(request)
    result = await drainer.drain_until_empty()
    return {
        "entries_read": result.entries_read,
        "rows_inserted": result.rows_inserted,
        # Redelivery after a crash lands here rather than double-counting: the idempotency
        # key makes the second insert a no-op, and this counts how often that happened.
        "duplicates_ignored": result.duplicates_ignored,
        "dead_lettered": result.dead_lettered,
        "acked": result.acked,
        "lag_ms": result.lag_ms,
    }


@router.post("/aggregate")
async def aggregate_now(request: Request) -> dict:
    _, engine, redis, _ = _deps(request)
    result = await aggregate.aggregate_dirty(engine, redis)
    return {
        "cells": result.cells,
        "rows_written": result.rows_written,
        # Events that matched no plan assignment. Counted rather than dropped: a request
        # from a customer who was on no plan at that instant is an anomaly to look at, not
        # something to forget silently.
        "unattributed": result.unattributed,
    }


@router.post("/thresholds")
async def thresholds_now(request: Request) -> dict:
    settings, engine, redis, _ = _deps(request)
    result = await thresholds.sweep(
        engine, redis, max_age_seconds=settings.threshold_max_age_seconds
    )
    return {
        "examined": result.examined,
        "recomputed": result.recomputed,
        "skipped_no_plan": result.skipped_no_plan,
        "oldest_age_seconds": round(result.oldest_age_seconds, 1),
        # ADR-0012's upgrade-exhausts-the-limit case, carried out rather than only logged.
        "limit_exhausted": result.exhausted,
    }


@router.post("/rebuild-counters")
async def rebuild_now(request: Request) -> dict:
    """ADR-0011's recovery path: rebuild counters from Postgres and mark them authoritative.

    Until the marker is set the hot path refuses everything, because a Redis that came back
    empty would otherwise serve happily against a zero counter -- which is worse than being
    down, and is the exact scenario failing closed was chosen for.
    """
    _, engine, redis, _ = _deps(request)
    result = await counters.rebuild(engine, redis)
    return {
        "period_month": result.period_month.strftime("%Y-%m"),
        "customers": result.customers,
        "requests": result.requests,
        "thresholds_written": result.thresholds_written,
        "seconds": round(result.seconds, 3),
        "authoritative": True,
    }


@router.get("/reconcile/{customer_id}")
async def reconcile_one(customer_id: str, request: Request, month: str | None = None) -> dict:
    """The number that must be zero, with the SQL that produced it."""
    _, engine, redis, drainer = _deps(request)
    result = await reconcile.reconcile(
        engine, redis, customer_id, _month(month), drainer=drainer
    )
    return {
        "converged": result.converged,
        "redis_counter": result.redis_counter,
        "events_billable": result.events_billable,
        "rollup_billable": result.rollup_billable,
        "stream_outstanding": result.stream_outstanding,
        "explain": result.explain(),
    }


def create_ops_app(settings, engine, redis, drainer) -> FastAPI:
    app = FastAPI(
        title="Metering pipeline ops",
        version="0.1.0",
        description=__doc__,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.redis = redis
    app.state.drainer = drainer
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    return app
