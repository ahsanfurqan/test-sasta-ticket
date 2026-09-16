"""Health and readiness. Unauthenticated by design -- used by docker compose healthchecks."""

from fastapi import APIRouter, Request, Response, status

from meter.storage import cache, db

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness: the process is up. Touches no dependency on purpose."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict:
    """Readiness: dependencies are reachable."""
    checks = {}
    try:
        checks["postgres"] = "ok" if await db.ping(request.app.state.engine) else "failed"
    except Exception:
        checks["postgres"] = "unreachable"
    try:
        checks["redis"] = "ok" if await cache.ping(request.app.state.redis) else "failed"
    except Exception:
        checks["redis"] = "unreachable"

    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not ready", "checks": checks}
