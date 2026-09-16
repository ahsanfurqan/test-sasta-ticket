"""Provisioning endpoints. ADMIN / DEMO SURFACE -- deliberately not a customer product.

Everything here is the operator side of the system: create a customer on a plan, issue and
revoke keys, set a spending limit, inspect the two integers enforcement compares. A real
deployment replaces this with a signup flow and a billing console, each with its own
authorisation and audit trail. These exist so the whole system can be exercised end to end
without hand-written SQL.

They are not metered: `meter.api.metering` meters `/v1` only. Provisioning is not billable
traffic, and a customer who could reach these could raise their own spending limit.

Authorisation is intentionally crude and intentionally loud: an `ADMIN_TOKEN` if one is
set, and otherwise local environments only.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from meter.api import provisioning
from meter.api.context import HotPathContext
from meter.storage.repositories import keys as keys_repo
from meter.storage.repositories import usage as usage_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin (demo surface)"])

ADMIN_TOKEN_HEADER = "X-Admin-Token"


def hot_path(request: Request) -> HotPathContext:
    return request.app.state.hot_path


HotPath = Annotated[HotPathContext, Depends(hot_path)]


async def require_admin(request: Request) -> None:
    context: HotPathContext = request.app.state.hot_path
    expected = context.hot.admin_token
    if expected:
        presented = request.headers.get(ADMIN_TOKEN_HEADER, "")
        if not secrets.compare_digest(presented, expected):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="admin token required"
            )
        return
    if context.settings.app_env != "local":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "provisioning is disabled outside a local environment unless ADMIN_TOKEN "
                "is configured"
            ),
        )


Admin = Depends(require_admin)


class CreateCustomer(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    plan: str = Field(default="Starter", description="Starter, Growth or Scale")
    key_label: str | None = None


class IssueKey(BaseModel):
    label: str | None = None


class ChangePlan(BaseModel):
    plan: str


class SetSpendingLimit(BaseModel):
    limit_paisa: int = Field(gt=0, description="Money is integer paisa. Rs. 50,000 is 5_000_000.")


@router.post("/customers", status_code=status.HTTP_201_CREATED, dependencies=[Admin])
async def create_customer(body: CreateCustomer, context: HotPath) -> dict:
    """Create a customer on a plan and issue their first API key.

    The key is in the response and is never retrievable again (ADR-0015). Losing it means
    issuing another one.
    """
    try:
        provisioned = await provisioning.provision_customer(
            context.sessions, name=body.name, plan=body.plan, label=body.key_label
        )
    except provisioning.ProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    provisioned["warning"] = "the api_key is shown once and is not recoverable"
    return provisioned


@router.post(
    "/customers/{customer_id}/keys",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Admin],
)
async def issue_key(customer_id: str, body: IssueKey, context: HotPath) -> dict:
    """Issue an additional key. Multiple live keys per customer is what makes rotation
    something a customer will actually do (ADR-0015)."""
    if not await keys_repo.customer_exists(context.sessions, customer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such customer")
    issued = await keys_repo.issue_key(context.sessions, customer_id, label=body.label)
    return {
        "customer_id": customer_id,
        "api_key": issued.secret,
        "api_key_id": issued.key_id,
        "api_key_prefix": issued.prefix,
        "warning": "the api_key is shown once and is not recoverable",
    }


@router.get("/customers/{customer_id}/keys", dependencies=[Admin])
async def list_keys(customer_id: str, context: HotPath) -> dict:
    keys = await keys_repo.list_keys(context.sessions, customer_id)
    return {"customer_id": customer_id, "keys": keys}


@router.delete("/keys/{key_id}", dependencies=[Admin])
async def revoke_key(key_id: str, context: HotPath) -> dict:
    """Revoke a key. Effective within the auth cache TTL, which is the stated window."""
    revoked = await keys_repo.revoke_key(context.sessions, key_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such live key"
        )
    return {
        "key_id": key_id,
        "revoked": True,
        "effective_within_seconds": context.hot.auth_cache_ttl_seconds,
        "note": (
            "usage served by this key before revocation is still billed: the charge "
            "reflects what we served (ADR-0015)"
        ),
    }


@router.post("/customers/{customer_id}/plan", dependencies=[Admin])
async def change_plan(customer_id: str, body: ChangePlan, context: HotPath) -> dict:
    """Move a customer to a different plan, effective now.

    The brief's definition of done walks through exactly this: a Growth customer moved to
    Scale partway through the month, then invoiced. ADR-0006 prorates the fee, the included
    allowance and the band widths for each segment, and gives the change day to the new plan.
    """
    if body.plan not in provisioning.PLANS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown plan {body.plan!r}; one of {sorted(provisioning.PLANS)}",
        )

    at = datetime.now(UTC)
    version_id = await provisioning.ensure_price_list_version(
        context.sessions, provisioning.PLANS[body.plan]
    )
    closed, opened = await provisioning.change_plan(
        context.sessions, customer_id, version_id, at
    )
    return {
        "customer_id": customer_id,
        "plan": body.plan,
        "effective_from": at.isoformat(),
        "closed_assignment_id": closed,
        "new_assignment_id": opened,
        "note": (
            "the period now has two segments; any spending-limit threshold is recomputed "
            "across both by the pipeline, never guessed from one (ADR-0008)"
        ),
    }


@router.put("/customers/{customer_id}/spending-limit", dependencies=[Admin])
async def set_spending_limit(customer_id: str, body: SetSpendingLimit, context: HotPath) -> dict:
    """Set the limit and invert it into a request-count threshold, now rather than later."""
    if not await keys_repo.customer_exists(context.sessions, customer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such customer")
    try:
        return await provisioning.set_spending_limit(
            context.sessions, context.cache, customer_id=customer_id, limit_paisa=body.limit_paisa
        )
    except provisioning.ProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("/customers/{customer_id}/usage", dependencies=[Admin])
async def usage(customer_id: str, context: HotPath) -> dict:
    """The two integers the hot path compares, exactly as it sees them.

    Not the live usage/cost endpoint the brief asks for -- that one rates the counter and
    belongs with the pipeline. This is the enforcement state, for operators and tests.
    """
    period = usage_repo.current_period()
    counter, threshold = await context.cache.mget(
        usage_repo.billable_counter_key(customer_id, period.label),
        usage_repo.threshold_key(customer_id, period.label),
    )
    nonbillable = await context.cache.hgetall(usage_repo.nonbillable_key(period.label))
    return {
        "customer_id": customer_id,
        "billing_period": period.label,
        "billable_requests": int(counter or 0),
        "request_threshold": int(threshold) if threshold is not None else None,
        "over_limit": threshold is not None and int(counter or 0) >= int(threshold),
        "non_billable": {
            outcome.split(":", 1)[1]: int(count)
            for outcome, count in nonbillable.items()
            if outcome.startswith(f"{customer_id}:")
        },
    }
