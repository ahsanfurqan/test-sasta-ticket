"""FastAPI application. Owned by hot-path. See docs/adr/0002-fastapi-over-django.md.

Startup does three things before the first request, in this order, and each one is load
bearing:

1. **Wire the hot path.** One Redis client, one engine, one `HotPathContext` the metering
   middleware closes over, so the request path does no framework attribute lookups.
2. **Register the development key as a real key.** `DEV_API_KEY` from `.env` becomes an
   ordinary hashed row belonging to an ordinary customer on an ordinary plan -- so the
   local stack, the load harness and the existing tests all exercise the SAME auth path a
   customer does, rather than a special case that hides bugs. Local environments only.
3. **Check whether the counters are authoritative** (ADR-0011) and say so loudly if they
   are not. Until `pipeline` has rebuilt them from Postgres and set the marker, the
   metering middleware refuses every metered request with a 503 -- a zero counter in an
   unrebuilt keyspace is not evidence that anybody is under their limit. The API only ever
   reads that marker; rebuilding it belongs to `pipeline`, and two writers would make it
   mean nothing.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from meter.api.context import HotPathContext
from meter.api.metering import UsageMeteringMiddleware
from meter.api.routes import account, admin, echo
from meter.api.settings import HotPathSettings
from meter.config import Settings, get_settings
from meter.ops import counters, health
from meter.storage import cache, db
from meter.storage.repositories import keys as keys_repo

logger = logging.getLogger(__name__)

DEV_CUSTOMER_NAME = "Local development"
DEV_KEY_PREFIX = "dev-key"


async def _bootstrap_dev_key(context: HotPathContext, settings: Settings) -> None:
    """Make DEV_API_KEY a real key row. Idempotent, and local environments only.

    This is scaffolding with a purpose: the alternative -- a hardcoded key compared in the
    auth function -- is a second authentication path that nothing else uses and that would
    therefore be the one path never exercised by a real request.
    """
    customer_id = await keys_repo.find_customer_by_name(context.sessions, DEV_CUSTOMER_NAME)
    if customer_id is None:
        from meter.api import provisioning

        provisioned = await provisioning.provision_customer(
            context.sessions,
            name=DEV_CUSTOMER_NAME,
            plan="Starter",
            secret=settings.dev_api_key,
            prefix=DEV_KEY_PREFIX,
            label="DEV_API_KEY from .env",
        )
        logger.info(
            "registered the development key against customer %s (%s)",
            provisioned["customer_id"],
            DEV_CUSTOMER_NAME,
        )
        return

    await keys_repo.issue_key(
        context.sessions,
        customer_id,
        label="DEV_API_KEY from .env",
        secret=settings.dev_api_key,
        prefix=DEV_KEY_PREFIX,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    context: HotPathContext = app.state.hot_path
    app.state.settings = settings
    app.state.engine = db.create_engine(settings)
    app.state.session_factory = db.create_session_factory(app.state.engine)
    app.state.redis = cache.create_client(settings)

    context.redis = app.state.redis
    context.session_factory = app.state.session_factory

    app.state.health_monitor = health.HealthMonitor(app.state.engine, app.state.redis)
    await app.state.health_monitor.start()

    if settings.app_env == "local" and context.hot.bootstrap_dev_key and settings.dev_api_key:
        try:
            await _bootstrap_dev_key(context, settings)
        except Exception:
            logger.exception("could not register the development key; local auth will fail")

    try:
        if await counters.is_authoritative(app.state.redis):
            logger.info("counters are marked authoritative: metered traffic will be served")
        else:
            logger.error(
                "counters are NOT marked authoritative: every metered request will be "
                "refused with 503 until pipeline rebuilds them from Postgres (ADR-0011). "
                "This is the fail-closed path working, and it is also an outage."
            )
    except Exception:
        logger.exception("could not read the counter authority marker")

    logger.info(
        "api ready: capture=%s auth cache ttl=%ss stream=%s bound=%s",
        context.hot.capture_enabled,
        context.hot.auth_cache_ttl_seconds,
        settings.usage_stream_key,
        settings.usage_stream_max_entries,
    )

    try:
        yield
    finally:
        await app.state.health_monitor.stop()
        await app.state.engine.dispose()
        await app.state.redis.aclose()
        logger.info("api shutdown: connections closed")


def create_app() -> FastAPI:
    settings = get_settings()
    context = HotPathContext(settings=settings, hot=HotPathSettings.from_env())

    app = FastAPI(
        title="Metered Billing API",
        version="0.2.0",
        description=(
            "Authenticated serving with usage captured before the response is sent "
            "(ADR-0018) and spending limits enforced against a precomputed request-count "
            "threshold (ADR-0008). See CLAUDE.md and docs/adr/."
        ),
        lifespan=lifespan,
    )
    app.state.hot_path = context

    # The outermost user middleware, so it sees the status of everything below it --
    # including a 404 for a mistyped /v1 path, which ADR-0007 bills.
    app.add_middleware(UsageMeteringMiddleware, context=context)

    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(echo.router)
    app.include_router(account.router)
    return app


app = create_app()
