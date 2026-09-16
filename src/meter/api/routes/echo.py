"""GET /v1/echo -- the only endpoint.

Its job is to prove the container talks to Postgres and Redis. That is ALL it is for.

WARNING: the dependency pings below are scaffolding, not a pattern. A real endpoint does
no synchronous Postgres call on the request path (hot-path invariant #1). Do not carry
these checks into anything customers actually use.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from meter.api.auth import Caller, require_api_key
from meter.storage import cache, db

router = APIRouter(prefix="/v1", tags=["echo"])

Authenticated = Annotated[Caller, Depends(require_api_key)]


@router.get("/echo")
async def echo(
    request: Request,
    caller: Authenticated,
    message: str = "hello",
) -> dict:
    postgres_ok = await db.ping(request.app.state.engine)
    redis_ok = await cache.ping(request.app.state.redis)

    return {
        "message": message,
        "customer_id": caller.customer_id,
        "served_at": datetime.now(UTC).isoformat(),
        "dependencies": {
            "postgres": "ok" if postgres_ok else "unreachable",
            "redis": "ok" if redis_ok else "unreachable",
        },
        "note": "scaffolding only -- this endpoint is not billed and records no usage",
    }
