"""Manual triggers for every pipeline stage. Owned by pipeline.

A demo must not require waiting for the 1st of the month, and an incident must not require
waiting for the next scheduler tick. Every stage the worker runs on a timer is also a
subcommand here, running the SAME functions -- so what a demo exercises is the production
path, not a parallel one written to be demonstrable.

    docker compose exec worker python -m meter.pipeline.cli drain
    docker compose exec worker python -m meter.pipeline.cli aggregate
    docker compose exec worker python -m meter.pipeline.cli reconcile --customer <uuid>
    docker compose exec worker python -m meter.pipeline.cli thresholds
    docker compose exec worker python -m meter.pipeline.cli rebuild-counters
    docker compose exec worker python -m meter.pipeline.cli close-month --month 2026-09
    docker compose exec worker python -m meter.pipeline.cli close-customer --customer <uuid>
    docker compose exec worker python -m meter.pipeline.cli status

There is deliberately NO "just generate the invoice" subcommand. `invoicing.generate` issues
whatever the rollups currently say, and ADR-0010's whole point is that the rollups are not
trustworthy until the buffer has been drained and reconciliation has converged. An early
draft of this CLI exposed it directly; the first time it was used on a customer whose
1.2 million requests were still draining, it issued an immutable invoice for Rs. 312,950.35
against a true total of Rs. 335,000.00. The immutability trigger then refused to fix it --
correctly, and expensively. Both close commands go through `close.close_customer`, which
drains, aggregates and reconciles before it issues.
"""

import argparse
import asyncio
import logging
from datetime import date

from meter.config import get_settings
from meter.pipeline import (
    aggregate,
    clock,
    close,
    counters,
    drain,
    keys,
    reconcile,
    thresholds,
)
from meter.storage import cache, db

logger = logging.getLogger("meter.pipeline.cli")


def _month(raw: str | None) -> date:
    """YYYY-MM, or the current billing month in Asia/Karachi."""
    if not raw:
        return clock.period_month(clock.now())
    year, month = raw.split("-")[:2]
    return date(int(year), int(month), 1)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    engine = db.create_engine(settings)
    redis = cache.create_client(settings)
    drainer = drain.Drain(settings, engine, redis)
    await drainer.ensure_group()

    try:
        if args.command == "drain":
            result = await drainer.drain_until_empty()
            print(
                f"drained {result.entries_read} entries: {result.rows_inserted} new rows, "
                f"{result.duplicates_ignored} already present, "
                f"{result.dead_lettered} dead-lettered, lag {result.lag_ms}ms"
            )

        elif args.command == "aggregate":
            if args.customer:
                summary = await aggregate.aggregate_period(
                    engine, args.customer, _month(args.month)
                )
            else:
                summary = await aggregate.aggregate_dirty(engine, redis)
            print(
                f"aggregated {summary.cells} cells into {summary.rows_written} rollup rows"
                + (
                    f"; {summary.unattributed} unattributable requests"
                    if summary.unattributed
                    else ""
                )
            )

        elif args.command == "reconcile":
            report = await reconcile.reconcile(
                engine, redis, args.customer, _month(args.month), drainer=drainer
            )
            print(report.explain())
            return 0 if report.unexplained == 0 else 1

        elif args.command == "thresholds":
            if args.customer:
                threshold = await thresholds.recompute_for(
                    engine, redis, args.customer, _month(args.month)
                )
                print("no spending limit for that customer-period" if threshold is None
                      else f"threshold = {threshold.threshold_requests} requests "
                           f"(fee component {threshold.fee_component_paisa} paisa)")
            else:
                summary = await thresholds.sweep(
                    engine, redis, max_age_seconds=settings.threshold_max_age_seconds
                )
                print(
                    f"examined {summary.examined}, recomputed {summary.recomputed}, "
                    f"skipped {summary.skipped_no_plan} with no plan, oldest threshold "
                    f"{summary.oldest_age_seconds:.0f}s old"
                )

        elif args.command == "rebuild-counters":
            result = await counters.rebuild(engine, redis, _month(args.month))
            print(result.explain())

        elif args.command == "close-customer":
            result = await close.close_customer(
                settings,
                engine,
                redis,
                drainer,
                args.customer,
                _month(args.month),
                grace_seconds=args.grace,
            )
            print(result.explain())
            return 0 if result.error is None else 1

        elif args.command == "close-month":
            results = await close.close_month(
                settings,
                engine,
                redis,
                drainer,
                _month(args.month),
                grace_seconds=args.grace,
            )
            for result in results:
                print(result.explain())
                print()
            return 0 if all(result.error is None for result in results) else 1

        elif args.command == "status":
            trimmed = await drainer.trim_drained()
            length, alerting = await drainer.check_stream_bound()
            print(f"trimmed {trimmed} fully-drained entries")
            print(f"stream {settings.usage_stream_key}: {length} entries"
                  + (" -- PAST THE ALERT THRESHOLD" if alerting else ""))
            print(f"undrained (pending + lag): {await drainer.outstanding()}")
            print(f"drain lag: {await drain.lag_ms(redis)} ms")
            print(f"counters authoritative: {await counters.is_authoritative(redis)}")
            print(f"last rebuild took: {await redis.get(keys.COUNTERS_REBUILD_SECONDS)} s")
            print(
                "oldest threshold age: "
                f"{await redis.get(keys.THRESHOLD_OLDEST_AGE_SECONDS)} s"
            )
        return 0
    finally:
        await engine.dispose()
        await redis.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(prog="meter.pipeline.cli", description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "drain",
            "aggregate",
            "reconcile",
            "thresholds",
            "rebuild-counters",
            "close-month",
            "close-customer",
            "status",
        ],
    )
    parser.add_argument("--customer", help="customer uuid")
    parser.add_argument("--month", help="YYYY-MM (default: the current billing month)")
    parser.add_argument(
        "--grace",
        type=float,
        default=None,
        help="grace window in seconds, overriding month_close_grace_seconds",
    )
    args = parser.parse_args()

    if args.command in {"reconcile", "close-customer"} and not args.customer:
        parser.error(f"{args.command} needs --customer")

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
