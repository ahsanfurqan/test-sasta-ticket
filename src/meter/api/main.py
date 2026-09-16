"""FastAPI application. Owned by hot-path. See docs/adr/0002-fastapi-over-django.md."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from meter.api.routes import echo
from meter.config import get_settings
from meter.ops import health
from meter.storage import cache, db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app.state.settings = settings
    app.state.engine = db.create_engine(settings)
    app.state.session_factory = db.create_session_factory(app.state.engine)
    app.state.redis = cache.create_client(settings)
    logger.info("api ready: database engine and redis client established")

    try:
        yield
    finally:
        await app.state.engine.dispose()
        await app.state.redis.aclose()
        logger.info("api shutdown: connections closed")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Metered Billing API",
        version="0.1.0",
        description=(
            "Session-1 skeleton. One endpoint, no billing logic. "
            "See CLAUDE.md and docs/adr/."
        ),
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(echo.router)
    return app


app = create_app()
