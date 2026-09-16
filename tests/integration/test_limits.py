"""Spending limits and failing closed: the two integers, and every way we refuse.

ADR-0008 says the hot path compares a counter with a precomputed threshold and does nothing
else, so the first thing these tests assert is what is NOT happening: no Postgres call, no
rating, no price list. The fake session factory raises if touched, and `meter.api.metering`
importing `meter.domain.rating` would fail the import test below.

ADR-0011 says we fail closed, and names the case that matters most -- a Redis that comes
back healthy but EMPTY must not serve one request against a zero counter. That is the
difference between "Redis is unreachable" and "Redis is reachable and lying", and both are
tested here.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import httpx
import pytest
import redis as sync_redis

from meter.api.metering import UsageMeteringMiddleware
from meter.config import get_settings
from meter.storage.repositories import usage as usage_repo
from tests.integration.test_capture import (
    CUSTOMER,
    FakeRedis,
    app_returning,
    authorised_store,
    body_of,
    call,
    header_of,
    make_context,
    status_of,
)

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")


def _period() -> str:
    return usage_repo.current_period().label


def counter_key(customer_id: str = CUSTOMER) -> str:
    return usage_repo.billable_counter_key(customer_id, _period())


def threshold_key(customer_id: str = CUSTOMER) -> str:
    return usage_repo.threshold_key(customer_id, _period())


@pytest.fixture()
def redis_client():
    client = sync_redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    client.close()


@pytest.fixture(scope="module")
def customer() -> dict:
    response = httpx.post(
        f"{BASE_URL}/admin/customers",
        json={"name": f"limit-tests-{os.getpid()}-{time.time()}", "plan": "Starter"},
        timeout=15,
    )
    assert response.status_code == 201, response.text
    return response.json()


def usage_of(customer_id: str) -> dict:
    return httpx.get(f"{BASE_URL}/admin/customers/{customer_id}/usage", timeout=15).json()


# ---------------------------------------------------------------------------------------
# ADR-0008: two integers, and nothing else
# ---------------------------------------------------------------------------------------


def test_the_request_path_does_not_import_the_rating_ladder():
    """Invariant #2, as a test rather than as a promise. `meter.api.metering` may not pull
    in the price-list math, directly or through anything it imports."""
    import importlib

    for module in [name for name in list(sys.modules) if name.startswith("meter.api")]:
        del sys.modules[module]
    sys.modules.pop("meter.domain.rating", None)

    importlib.import_module("meter.api.metering")

    assert "meter.domain.rating" not in sys.modules, (
        "deciding whether a customer is over their limit must not compute what they owe"
    )


async def test_under_the_threshold_is_served_and_at_the_threshold_is_refused():
    store = authorised_store(**{counter_key(): "9", threshold_key(): "10"})
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(FakeRedis(store)))
    redis = middleware.context.cache

    assert status_of(await call(middleware)) == 200
    assert redis.store[counter_key()] == "10"

    sent = await call(middleware)
    assert status_of(sent) == 402
    assert redis.store[counter_key()] == "10", "a refusal must not move the counter"


async def test_a_refused_request_is_not_billed_and_says_so():
    store = authorised_store(**{counter_key(): "10", threshold_key(): "10"})
    redis = FakeRedis(store)
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    sent = await call(middleware)
    body = body_of(sent)

    assert status_of(sent) == 402
    assert body["billed"] is False
    assert body["requests_served"] == 10
    assert body["request_threshold"] == 10
    assert redis.stream == [], "charging to be told 'no' is indefensible (ADR-0007)"
    assert redis.hashes[usage_repo.nonbillable_key(_period())][f"{CUSTOMER}:limit_refused"] == 1


async def test_no_threshold_means_no_limit():
    redis = FakeRedis(authorised_store(**{counter_key(): "1000000"}))
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    assert status_of(await call(middleware)) == 200


# ---------------------------------------------------------------------------------------
# ADR-0011: failing closed, and the two different ways Redis can be useless
# ---------------------------------------------------------------------------------------


async def test_redis_unreachable_is_a_503_with_retry_after():
    redis = FakeRedis(authorised_store(), fail_on={"get"})
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    sent = await call(middleware)

    assert status_of(sent) == 503
    assert header_of(sent, b"retry-after") == "1"
    assert body_of(sent)["reason"] == "redis_unreachable"
    assert redis.stream == []


async def test_a_reachable_but_unrebuilt_redis_serves_nothing():
    """The case ADR-0011 calls worse than being down: every counter reads zero, every limit
    looks unreached, and a naive implementation happily serves the whole month again."""
    store = authorised_store()
    del store[usage_repo.COUNTERS_AUTHORITATIVE]
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(FakeRedis(store)))

    sent = await call(middleware)

    assert status_of(sent) == 503
    assert body_of(sent)["reason"] == "counters_not_authoritative"
    assert header_of(sent, b"retry-after") == "1"


async def test_an_empty_keyspace_does_not_serve_against_a_zero_counter():
    """The same thing said the other way round: a customer who is over their limit in the
    system of record, and a Redis that has lost the counter and the threshold along with the
    marker. Nothing in the keyspace says "stop", and that is exactly why we stop.

    Auth still resolves -- the key directory is in Postgres, which is fine -- so this is not
    a request that would have been refused anyway.
    """
    store = authorised_store()
    del store[usage_repo.COUNTERS_AUTHORITATIVE]
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(FakeRedis(store)))

    sent = await call(middleware)
    assert status_of(sent) == 503
    assert body_of(sent)["billed"] is False


async def test_the_stream_bound_fails_closed_rather_than_dropping_usage():
    """ADR-0018: past the bound we are buffering usage we may never drain, so we stop."""
    redis = FakeRedis(authorised_store())
    context = make_context(redis)
    context.settings = context.settings.model_copy(update={"usage_stream_max_entries": 2})
    middleware = UsageMeteringMiddleware(app_returning(200), context)

    for _ in range(3):
        assert status_of(await call(middleware)) == 200

    sent = await call(middleware)
    assert status_of(sent) == 503
    assert body_of(sent)["reason"] == "usage_buffer_full"
    assert len(redis.stream) == 3, "nothing is trimmed away: the entries are money"


# ---------------------------------------------------------------------------------------
# Against the running stack
# ---------------------------------------------------------------------------------------


def test_setting_a_limit_inverts_it_into_a_request_count(base_url, customer):
    """Starter includes 10,000 requests a month at Rs. 0 and charges Rs. 0.80 after that.

    Signing up mid-month prorates the allowance by whole days, rounded UP -- the customer
    wins the fraction (ADR-0006/0017) -- so the threshold is that allowance plus whatever
    the limit buys at 80 paisa each.
    """
    response = httpx.put(
        f"{base_url}/admin/customers/{customer['customer_id']}/spending-limit",
        json={"limit_paisa": 1000},
        timeout=15,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    expected_allowance = -(-10_000 * body["days_in_period"] // body["days_in_month"])
    assert body["prorated_included_requests"] == expected_allowance
    assert body["request_threshold"] == expected_allowance + 1000 // 80
    assert body["prorated_fee_paisa"] == 0


def test_a_limit_below_the_monthly_fee_is_rejected_when_it_is_set(base_url):
    """ADR-0012: the limit caps the TOTAL bill, fee included, so a limit the fee alone
    exceeds can never be honoured. It is refused with the fee named, not accepted and
    silently turned into a threshold of zero."""
    growth = httpx.post(
        f"{base_url}/admin/customers",
        json={"name": f"limit-below-fee-{time.time()}", "plan": "Growth"},
        timeout=15,
    ).json()

    response = httpx.put(
        f"{base_url}/admin/customers/{growth['customer_id']}/spending-limit",
        json={"limit_paisa": 100},
        timeout=15,
    )

    assert response.status_code == 422
    assert "monthly fee" in response.json()["detail"]


def test_crossing_the_threshold_returns_402_and_stops_counting(base_url, redis_client):
    """The end-to-end refusal, with the threshold published exactly as `pipeline` publishes
    it: one integer at one key. Set here directly so the test does not depend on the
    threshold sweep having run."""
    created = httpx.post(
        f"{base_url}/admin/customers",
        json={"name": f"crossing-{time.time()}", "plan": "Starter"},
        timeout=15,
    ).json()
    headers = {"X-API-Key": created["api_key"]}
    customer_id = created["customer_id"]

    redis_client.set(usage_repo.threshold_key(customer_id, _period()), 3)

    statuses = [
        httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10).status_code
        for _ in range(5)
    ]
    assert statuses == [200, 200, 200, 402, 402]

    state = usage_of(customer_id)
    assert state["billable_requests"] == 3, "refused requests are not billed (ADR-0007)"
    assert state["over_limit"] is True
    assert state["non_billable"]["limit_refused"] == 2


def test_raising_the_limit_starts_serving_again(base_url, redis_client):
    created = httpx.post(
        f"{base_url}/admin/customers",
        json={"name": f"raised-{time.time()}", "plan": "Starter"},
        timeout=15,
    ).json()
    headers = {"X-API-Key": created["api_key"]}
    key = usage_repo.threshold_key(created["customer_id"], _period())

    redis_client.set(key, 1)
    assert httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10).status_code == 200
    assert httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10).status_code == 402

    redis_client.set(key, 5)
    assert httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10).status_code == 200


def test_an_over_limit_customer_can_still_read_their_own_usage(base_url, redis_client):
    """A spending limit caps what a customer SPENDS, and the account endpoints cannot move
    that number -- they are exempt from billing for exactly that reason. Refusing them would
    enforce a cap against requests that can never reach it, and would blind the customer to
    the limit that just stopped them at the one moment they need to see it.

    `/v1/usage` reports `spending_limit.serving`, which is False only in this state: if the
    limit gate refused this path, that field could never be observed as False at all.
    """
    created = httpx.post(
        f"{base_url}/admin/customers",
        json={"name": f"over-limit-visibility-{time.time()}", "plan": "Starter"},
        timeout=15,
    ).json()
    headers = {"X-API-Key": created["api_key"]}
    customer_id = created["customer_id"]

    redis_client.set(usage_repo.threshold_key(customer_id, _period()), 1)

    assert httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10).status_code == 200
    assert httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10).status_code == 402

    usage = httpx.get(f"{base_url}/v1/usage", headers=headers, timeout=15)
    assert usage.status_code == 200, "a refused customer must still be able to ask why"
    limit = usage.json()["spending_limit"]
    assert limit["request_threshold"] == 1
    assert limit["requests_remaining"] == 0
    assert limit["serving"] is False, (
        "the endpoint that explains the refusal must be reachable while refused"
    )

    assert httpx.get(f"{base_url}/v1/invoices", headers=headers, timeout=15).status_code == 200

    # The carve-out is not a hole: neither account call moved the number being capped.
    assert usage_of(customer_id)["billable_requests"] == 1


def test_overshoot_at_the_moment_of_crossing_is_bounded_by_concurrency(base_url, redis_client):
    """ADR-0008's irreducible overshoot: requests already in flight when the counter crosses
    still complete. The bound is concurrency, not spend rate -- which is the whole reason
    the threshold is precomputed rather than refreshed on a timer.
    """
    created = httpx.post(
        f"{base_url}/admin/customers",
        json={"name": f"overshoot-{time.time()}", "plan": "Starter"},
        timeout=15,
    ).json()
    headers = {"X-API-Key": created["api_key"]}
    concurrency, threshold, requests = 20, 10, 120
    redis_client.set(usage_repo.threshold_key(created["customer_id"], _period()), threshold)

    async def fire() -> list[int]:
        limits = httpx.Limits(max_connections=concurrency)
        async with httpx.AsyncClient(limits=limits, timeout=20) as client:
            responses = await asyncio.gather(
                *(client.get(f"{base_url}/v1/echo", headers=headers) for _ in range(requests))
            )
        return [response.status_code for response in responses]

    statuses = asyncio.run(fire())
    served = statuses.count(200)
    billed = usage_of(created["customer_id"])["billable_requests"]

    assert served + statuses.count(402) == requests
    assert served >= threshold
    assert served <= threshold + concurrency, (
        f"served {served} against a threshold of {threshold} at concurrency {concurrency}: "
        "overshoot must be bounded by in-flight requests"
    )
    assert billed == served, "every served request billed, every refused request not"


class TestAnUpgradeCannotSilentlyMakeTheLimitImpossible:
    """ADR-0012 predicted this and the mitigation was never built, so it happened.

    A customer set a Rs. 10,000 limit on Growth, upgraded to Scale, and was refused from
    their next request -- because Scale's prorated fee for half a month is Rs. 45,000, four
    and a half times their whole limit. They took an action expecting MORE capacity and got
    none, with no warning and no explanation.

    The invariant "a limit that cannot be satisfied is not a limit" was enforced on
    PUT /spending-limit and not on POST /plan. Same invariant, two ways to violate it.
    """

    @staticmethod
    def _growth_customer_with_a_small_limit(base_url) -> dict:
        customer = httpx.post(
            f"{base_url}/admin/customers",
            json={"name": f"limit-conflict-{id(object())}", "plan": "Growth"},
            timeout=20,
        ).json()
        limit = httpx.put(
            f"{base_url}/admin/customers/{customer['customer_id']}/spending-limit",
            json={"limit_paisa": 1_000_000},  # Rs. 10,000 -- workable on Growth
            timeout=20,
        )
        assert limit.status_code == 200, limit.text
        assert limit.json()["request_threshold"] > 0, "the limit must be workable to start"
        return customer

    def test_the_upgrade_is_refused_with_both_numbers_named(self, base_url):
        customer = self._growth_customer_with_a_small_limit(base_url)

        response = httpx.post(
            f"{base_url}/admin/customers/{customer['customer_id']}/plan",
            json={"plan": "Scale"},
            timeout=20,
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        # Naming both numbers is the point: "would make the limit impossible" without them
        # leaves the operator guessing which way to move which value.
        assert detail["limit_paisa"] == 1_000_000
        assert detail["prorated_fee_paisa"] > detail["limit_paisa"]
        assert "Rs. 10,000.00" in detail["explanation"]
        assert detail["resolve_by"], "a refusal with no way forward is worse than none"

    def test_it_can_be_overridden_deliberately(self, base_url):
        """A hard block would put engineering's upgrade behind finance's limit. The
        acknowledgement makes it a decision rather than a dead end."""
        customer = self._growth_customer_with_a_small_limit(base_url)

        response = httpx.post(
            f"{base_url}/admin/customers/{customer['customer_id']}/plan",
            json={"plan": "Scale", "acknowledge_limit_conflict": True},
            timeout=20,
        )

        assert response.status_code == 200
        assert response.json()["limit_conflict_acknowledged"] is True

    def test_raising_the_limit_first_lets_the_upgrade_through(self, base_url):
        customer = self._growth_customer_with_a_small_limit(base_url)
        httpx.put(
            f"{base_url}/admin/customers/{customer['customer_id']}/spending-limit",
            json={"limit_paisa": 20_000_000},  # Rs. 200,000, comfortably over Scale's fee
            timeout=20,
        )

        response = httpx.post(
            f"{base_url}/admin/customers/{customer['customer_id']}/plan",
            json={"plan": "Scale"},
            timeout=20,
        )
        assert response.status_code == 200
        assert response.json()["limit_conflict_acknowledged"] is False

    def test_a_customer_with_no_limit_is_never_blocked_from_upgrading(self, base_url):
        customer = httpx.post(
            f"{base_url}/admin/customers",
            json={"name": f"no-limit-{id(object())}", "plan": "Growth"},
            timeout=20,
        ).json()

        response = httpx.post(
            f"{base_url}/admin/customers/{customer['customer_id']}/plan",
            json={"plan": "Scale"},
            timeout=20,
        )
        assert response.status_code == 200


class TestAnImpossibleLimitIsNotReportedAsAnExhaustedOne:
    """'You used up your limit' and 'your limit became impossible' are different facts, and
    only one of them is something the customer did.

    The distinction matters because refusing traffic CANNOT bring the bill under the limit
    once the prorated fee alone exceeds it -- the fee is owed for the days they were on the
    plan whether or not we serve a single request. We still refuse, to stop the overage
    growing, but telling them they exhausted a limit they never got to use is simply false.
    """

    @staticmethod
    def _stuck_customer(base_url) -> dict:
        customer = httpx.post(
            f"{base_url}/admin/customers",
            json={"name": f"unsatisfiable-{id(object())}", "plan": "Growth"},
            timeout=20,
        ).json()
        httpx.put(
            f"{base_url}/admin/customers/{customer['customer_id']}/spending-limit",
            json={"limit_paisa": 1_000_000},
            timeout=20,
        )
        httpx.post(
            f"{base_url}/admin/customers/{customer['customer_id']}/plan",
            json={"plan": "Scale", "acknowledge_limit_conflict": True},
            timeout=20,
        )
        subprocess.run(
            ["python", "-m", "meter.pipeline.cli", "thresholds"],
            capture_output=True, timeout=180,
        )
        return customer

    def test_the_usage_endpoint_explains_why_rather_than_showing_an_exhausted_limit(
        self, base_url
    ):
        customer = self._stuck_customer(base_url)
        body = httpx.get(
            f"{base_url}/v1/usage", headers={"X-API-Key": customer["api_key"]}, timeout=20
        ).json()["spending_limit"]

        assert body["unsatisfiable"] is True
        assert body["serving"] is False
        assert body["why"], "the one place they can find out why must say why"
        # Not an exhausted count: there is no threshold they could stay under.
        assert body["request_threshold"] is None

    def test_the_refusal_names_the_right_reason(self, base_url):
        customer = self._stuck_customer(base_url)
        response = httpx.get(
            f"{base_url}/v1/echo", headers={"X-API-Key": customer["api_key"]}, timeout=20
        )

        assert response.status_code == 402
        assert response.json()["reason"] == "limit_unsatisfiable"

    def test_they_can_still_read_their_own_usage_while_refused(self, base_url):
        """The account endpoints stay reachable, or a cut-off customer cannot discover why."""
        customer = self._stuck_customer(base_url)
        assert httpx.get(
            f"{base_url}/v1/usage", headers={"X-API-Key": customer["api_key"]}, timeout=20
        ).status_code == 200
