"""Usage capture and spending-limit enforcement, as ASGI middleware. Owned by hot-path.

This module is ADR-0018 made executable, and the ordering inside `__call__` is the point of
it. Read it as a sequence:

    authenticate  ->  gate on the limit  ->  run the handler  ->  status known
                  ->  XADD the usage event  ->  release the response

The capture sits between the handler finishing and the response leaving, because that is
what makes "no acknowledged request goes unbilled" true rather than aspirational. The
`http.response.start` message -- status line and headers -- is held until the XADD has
returned. A process killed one instruction earlier also died before the customer received
an answer, so their retry *is* the request. There is no state in which we answered and did
not record.

Two consequences of that ordering, both deliberate:

  * **No in-process batching.** One XADD per request (ADR-0018 §2). Batching is the obvious
    way to spend less of ADR-0014's 1ms budget, and it would reopen exactly the window this
    ordering closes: the loss becomes the whole unflushed batch. Batching belongs in the
    drain, where a crash is recoverable. If you are here to add a buffer, read ADR-0018.
  * **A failed capture refuses the response.** If Redis will not take the event, the
    customer gets a 503 instead of the answer we computed. We did the work and will not be
    paid for it; that is cheaper than serving traffic we cannot record (ADR-0011).

Round trips per served request, which is the whole latency story:

    1. pipelined GET auth / GET authoritative-marker / XLEN stream   (before the handler)
    2. MGET counter, threshold                                        (before the handler)
    3. pipelined XADD event / INCR counter                            (after the handler)

Three, none of them Postgres, none of them a rating calculation.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from redis.exceptions import RedisError

from meter.api import auth
from meter.api.context import HotPathContext
from meter.storage.repositories import keys as keys_repo
from meter.storage.repositories import usage as usage_repo

logger = logging.getLogger(__name__)

#: Everything under /v1 is customer-facing and metered. /healthz, /readyz and /admin are
#: not: health checks are not billable traffic and provisioning is not a product surface.
METERED_PREFIX = "/v1/"

#: Authenticated but NOT billable. ADR-0007 defines a billable request as one we
#: authenticated and processed, which read literally would charge a customer for asking what
#: they owe. That is the same objection that ruled out billing a request refused for hitting
#: a spending limit: being told your own balance is not a thing to be charged for. These
#: still authenticate, still enforce, and still count toward nothing.
UNBILLED_PATHS = ("/v1/usage", "/v1/invoices")


def _is_billable_path(path: str) -> bool:
    return not any(path == p or path.startswith(p + "/") for p in UNBILLED_PATHS)

# ---------------------------------------------------------------------------------------
# ADR-0007: what counts. The status-code list is deliberate and maintained, not a category.
# ---------------------------------------------------------------------------------------

SUCCESS = "success"
CLIENT_ERROR = "client_error"
UNAUTHENTICATED = "unauthenticated"
SERVER_ERROR = "server_error"
LIMIT_REFUSED = "limit_refused"
#: Not a `usage_outcome` value: no route redirects, and a redirect is neither work we did
#: for the customer nor a failure. Counted, never billed, never streamed.
REDIRECT = "redirect"


@dataclass(frozen=True, slots=True)
class Outcome:
    name: str
    billable: bool


_SUCCESS = Outcome(SUCCESS, True)
_CLIENT_ERROR = Outcome(CLIENT_ERROR, True)
_UNAUTHENTICATED = Outcome(UNAUTHENTICATED, False)
_SERVER_ERROR = Outcome(SERVER_ERROR, False)
_LIMIT_REFUSED = Outcome(LIMIT_REFUSED, False)
_REDIRECT = Outcome(REDIRECT, False)


def classify(status_code: int) -> Outcome:
    """The billability rule from ADR-0007, and nothing else.

    2xx and client 4xx are billable: we authenticated, routed and processed them, and the
    work was done. 401/403 are not -- billing an unauthenticated request is an attack
    vector. Our 5xx are not -- we do not charge for our own faults. A limit refusal is not
    -- charging to be told "no" is indefensible.
    """
    if status_code in (401, 403):
        return _UNAUTHENTICATED
    if status_code == 402:
        return _LIMIT_REFUSED
    if status_code >= 500:
        return _SERVER_ERROR
    if status_code >= 400:
        return _CLIENT_ERROR
    if status_code >= 300:
        return _REDIRECT
    return _SUCCESS


# ---------------------------------------------------------------------------------------
# Refusals. Written by hand rather than raised, because a refusal must not reach a handler.
# ---------------------------------------------------------------------------------------

_JSON = (b"content-type", b"application/json")


async def _respond(send, status_code: int, body: dict, extra_headers: list | None = None) -> None:
    payload = json.dumps(body).encode()
    headers = [_JSON, (b"content-length", str(len(payload)).encode())]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": payload, "more_body": False})


class UsageMeteringMiddleware:
    """Authenticate, enforce the limit, serve, capture -- in that order.

    Takes its dependencies as a `HotPathContext` rather than reading `app.state`, so every
    branch below (Redis unreachable, keyspace not authoritative, stream at its bound,
    counter over threshold, capture failing after a successful handler) is reachable in a
    test with a fake Redis and no container.
    """

    def __init__(self, app, context: HotPathContext) -> None:
        self.app = app
        self.context = context

    # -- helpers ------------------------------------------------------------------------

    def _retry_after(self) -> list[tuple[bytes, bytes]]:
        return [(b"retry-after", str(self.context.hot.retry_after_seconds).encode())]

    async def _unavailable(self, send, reason: str) -> None:
        """ADR-0011: fail closed. We can neither count nor enforce, so we do not serve."""
        await _respond(
            send,
            503,
            {
                "detail": "usage metering is unavailable, so the request was not served",
                "reason": reason,
                "billed": False,
            },
            self._retry_after(),
        )

    async def _unauthorized(self, send) -> None:
        await _respond(
            send,
            401,
            {"detail": "invalid or missing API key", "billed": False},
            [(b"www-authenticate", auth.API_KEY_HEADER.encode())],
        )

    async def _count_nonbillable(self, period_label: str, customer_id: str, outcome: str) -> None:
        """Record an outcome nobody pays for, so it is visible somewhere.

        Redis failures here are swallowed: this moves no counter anyone is billed from, and
        turning a 500 into a 503 because an observability write failed would lose the more
        useful of the two answers.
        """
        try:
            await self.context.cache.hincrby(
                usage_repo.nonbillable_key(period_label), f"{customer_id}:{outcome}", 1
            )
        except RedisError:
            logger.warning("could not record non-billable outcome %s", outcome)

    async def _capture(
        self,
        *,
        caller: auth.Caller,
        period: usage_repo.Period,
        occurred_at: datetime,
        event_id: str,
        status_code: int,
        outcome: Outcome,
    ) -> None:
        """One XADD and one INCR, in one round trip, before the response is released.

        The idempotency key is minted with the event and travels with it, so a redelivered
        batch in the drain resolves to the same row (`uq_usage_events_idempotency`). It is
        never minted per attempt -- that would make every retry a new request.
        """
        pipe = self.context.cache.pipeline(transaction=False)
        pipe.xadd(
            self.context.settings.usage_stream_key,
            {
                "idempotency_key": event_id,
                "customer_id": caller.customer_id,
                "api_key_id": caller.api_key_id,
                "occurred_at": occurred_at.isoformat(),
                "billing_period_start": period.start.isoformat(),
                "status_code": str(status_code),
                "outcome": outcome.name,
                "billable": "1",
            },
        )
        pipe.incr(usage_repo.billable_counter_key(caller.customer_id, period.label))
        await pipe.execute()

    # -- the request path ---------------------------------------------------------------

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(METERED_PREFIX):
            await self.app(scope, receive, send)
            return

        context = self.context
        presented = auth.api_key_from_headers(scope["headers"])
        if presented is None:
            await self._unauthorized(send)
            return

        digest = keys_repo.hash_key(presented)
        now = datetime.now(UTC)
        period = usage_repo.period_for(now)
        metering = context.hot.capture_enabled

        # Round trip 1. The auth entry is customer-independent, and so are the two
        # fail-closed checks, so all three are one pipeline rather than three calls.
        try:
            if metering:
                pipe = context.cache.pipeline(transaction=False)
                pipe.get(keys_repo.auth_cache_key(digest))
                pipe.get(usage_repo.COUNTERS_AUTHORITATIVE)
                pipe.xlen(context.settings.usage_stream_key)
                cached, marker, stream_depth = await pipe.execute()
            else:
                cached = await context.cache.get(keys_repo.auth_cache_key(digest))
                marker, stream_depth = "capture-disabled", 0
        except RedisError as exc:
            logger.warning("redis unreachable on the request path: %s", type(exc).__name__)
            await self._unavailable(send, "redis_unreachable")
            return

        try:
            caller = await auth.resolve(context, presented, digest, cached)
        except RedisError as exc:
            logger.warning("redis unreachable caching auth: %s", type(exc).__name__)
            await self._unavailable(send, "redis_unreachable")
            return
        except auth.PostgresUnavailable as exc:
            # Postgres is down AND this key had no positive entry to serve stale (ADR-0019):
            # either it was never cached, or its stale ceiling has passed, or it is a
            # negative entry -- which never goes stale, because an outage must not turn
            # "no such key" into "maybe". A key that WAS cached keeps working; new keys and
            # new customers cannot authenticate until the directory returns.
            logger.warning("cannot resolve an uncached key: %s", exc)
            await self._unavailable(send, "key_directory_unavailable")
            return

        if caller is None:
            await self._unauthorized(send)
            return

        if metering:
            # ADR-0011: reachable-but-empty is not the same as "no usage yet". Until a
            # rebuild has marked the counters authoritative, a zero counter is not evidence
            # that a customer is under their limit, so we refuse rather than serve.
            if marker is None:
                await self._unavailable(send, "counters_not_authoritative")
                return

            # ADR-0018: the stream is explicitly bounded. Past the bound we are buffering
            # usage we may never be able to drain, which is serving unrecordable traffic.
            if stream_depth > context.settings.usage_stream_max_entries:
                logger.error("usage stream at its bound (%s entries): failing closed", stream_depth)
                await self._unavailable(send, "usage_buffer_full")
                return

            # Round trip 2. ADR-0008: two integers. No ladder, no price list, no Postgres,
            # and nothing from meter.domain.rating anywhere near this comparison.
            try:
                counter_raw, threshold_raw = await context.cache.mget(
                    usage_repo.billable_counter_key(caller.customer_id, period.label),
                    usage_repo.threshold_key(caller.customer_id, period.label),
                )
            except RedisError as exc:
                logger.warning("redis unreachable reading counters: %s", type(exc).__name__)
                await self._unavailable(send, "redis_unreachable")
                return

            # A spending limit caps what a customer SPENDS. The UNBILLED_PATHS cannot
            # move that number -- they are excluded from billing for exactly that reason
            # -- so refusing them enforces a cap against a request that can never reach
            # it, and blinds the customer to the limit that just stopped them at the one
            # moment they need to see it. Echo is refused; "what do I owe?" is not.
            if (
                threshold_raw is not None
                and _is_billable_path(scope["path"])
                and int(counter_raw or 0) >= int(threshold_raw)
            ):
                await self._count_nonbillable(period.label, caller.customer_id, LIMIT_REFUSED)
                await _respond(
                    send,
                    402,
                    {
                        "detail": (
                            "spending limit reached for this billing period; no further "
                            "requests will be served until the limit is raised or the "
                            "period rolls over"
                        ),
                        "billed": False,
                        "period": period.label,
                        "requests_served": int(counter_raw or 0),
                        "request_threshold": int(threshold_raw),
                    },
                )
                return

        scope.setdefault("state", {})["caller"] = caller

        event_id = uuid.uuid4().hex  # minted WITH the event, never per attempt
        state = {"captured": False, "replaced": False}

        async def send_wrapper(message) -> None:
            if state["replaced"]:
                return  # we substituted a refusal; the handler's body is discarded

            if message["type"] != "http.response.start":
                await send(message)
                return

            status_code = message["status"]
            outcome = classify(status_code)

            if not metering:
                await send(message)
                return

            started = perf_counter()
            # An account endpoint is authenticated and enforced like any other, but never
            # billed: a customer is not charged for asking what they owe.
            if outcome.billable and _is_billable_path(scope["path"]):
                try:
                    await self._capture(
                        caller=caller,
                        period=period,
                        occurred_at=now,
                        event_id=event_id,
                        status_code=status_code,
                        outcome=outcome,
                    )
                except RedisError as exc:
                    # THE ordering guarantee: nothing has been sent yet, so we can still
                    # refuse. Serving here would mean an answered, unrecorded request.
                    logger.error("usage capture failed, refusing the response: %s", exc)
                    state["replaced"] = True
                    await self._unavailable(send, "usage_capture_failed")
                    return
                state["captured"] = True
            else:
                await self._count_nonbillable(period.label, caller.customer_id, outcome.name)

            micros = int((perf_counter() - started) * 1_000_000)
            message["headers"] = [
                *message["headers"],
                (b"x-request-id", event_id.encode()),
                (b"x-usage-capture-us", str(micros).encode()),
                (b"x-usage-billable", b"1" if outcome.billable else b"0"),
            ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The handler blew up before any response was started. That is our fault, so
            # it is not billable (ADR-0007) -- but it is counted, and then re-raised so the
            # server error handler above us produces the 500.
            if metering and not state["captured"]:
                await self._count_nonbillable(period.label, caller.customer_id, SERVER_ERROR)
            raise
