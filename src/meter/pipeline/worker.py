"""Worker entrypoint. Owned by pipeline.

Session 1: the worker exists, connects to Postgres and Redis, and idles. That proves the
container, the network and the credentials work before there is any real work to schedule.

Next session it owns: draining buffered usage from Redis into Postgres, aggregation,
reconciliation (proving the Redis counter and the Postgres truth differ by a known delta),
month close, and the invoice job. Every one of those must be idempotent and replayable --
the tests will kill this process mid-flight.
"""

import asyncio
import logging
import signal

from meter.config import get_settings
from meter.storage import cache, db

logger = logging.getLogger("meter.worker")

HEARTBEAT_SECONDS = 30


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    engine = db.create_engine(settings)
    redis = cache.create_client(settings)
    logger.info(
        "worker ready: postgres=%s redis=%s", await db.ping(engine), await cache.ping(redis)
    )

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    try:
        while not stopping.is_set():
            # No work yet, by design. A real loop drains a buffer and commits a batch --
            # and must be safe to kill at any point between those two things.
            logger.info("worker heartbeat: idle, no pipeline stages implemented yet")
            try:
                await asyncio.wait_for(stopping.wait(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                pass
    finally:
        logger.info("worker draining and shutting down")
        await engine.dispose()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
