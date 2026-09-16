"""GET /v1/echo -- the billable endpoint.

What it returns is deliberately trivial. None of the interesting work is here: it is in
what happens around the handler, in `meter.api.metering`, where the request is
authenticated from cache, gated against a precomputed threshold, and captured into Redis
before the response is allowed out.

The handler itself touches nothing. It used to ping Postgres and Redis to prove the
containers were wired together; that was session-1 scaffolding and a synchronous Postgres
call has no business on a path with a 1ms budget (ADR-0014, hot-path invariant #1). The
`dependencies` field survives because it is part of this endpoint's published shape, but it
now reports the background health monitor's last observation rather than probing per
request -- a cached fact, not a round trip.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from meter.api.auth import Caller, require_api_key

router = APIRouter(prefix="/v1", tags=["echo"])

Authenticated = Annotated[Caller, Depends(require_api_key)]


@router.get("/echo")
async def echo(request: Request, caller: Authenticated, message: str = "hello") -> dict:
    monitor = request.app.state.health_monitor
    return {
        "message": message,
        "customer_id": caller.customer_id,
        "api_key_id": caller.api_key_id,
        "served_at": datetime.now(UTC).isoformat(),
        "dependencies": monitor.snapshot.as_dict(),
        "billable": True,
    }
