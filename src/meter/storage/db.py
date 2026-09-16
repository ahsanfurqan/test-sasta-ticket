"""Database engine and session factory. Owned by data-model.

One async engine per process, one session per unit of work. Async sessions are not safe
to share across tasks, so nothing here hands out a module-level session.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from meter.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_min,
        max_overflow=settings.db_pool_max - settings.db_pool_min,
        pool_pre_ping=True,
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: otherwise a commit re-queries on the next attribute
    # access, which is a silent round trip on a path with a latency budget.
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine) -> bool:
    """Liveness check. Not a pattern for the request path -- see meter.api.routes.echo."""
    async with engine.connect() as connection:
        return await connection.scalar(text("SELECT 1")) == 1
