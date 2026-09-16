"""Health and readiness. Unauthenticated by design -- used by docker compose healthchecks.

Also home to the background dependency monitor. The distinction it exists to enforce:

  * `/readyz` is asked rarely, by an orchestrator, and may pay for a live probe.
  * A customer request is asked constantly and may not. Anything a served request reports
    about a dependency is a cached observation from the monitor below, never a probe.

That is the whole reason this class exists: without it, "tell the caller whether Postgres is
up" turns into a Postgres round trip per request, which is exactly the scaffolding
ADR-0014's budget and hot-path invariant #1 forbid.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine

from meter.ops import counters
from meter.storage import cache, db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

DEFAULT_PROBE_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class DependencySnapshot:
    postgres: str
    redis: str
    observed_at: datetime | None

    def as_dict(self) -> dict:
        return {"postgres": self.postgres, "redis": self.redis}


class HealthMonitor:
    """Probes Postgres and Redis on a timer and remembers the answer."""

    def __init__(
        self,
        engine: AsyncEngine,
        redis: aioredis.Redis,
        interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
    ) -> None:
        self._engine = engine
        self._redis = redis
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self.snapshot = DependencySnapshot(postgres="unknown", redis="unknown", observed_at=None)

    async def probe(self) -> DependencySnapshot:
        try:
            postgres = "ok" if await db.ping(self._engine) else "failed"
        except Exception:
            postgres = "unreachable"
        try:
            redis_state = "ok" if await cache.ping(self._redis) else "failed"
        except Exception:
            redis_state = "unreachable"
        self.snapshot = DependencySnapshot(
            postgres=postgres, redis=redis_state, observed_at=datetime.now(UTC)
        )
        return self.snapshot

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.probe()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - the probe already swallows its own
                logger.exception("health probe failed")

    async def start(self) -> None:
        await self.probe()
        self._task = asyncio.create_task(self._loop(), name="health-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness: the process is up. Touches no dependency on purpose."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict:
    """Readiness: dependencies are reachable, and the counters can be trusted.

    `counters` is reported alongside `checks` rather than inside it, because it is not a
    dependency probe: Redis can be perfectly healthy while its keyspace is not yet
    authoritative, and that state refuses traffic (ADR-0011) without either dependency
    being down.
    """
    checks = {}
    try:
        checks["postgres"] = "ok" if await db.ping(request.app.state.engine) else "failed"
    except Exception:
        checks["postgres"] = "unreachable"
    try:
        checks["redis"] = "ok" if await cache.ping(request.app.state.redis) else "failed"
    except Exception:
        checks["redis"] = "unreachable"

    marker = None
    if checks["redis"] == "ok":
        try:
            marker = await counters.marker(request.app.state.redis)
        except Exception:
            marker = None

    ready = all(value == "ok" for value in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "not ready",
        "checks": checks,
        "counters": (
            "authoritative"
            if marker is not None
            else "not authoritative -- metered requests are being refused (ADR-0011)"
        ),
    }
