"""What the hot path needs to serve one request, in one object.

The middleware takes this rather than reaching into `app.state`, for two reasons: the
request path should not be doing attribute lookups through a framework object, and every
failure branch in `meter.api.metering` can then be tested against a fake Redis in-process,
with no container, no network and no timing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meter.api.settings import HotPathSettings
from meter.config import Settings


@dataclass
class HotPathContext:
    settings: Settings
    hot: HotPathSettings = field(default_factory=HotPathSettings)
    redis: aioredis.Redis | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def cache(self) -> aioredis.Redis:
        if self.redis is None:  # pragma: no cover - a wiring bug, not a runtime state
            raise RuntimeError("hot path used before the Redis client was created")
        return self.redis

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self.session_factory is None:  # pragma: no cover - wiring bug
            raise RuntimeError("hot path used before the session factory was created")
        return self.session_factory
