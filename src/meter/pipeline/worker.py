"""Worker entrypoint. Owned by pipeline.

Five concurrent loops, each of which may be killed at any instant:

* **drain** -- Redis stream to Postgres via a consumer group, XACK only after the commit
  (ADR-0018). Everything else here is downstream of it.
* **aggregate** -- the cells the drain touched, re-derived into rollups (ADR-0016).
* **thresholds** -- rupee limits inverted into request counts, republished whenever an
  input moves or the threshold simply goes stale (ADR-0008, ADR-0012).
* **watchdog** -- if `meter:counters:authoritative` is gone, Redis restarted empty, so
  counters are rebuilt from Postgres before the hot path may serve again (ADR-0011). Also
  where the stream bound and the drain lag are published.
* **close** -- once a month has ended, reconcile and then issue (ADR-0010). It runs on
  evidence rather than on the calendar, so a worker that was down on the 1st still closes
  the month when it comes back, and one that is up on the 3rd does not close it twice.

Nothing here holds state across a restart that matters. The consumer group holds the
position, the idempotency key makes redelivery safe, and every loop is re-entrant -- which
is the only way "kill it mid-flight" can be a test rather than an incident.

SIGTERM stops the loops between batches. SIGKILL does not, which is the interesting case,
and the one `tests/integration/test_drain.py` exercises.
"""

import asyncio
import contextlib
import logging
import signal

from meter.config import get_settings
from meter.pipeline import aggregate, close, counters, drain, thresholds
from meter.storage import cache, db

logger = logging.getLogger("meter.worker")

HEARTBEAT_SECONDS = 30
WATCHDOG_SECONDS = 2.0


async def _every(seconds: float, stopping: asyncio.Event, name: str, work) -> None:
    """Run `work` on an interval until asked to stop. One failure never kills the loop."""
    while not stopping.is_set():
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s pass failed; retrying on the next tick", name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=seconds)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    engine = db.create_engine(settings)
    redis = cache.create_client(settings)
    logger.info(
        "worker starting: postgres=%s redis=%s", await db.ping(engine), await cache.ping(redis)
    )

    drainer = drain.Drain(settings, engine, redis)
    await drainer.ensure_group()

    # ADR-0011, before anything else: a Redis that came back empty must not be serving
    # against a zero counter, and the hot path is refusing traffic until this finishes.
    await counters.ensure_authoritative(engine, redis)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    async def aggregate_pass() -> None:
        result = await aggregate.aggregate_dirty(engine, redis)
        if result.cells:
            logger.debug(
                "aggregated %d cells into %d rollup rows", result.cells, result.rows_written
            )

    async def threshold_pass() -> None:
        await thresholds.sweep(
            engine, redis, max_age_seconds=settings.threshold_max_age_seconds
        )

    async def watchdog_pass() -> None:
        await counters.ensure_authoritative(engine, redis)
        # Trim BEFORE checking the bound: an acked entry is finished business, and leaving
        # it in the stream would walk the length into the bound on ordinary traffic.
        await drainer.trim_drained()
        await drainer.check_stream_bound()

    async def close_pass() -> None:
        month = await close.due_month(engine)
        if month is None:
            return
        logger.warning("month %s has ended and is not fully invoiced; closing it", month)
        await close.close_month(settings, engine, redis, drainer, month)

    tasks = [
        asyncio.create_task(drainer.run_forever(stopping), name="drain"),
        asyncio.create_task(
            _every(settings.aggregate_interval_seconds, stopping, "aggregate", aggregate_pass),
            name="aggregate",
        ),
        asyncio.create_task(
            _every(settings.threshold_interval_seconds, stopping, "thresholds", threshold_pass),
            name="thresholds",
        ),
        asyncio.create_task(
            _every(WATCHDOG_SECONDS, stopping, "watchdog", watchdog_pass), name="watchdog"
        ),
        asyncio.create_task(
            _every(HEARTBEAT_SECONDS, stopping, "close", close_pass), name="close"
        ),
    ]

    try:
        await stopping.wait()
    finally:
        logger.info("worker shutting down; in-flight batches stay pending for redelivery")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
